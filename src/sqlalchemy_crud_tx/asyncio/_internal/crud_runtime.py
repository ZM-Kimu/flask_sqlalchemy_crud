"""Internal runtime helpers for async CRUD context management."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSessionTransaction

from ...types import AsyncSessionLike, ErrorLogger
from ..transaction import (
    ExistingTxnPolicy,
    activate_txn_state,
    begin_session,
    get_txn_origin_name,
    get_txn_state,
    in_transaction,
    raise_existing_txn_error,
    reset_existing_txn,
)


@dataclass(slots=True)
class EnterScopeResult:
    joined_existing: bool
    nested_txn: AsyncSessionTransaction | None
    owns_provider_session: bool


async def enter_crud_scope(
    *,
    session: AsyncSessionLike,
    existing_txn_policy: ExistingTxnPolicy,
) -> EnterScopeResult:
    """Join or create an async transaction scope for a CRUD context."""
    state = get_txn_state(session)
    joined_existing = bool(state is not None and state.active)
    in_txn = in_transaction(session)
    entered_with_existing_txn = in_txn
    origin_name = get_txn_origin_name(session) if in_txn else None
    nested_txn: AsyncSessionTransaction | None = None

    if joined_existing and not in_txn and state is not None:
        state.active = False
        joined_existing = False

    if not joined_existing:
        if in_txn:
            policy = existing_txn_policy
            if policy == "error":
                raise_existing_txn_error(policy=policy, origin=origin_name)
            if policy == "join":
                joined_existing = True
            elif policy == "savepoint":
                joined_existing = True
                nested_txn = await session.begin_nested()
            elif policy == "adopt_autobegin":
                if origin_name not in (None, "AUTOBEGIN"):
                    raise_existing_txn_error(policy=policy, origin=origin_name)
            elif policy == "reset":
                await reset_existing_txn(
                    session,
                    policy=policy,
                    origin=origin_name,
                )
                in_txn = False
            else:
                raise ValueError(f"Unsupported existing_txn_policy: {policy}")

        state = activate_txn_state(session)
        if not (joined_existing or in_txn):
            await begin_session(session, state)

    assert state is not None
    state.depth += 1

    return EnterScopeResult(
        joined_existing=joined_existing,
        nested_txn=nested_txn,
        owns_provider_session=not entered_with_existing_txn and not joined_existing,
    )


async def exit_crud_scope(
    *,
    model_name: str,
    logger: ErrorLogger,
    session: AsyncSessionLike | None,
    nested_txn: AsyncSessionTransaction | None,
    need_commit: bool,
    explicit_committed: bool,
    joined_existing: bool,
    owns_provider_session: bool,
    discarded: bool,
    error: Exception | None,
    exc_type: type[BaseException] | None,
    exc_val: BaseException | None,
    exc_tb: TracebackType | None,
    close_session: Callable[[AsyncSessionLike], Awaitable[None]],
) -> None:
    """Finalize an async CRUD context and close owned session if needed."""
    session_to_close: AsyncSessionLike | None = None
    should_close_owned_session = False
    try:
        has_exc = bool(exc_type or exc_val or exc_tb)
        should_rollback = has_exc or error is not None or discarded

        if should_rollback:
            if has_exc or error:
                logger(
                    "CRUD[%s]: <catch: %s> <except: (%s: %s)>",
                    model_name,
                    error,
                    exc_type,
                    exc_val,
                )
            if nested_txn is not None and nested_txn.is_active:
                try:
                    await nested_txn.rollback()
                except Exception:
                    logger("CRUD sub-txn rollback failed", exc_info=True)
            need_commit = False
        elif need_commit and not explicit_committed:
            try:
                if nested_txn is not None and nested_txn.is_active:
                    await nested_txn.commit()
            except Exception as exc:
                logger("CRUD sub-txn commit failed: %s", exc)
                raise

        if session is not None:
            session_to_close = session
            should_close_owned_session = owns_provider_session and not joined_existing
            state = get_txn_state(session)
            if state is not None and state.active:
                state.depth -= 1
                is_outermost = state.depth <= 0
                if is_outermost:
                    state.active = False
                    try:
                        if should_rollback and not joined_existing:
                            await session.rollback()
                        elif (
                            need_commit
                            and not explicit_committed
                            and not joined_existing
                        ):
                            await session.commit()
                    except Exception as exc:
                        logger("CRUD commit/rollback failed: %s", exc)
                        try:
                            await session.rollback()
                        except Exception:
                            pass
                        raise
                else:
                    should_close_owned_session = False
    finally:
        if should_close_owned_session and session_to_close is not None:
            await close_session(session_to_close)
