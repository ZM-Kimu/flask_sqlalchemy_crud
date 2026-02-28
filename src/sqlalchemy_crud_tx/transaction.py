"""Generic transaction state machine and decorator implementation."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from types import TracebackType
from typing import Any, Literal, ParamSpec, TypeAlias, TypeVar, cast

from sqlalchemy.exc import InvalidRequestError, SQLAlchemyError

from .types import SessionLike, SessionProvider

P = ParamSpec("P")
R = TypeVar("R")

ErrorPolicy = Literal["raise", "status_only"]
ExistingTxnPolicy = Literal[
    "error",
    "join",
    "savepoint",
    "adopt_autobegin",
    "reset",
]

TransactionDecorator: TypeAlias = Callable[[Callable[P, R]], Callable[P, R]]


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


def _resolve_session(session: SessionLike) -> Any:
    """Return a real Session instance from a SessionLike value."""
    if hasattr(session, "in_transaction") or hasattr(session, "get_transaction"):
        return session
    if callable(session):
        try:
            return session()
        except Exception:
            return session
    return session


def in_transaction(session: SessionLike) -> bool:
    """Safely check whether a Session is currently in a transaction."""
    session_obj = _resolve_session(session)
    try:
        return bool(session_obj.in_transaction())
    except Exception:
        return False


def _get_transaction(session: SessionLike) -> Any | None:
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
    origin = getattr(txn, "origin", None)
    if origin is None:
        return None
    name = getattr(origin, "name", None)
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


def raise_existing_txn_error(
    *,
    policy: ExistingTxnPolicy,
    origin: str | None,
    detail: str | None = None,
) -> None:
    """Raise a consistent InvalidRequestError for existing transaction conflicts."""
    origin_label = origin or "UNKNOWN"
    hint = (
        "Configure CRUD.configure(existing_txn_policy='join'|'savepoint'|"
        "'adopt_autobegin'|'reset') to change this behavior."
    )
    detail_text = f" {detail}" if detail else ""
    raise InvalidRequestError(
        "Session already has an active transaction "
        f"(origin={origin_label}). existing_txn_policy='{policy}' "
        f"disallows this operation.{detail_text} {hint}"
    )


def activate_txn_state(session: SessionLike) -> TxnState:
    """Create or reset the transaction state for a Session."""
    state = _get_or_create_txn_state(session)
    state.depth = 0
    state.active = True
    return state


def begin_session(session: SessionLike, state: TxnState) -> None:
    """Begin a transaction and mark state inactive on failure."""
    try:
        session.begin()
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
    session.rollback()


class _TxnContext:
    """Basic building block for a transaction context manager.

    Currently only used as an internal helper:
    - obtains a Session via ``SessionProvider``;
    - ensures there is a ``TxnState`` associated with that Session.

    Commit/rollback behaviour is handled by the generic ``transaction(...)``
    decorator.
    """

    __slots__ = ("_session_provider", "_session", "_state")

    def __init__(self, session_provider: SessionProvider) -> None:
        self._session_provider = session_provider
        self._session: SessionLike | None = None
        self._state: TxnState | None = None

    @property
    def session(self) -> SessionLike:
        assert self._session is not None
        return self._session

    @property
    def state(self) -> TxnState | None:
        return self._state

    def __enter__(self) -> "_TxnContext":
        session = self._session_provider()
        self._session = session
        self._state = _get_or_create_txn_state(session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> Literal[False]:
        # Commit/rollback logic is handled by the generic transaction decorator;
        # this context itself never touches the database.
        return False


def transaction(
    session_provider: SessionProvider,
    *,
    join_existing: bool = True,
    existing_txn_policy: ExistingTxnPolicy = "error",
    # nested: bool | None = None, TODO: Implement soon
    error_policy: ErrorPolicy = "raise",
) -> TransactionDecorator[P, R]:
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
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            # Obtain Session and its transaction state mapping.
            session = session_provider()
            state = get_txn_state(session)
            in_txn = in_transaction(session)
            origin_name = get_txn_origin_name(session) if in_txn else None

            if state is not None and state.active and not in_txn:
                # Stale internal state; reset so policy can re-evaluate.
                state.active = False
                state.depth = 0

            # Whether to join an existing transaction.
            joining_existing = bool(
                join_existing and state is not None and state.active
            )
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

        return wrapper

    return decorator
