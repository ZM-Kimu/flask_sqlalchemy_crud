"""Async transaction state machine and decorator implementation."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from functools import wraps
from typing import Any, Literal, ParamSpec, TypeAlias, TypeVar, cast

from sqlalchemy.exc import InvalidRequestError, SQLAlchemyError

from ..types import AsyncSessionLike, AsyncSessionProvider

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


class TxnState:
    """Transaction state associated with one AsyncSession."""

    __slots__ = ("session", "depth", "active")

    def __init__(self, session: AsyncSessionLike) -> None:
        self.session: AsyncSessionLike = session
        self.depth: int = 0
        self.active: bool = False


_TxnMap: TypeAlias = dict[int, TxnState]

_current_txn_map: ContextVar[_TxnMap] = ContextVar("_current_async_txn_map")
_current_error_policy: ContextVar[ErrorPolicy | None] = ContextVar(
    "_current_async_error_policy"
)


def _get_txn_map() -> _TxnMap:
    try:
        return _current_txn_map.get()
    except LookupError:
        mapping: _TxnMap = {}
        _current_txn_map.set(mapping)
        return mapping


def get_txn_state(session: AsyncSessionLike) -> TxnState | None:
    return _get_txn_map().get(id(session))


def _get_or_create_txn_state(session: AsyncSessionLike) -> TxnState:
    mapping = _get_txn_map()
    key = id(session)
    state = mapping.get(key)
    if state is None:
        state = TxnState(session)
        mapping[key] = state
    return state


def get_current_error_policy() -> ErrorPolicy | None:
    try:
        return _current_error_policy.get()
    except LookupError:
        return None


def _resolve_session(session: AsyncSessionLike) -> Any:
    if hasattr(session, "in_transaction") or hasattr(session, "get_transaction"):
        return session
    if callable(session):
        try:
            return session()
        except Exception:
            return session
    return session


def in_transaction(session: AsyncSessionLike) -> bool:
    session_obj = _resolve_session(session)
    try:
        return bool(session_obj.in_transaction())
    except Exception:
        return False


def _get_transaction(session: AsyncSessionLike) -> Any | None:
    session_obj = _resolve_session(session)
    try:
        return session_obj.get_transaction()
    except Exception:
        return None


def get_txn_origin_name(session: AsyncSessionLike) -> str | None:
    txn = _get_transaction(session)
    if txn is None:
        return None

    origin = getattr(txn, "origin", None)
    if origin is None:
        sync_txn = getattr(txn, "sync_transaction", None)
        if sync_txn is not None:
            origin = getattr(sync_txn, "origin", None)

    if origin is None:
        return None
    name = getattr(origin, "name", None)
    if name:
        return name
    return str(origin).split(".")[-1]


def _has_pending_changes(session: AsyncSessionLike) -> bool:
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


def activate_txn_state(session: AsyncSessionLike) -> TxnState:
    state = _get_or_create_txn_state(session)
    state.depth = 0
    state.active = True
    return state


async def begin_session(session: AsyncSessionLike, state: TxnState) -> None:
    try:
        await session.begin()
    except Exception:
        state.active = False
        raise


async def reset_existing_txn(
    session: AsyncSessionLike, *, policy: ExistingTxnPolicy, origin: str | None
) -> None:
    if _has_pending_changes(session):
        raise_existing_txn_error(
            policy=policy,
            origin=origin,
            detail="Pending changes found; reset is unsafe.",
        )
    await session.rollback()


def transaction(
    session_provider: AsyncSessionProvider,
    *,
    join_existing: bool = True,
    existing_txn_policy: ExistingTxnPolicy = "error",
    error_policy: ErrorPolicy = "raise",
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Async transaction decorator.

    Only async callables are supported.
    """

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        if not inspect.iscoroutinefunction(func):
            raise TypeError(
                "CRUD.transaction() in asyncio namespace only supports async "
                "functions."
            )

        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            session = session_provider()
            state = get_txn_state(session)
            in_txn = in_transaction(session)
            origin_name = get_txn_origin_name(session) if in_txn else None

            if state is not None and state.active and not in_txn:
                state.active = False
                state.depth = 0

            joining_existing = bool(
                join_existing and state is not None and state.active
            )
            adopted_external = False
            nested_txn = None

            token = None

            try:
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
                            nested_txn = await session.begin_nested()
                        elif existing_txn_policy == "adopt_autobegin":
                            if origin_name not in (None, "AUTOBEGIN"):
                                raise_existing_txn_error(
                                    policy=existing_txn_policy, origin=origin_name
                                )
                            adopted_external = True
                        elif existing_txn_policy == "reset":
                            await reset_existing_txn(
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
                        await begin_session(session, state)
                        token = _current_error_policy.set(error_policy)

                assert state is not None
                state.depth += 1

                captured_exc: BaseException | None = None
                result: R | None = None

                try:
                    result = await func(*args, **kwargs)
                    return result
                except BaseException as exc:
                    captured_exc = exc

                    if not joining_existing:
                        try:
                            await session.rollback()
                        except Exception:
                            pass
                    if nested_txn is not None:
                        try:
                            await nested_txn.rollback()
                        except Exception:
                            pass

                    is_db_error = isinstance(exc, SQLAlchemyError)
                    if not is_db_error:
                        raise
                    if error_policy == "raise":
                        raise

                    return cast(R, None)
                finally:
                    if state.active:
                        state.depth -= 1
                        if state.depth <= 0:
                            state.active = False
                            if captured_exc is None and not joining_existing:
                                try:
                                    await session.commit()
                                except Exception as commit_exc:
                                    try:
                                        await session.rollback()
                                    except Exception:
                                        pass
                                    raise commit_exc
                    if nested_txn is not None and captured_exc is None:
                        try:
                            await nested_txn.commit()
                        except Exception as commit_exc:
                            try:
                                await nested_txn.rollback()
                            except Exception:
                                pass
                            raise commit_exc
            finally:
                if token is not None:
                    _current_error_policy.reset(token)

        return wrapper

    return decorator
