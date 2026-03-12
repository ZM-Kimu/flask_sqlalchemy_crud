"""Generic transaction state machine and decorator implementation."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from functools import wraps
from typing import ParamSpec, TypeAlias, TypeVar, cast

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, SessionTransaction

from ._internal.transaction_common import (
    ErrorPolicy,
    ExistingTxnPolicy,
    raise_existing_txn_error,
)
from .types import SessionLike, SessionProvider

P = ParamSpec("P")
R = TypeVar("R")


class TxnState:
    """Transaction state associated with a single Session.

    Shared between the generic transaction state machine and CRUD contexts to
    track:
    - join depth (``depth``);
    - whether there is an active transaction (``active``).
    """

    __slots__ = ("session", "depth", "active")

    def __init__(self, session: SessionLike) -> None:
        self.session: SessionLike = session
        self.depth: int = 0  # current join depth
        self.active: bool = False  # whether there is an active transaction


_TxnMap: TypeAlias = dict[int, TxnState]

_current_txn_map: ContextVar[_TxnMap] = ContextVar("_current_txn_map")
_current_error_policy: ContextVar[ErrorPolicy | None] = ContextVar(
    "_current_error_policy"
)


def _get_txn_map() -> _TxnMap:
    """Return the transaction state mapping for the current ContextVar scope.

    The mapping uses ``id(Session)`` as the key and stores the corresponding
    ``TxnState``.
    """
    try:
        return _current_txn_map.get()
    except LookupError:
        mapping: _TxnMap = {}
        _current_txn_map.set(mapping)
        return mapping


def get_txn_state(session: SessionLike) -> TxnState | None:
    """Return the transaction state associated with a Session, if any."""
    return _get_txn_map().get(id(session))


def _get_or_create_txn_state(session: SessionLike) -> TxnState:
    """Get or create the transaction state for the given Session.

    The transaction state machine uses this structure to implement join/nested
    semantics.
    """
    mapping = _get_txn_map()
    key = id(session)
    state = mapping.get(key)
    if state is None:
        state = TxnState(session)
        mapping[key] = state
    return state


def get_current_error_policy() -> ErrorPolicy | None:
    """Return the current ``error_policy`` from the ContextVar, if any."""
    try:
        return _current_error_policy.get()
    except LookupError:
        return None


def _resolve_session(session: SessionLike) -> Session:
    """Return a concrete ``Session`` from a ``SessionLike`` value."""
    if isinstance(session, Session):
        return session
    return session()


def in_transaction(session: SessionLike) -> bool:
    """Safely check whether a Session is currently in a transaction."""
    session_obj = _resolve_session(session)
    try:
        return bool(session_obj.in_transaction())
    except Exception:
        return False


def _get_transaction(session: SessionLike) -> SessionTransaction | None:
    """Return the current transaction object for a Session, if any."""
    session_obj = _resolve_session(session)
    try:
        return session_obj.get_transaction()
    except Exception:
        return None


def get_txn_origin_name(session: SessionLike) -> str | None:
    """Return the origin name for the current transaction, if available."""
    txn = _get_transaction(session)
    if txn is None:
        return None
    try:
        origin = txn.origin
    except Exception:
        origin = None
    if origin is None:
        return None
    try:
        name = origin.name
    except Exception:
        name = None
    if name:
        return name
    return str(origin).split(".")[-1]


def _has_pending_changes(session: SessionLike) -> bool:
    """Return True if the Session has pending changes."""
    session_obj = _resolve_session(session)
    try:
        return bool(session_obj.new or session_obj.dirty or session_obj.deleted)
    except Exception:
        return False


def activate_txn_state(session: SessionLike) -> TxnState:
    """Create or reset the transaction state for a Session."""
    state = _get_or_create_txn_state(session)
    state.depth = 0
    state.active = True
    return state


def begin_session(session: SessionLike, state: TxnState) -> None:
    """Begin a transaction and mark state inactive on failure."""
    try:
        _resolve_session(session).begin()
    except Exception:
        state.active = False
        raise


def reset_existing_txn(
    session: SessionLike, *, policy: ExistingTxnPolicy, origin: str | None
) -> None:
    """Rollback an existing transaction if it is safe to do so."""
    if _has_pending_changes(session):
        raise_existing_txn_error(
            policy=policy,
            origin=origin,
            detail="Pending changes found; reset is unsafe.",
        )
    _resolve_session(session).rollback()


def _close_managed_session(session: SessionLike) -> None:
    try:
        if isinstance(session, Session):
            session.close()
            return
        session.remove()
        return
    except Exception:
        # Closing must not mask business exceptions raised by wrapped function.
        pass


def transaction(
    session_provider: SessionProvider,
    *,
    join_existing: bool = True,
    existing_txn_policy: ExistingTxnPolicy = "error",
    error_policy: ErrorPolicy = "raise",
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Generic transaction decorator.

    - Each function call corresponds to a "transaction scope", unless the call
      joins an already active transaction according to the ``join_existing`` rules.
    - Default join semantics: if there is an active transaction for the same
      Session, join it and let only the outermost call perform commit/rollback.
    - existing_txn_policy controls how to handle an already-active transaction:
        - "error": raise InvalidRequestError (default).
        - "join": join the existing transaction and skip commit/rollback here.
        - "savepoint": begin a nested transaction (SAVEPOINT).
        - "adopt_autobegin": only allow AUTOBEGIN and treat it as owned.
        - "reset": rollback only if there are no pending changes, then begin.
    - ``error_policy`` only affects ``SQLAlchemyError``:
        - ``"raise"``: rollback and then re-raise the database error;
        - ``"status_only"``: rollback and swallow the database error so
          callers can inspect status via other channels;
        - non-database exceptions always cause rollback and are re-raised.
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            # Obtain Session and its transaction state mapping.
            session = session_provider()
            state = get_txn_state(session)
            in_txn = in_transaction(session)
            entered_with_existing_txn = in_txn
            origin_name = get_txn_origin_name(session) if in_txn else None

            if state is not None and state.active and not in_txn:
                # Stale internal state; reset so policy can re-evaluate.
                state.active = False
                state.depth = 0

            # Whether to join an existing transaction.
            joining_existing = bool(
                join_existing and state is not None and state.active
            )
            should_close_session = not entered_with_existing_txn and not joining_existing
            adopted_external = False
            nested_txn = None

            token = None

            try:
                # If there is no active transaction, create state and begin one.
                if not joining_existing:
                    if in_txn:
                        if existing_txn_policy == "error":
                            raise_existing_txn_error(
                                policy=existing_txn_policy, origin=origin_name
                            )
                        if existing_txn_policy == "join":
                            joining_existing = True
                        elif existing_txn_policy == "savepoint":
                            joining_existing = True
                            nested_txn = session.begin_nested()
                        elif existing_txn_policy == "adopt_autobegin":
                            if origin_name != "AUTOBEGIN":
                                raise_existing_txn_error(
                                    policy=existing_txn_policy, origin=origin_name
                                )
                            adopted_external = True
                        elif existing_txn_policy == "reset":
                            reset_existing_txn(
                                session,
                                policy=existing_txn_policy,
                                origin=origin_name,
                            )
                            in_txn = False
                        else:
                            raise ValueError(
                                f"Unsupported existing_txn_policy: {existing_txn_policy}"
                            )

                    if joining_existing or adopted_external:
                        state = activate_txn_state(session)
                        if adopted_external:
                            token = _current_error_policy.set(error_policy)
                    elif not in_txn:
                        state = activate_txn_state(session)
                        begin_session(session, state)
                        token = _current_error_policy.set(error_policy)

                assert state is not None
                state.depth += 1

                captured_exc: BaseException | None = None
                result: R | None = None

                try:
                    result = func(*args, **kwargs)
                    return result
                except BaseException as exc:
                    captured_exc = exc

                    if not joining_existing:
                        try:
                            session.rollback()
                        except Exception:
                            # Rollback failure should not mask the original exception.
                            pass
                    if nested_txn is not None:
                        try:
                            nested_txn.rollback()
                        except Exception:
                            pass

                    is_db_error = isinstance(exc, SQLAlchemyError)

                    if not is_db_error:
                        raise

                    # DB errors may be re-raised depending on error_policy
                    if error_policy == "raise":
                        raise

                    # error_policy == "status_only": swallow SQLAlchemyError,
                    # caller (e.g., CRUD) should record status separately.
                    return cast(R, None)
                finally:
                    # Only adjust depth while state is still active.
                    if state.active:
                        state.depth -= 1
                        if state.depth <= 0:
                            state.active = False
                            # Commit only when outermost and no exception.
                            if captured_exc is None and not joining_existing:
                                try:
                                    session.commit()
                                except Exception as commit_exc:
                                    # On commit failure, attempt rollback then re-raise.
                                    try:
                                        session.rollback()
                                    except Exception:
                                        pass
                                    raise commit_exc
                    if nested_txn is not None and captured_exc is None:
                        try:
                            nested_txn.commit()
                        except Exception as commit_exc:
                            try:
                                nested_txn.rollback()
                            except Exception:
                                pass
                            raise commit_exc
            finally:
                if token is not None:
                    _current_error_policy.reset(token)
                if should_close_session:
                    _close_managed_session(session)

        return wrapper

    return decorator
