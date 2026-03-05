"""Async CRUD helper utilities and transaction integration."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Generic,
    ParamSpec,
    Self,
    TypeAlias,
    TypeVar,
    cast,
    overload,
)

from sqlalchemy import delete as sa_delete
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select as sa_select
from sqlalchemy import tuple_ as sa_tuple
from sqlalchemy.engine import CursorResult, Result, ScalarResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSessionTransaction
from sqlalchemy.orm import Mapper, object_session
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import TypedReturnsRows

from ..status import SQLStatus
from ..types import AsyncSessionLike, AsyncSessionProvider, ErrorLogger, ORMModel
from .transaction import (
    ErrorPolicy,
    ExistingTxnPolicy,
    activate_txn_state,
    begin_session,
    get_current_error_policy,
    get_txn_origin_name,
    get_txn_state,
    in_transaction,
    raise_existing_txn_error,
    reset_existing_txn,
)
from .transaction import transaction as _txn_transaction

P = ParamSpec("P")
R = TypeVar("R")

ModelTypeVar = TypeVar("ModelTypeVar", bound=ORMModel)
RowTypeVar = TypeVar("RowTypeVar", bound=tuple[Any, ...])
ScalarTypeVar = TypeVar("ScalarTypeVar")
EntityTypeVar1 = TypeVar("EntityTypeVar1")
EntityTypeVar2 = TypeVar("EntityTypeVar2")
EntityTypeVar3 = TypeVar("EntityTypeVar3")
EntityTypeVar4 = TypeVar("EntityTypeVar4")
EntityTypeVar5 = TypeVar("EntityTypeVar5")
EntityTypeVar6 = TypeVar("EntityTypeVar6")
EntityTypeVar7 = TypeVar("EntityTypeVar7")
EntityTypeVar8 = TypeVar("EntityTypeVar8")

_DEFAULT_LOGGER: ErrorLogger = logging.getLogger("CRUD").error


class AsyncSessionProxy:
    """Async session facade exposed to callers."""

    __slots__ = ("_crud", "_session")

    def __init__(self, crud: "CRUD[Any]", session: AsyncSessionLike) -> None:
        self._crud = crud
        self._session = session

    async def commit(self) -> None:
        self._crud.logger(
            "CRUD.session.commit() is redirected to CRUD.commit(); "
            "consider calling CRUD.commit() explicitly.",
        )
        await self._crud.commit()

    async def rollback(self) -> None:
        self._crud.logger(
            "CRUD.session.rollback() is redirected to CRUD.discard(); "
            "consider calling CRUD.discard() explicitly.",
        )
        await self._crud.discard()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


if TYPE_CHECKING:
    SessionViewType: TypeAlias = AsyncSessionLike
else:
    SessionViewType = AsyncSessionProxy


class CRUD(Generic[ModelTypeVar]):
    """Async generic CRUD wrapper."""

    _global_filter_conditions: ClassVar[tuple[list[Any], dict[str, Any]]] = ([], {})
    _session_provider: ClassVar[tuple[AsyncSessionProvider] | None] = None
    _default_error_policy: ClassVar[ErrorPolicy] = "raise"
    _existing_txn_policy: ClassVar[ExistingTxnPolicy] = "error"
    _logger: ClassVar[ErrorLogger] = _DEFAULT_LOGGER

    @classmethod
    def register_global_filters(cls, *base_exprs: Any, **base_kwargs: Any) -> None:
        cls._global_filter_conditions = (list(base_exprs) or []), (base_kwargs or {})

    def __init__(self, model: type[ModelTypeVar], **kwargs: Any) -> None:
        self._model = model
        self._kwargs = kwargs

        self._base_filter_exprs: list[Any] = list(self._global_filter_conditions[0])
        self._base_filter_kwargs: dict[str, Any] = dict(
            self._global_filter_conditions[1]
        )
        self._instance_default_kwargs: dict[str, Any] = dict(kwargs)

        self.error: Exception | None = None
        self.status: SQLStatus = SQLStatus.OK

        self._need_commit = False
        self._error_policy: ErrorPolicy | None = None
        self._apply_global_filters = True
        self._joined_existing = False
        self._nested_txn: AsyncSessionTransaction | None = None
        self._explicit_committed = False
        self._discarded = False
        self._session: AsyncSessionLike | None = None

    def resolve_error_policy(self) -> ErrorPolicy:
        from_ctx = get_current_error_policy()
        if from_ctx is not None:
            return from_ctx
        if self._error_policy is not None:
            return self._error_policy
        return self._default_error_policy

    async def __aenter__(self) -> Self:
        session = self._get_session()

        state = get_txn_state(session)
        joined_existing = bool(state is not None and state.active)
        in_txn = in_transaction(session)
        origin_name = get_txn_origin_name(session) if in_txn else None

        if joined_existing and not in_txn and state is not None:
            state.active = False
            joined_existing = False

        if not joined_existing:
            if in_txn:
                policy = type(self)._existing_txn_policy
                if policy == "error":
                    raise_existing_txn_error(policy=policy, origin=origin_name)
                if policy == "join":
                    joined_existing = True
                elif policy == "savepoint":
                    joined_existing = True
                    self._nested_txn = await session.begin_nested()
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

        self._session = session
        self._joined_existing = joined_existing
        self._explicit_committed = False
        self._discarded = False
        return self

    @classmethod
    def configure(
        cls,
        *,
        session_provider: AsyncSessionProvider | None = None,
        logger: ErrorLogger | None = None,
        error_policy: ErrorPolicy | None = None,
        existing_txn_policy: ExistingTxnPolicy | None = None,
    ) -> None:
        if session_provider is None:
            raise ValueError(
                "session_provider is required for CRUD.configure; "
                "pass a callable that returns an active AsyncSession."
            )

        cls._session_provider = (session_provider,)

        if logger is not None:
            cls._logger = logger
        if error_policy is not None:
            cls._default_error_policy = error_policy
        if existing_txn_policy is not None:
            cls._existing_txn_policy = existing_txn_policy

    @classmethod
    def _get_session_provider(cls) -> AsyncSessionProvider:
        if cls._session_provider is None:
            raise RuntimeError(
                "CRUD session is not configured. Please call "
                "CRUD.configure(session_provider=...) before using CRUD."
            )
        return cls._session_provider[0]

    def _get_session(self) -> AsyncSessionLike:
        provider = self._get_session_provider()
        return provider()

    def _require_session(self) -> AsyncSessionLike:
        if self._session is None:
            raise RuntimeError("CRUD session is not bound to current context.")
        return self._session

    @property
    def session(self) -> SessionViewType:
        session = self._require_session()
        return cast(SessionViewType, AsyncSessionProxy(self, session))

    def config(
        self,
        error_policy: ErrorPolicy | None = None,
        disable_global_filter: bool | None = None,
    ) -> Self:
        if error_policy is not None:
            self._error_policy = error_policy
        if disable_global_filter is not None:
            self._apply_global_filters = not disable_global_filter
        return self

    def create_instance(self, **kwargs: Any) -> ModelTypeVar:
        payload = dict(self._kwargs)
        payload.update(kwargs)
        return self._model(**payload)

    async def add(
        self,
        instance: ModelTypeVar | None = None,
        **kwargs: Any,
    ) -> ModelTypeVar | None:
        try:
            session = self._require_session()
            await self._ensure_nested_txn()

            if instance is None:
                target = self.create_instance(**kwargs)
            else:
                target = await self._merge_if_needed(session, instance)
                self._apply_updates(session, target, kwargs)

            session.add(target)
            await session.flush()
            self._need_commit = True
            return target
        except SQLAlchemyError as exc:
            await self._on_sql_error(exc)
        except Exception as exc:
            self.error = exc
            self.status = SQLStatus.INTERNAL_ERR
        return None

    async def add_many(
        self,
        instances: list[ModelTypeVar],
        **kwargs: Any,
    ) -> list[ModelTypeVar] | None:
        try:
            if not instances:
                return []

            session = self._require_session()
            await self._ensure_nested_txn()

            managed_instances: list[ModelTypeVar] = []
            for instance in instances:
                target = await self._merge_if_needed(session, instance)
                self._apply_updates(session, target, kwargs)
                managed_instances.append(target)

            session.add_all(managed_instances)
            await session.flush()
            self._need_commit = True
            return managed_instances
        except SQLAlchemyError as exc:
            await self._on_sql_error(exc)
        except Exception as exc:
            self.error = exc
            self.status = SQLStatus.INTERNAL_ERR
        return None

    @overload
    def select(
        self,
        *,
        pure: bool = False,
        **kwargs: Any,
    ) -> Select[tuple[ModelTypeVar]]: ...

    @overload
    def select(
        self,
        entity1: EntityTypeVar1,
        *,
        pure: bool = False,
        **kwargs: Any,
    ) -> Select[tuple[EntityTypeVar1]]: ...

    @overload
    def select(
        self,
        entity1: EntityTypeVar1,
        entity2: EntityTypeVar2,
        *,
        pure: bool = False,
        **kwargs: Any,
    ) -> Select[tuple[EntityTypeVar1, EntityTypeVar2]]: ...

    @overload
    def select(
        self,
        entity1: EntityTypeVar1,
        entity2: EntityTypeVar2,
        entity3: EntityTypeVar3,
        *,
        pure: bool = False,
        **kwargs: Any,
    ) -> Select[tuple[EntityTypeVar1, EntityTypeVar2, EntityTypeVar3]]: ...

    @overload
    def select(
        self,
        entity1: EntityTypeVar1,
        entity2: EntityTypeVar2,
        entity3: EntityTypeVar3,
        entity4: EntityTypeVar4,
        *,
        pure: bool = False,
        **kwargs: Any,
    ) -> Select[
        tuple[EntityTypeVar1, EntityTypeVar2, EntityTypeVar3, EntityTypeVar4]
    ]: ...

    @overload
    def select(
        self,
        entity1: EntityTypeVar1,
        entity2: EntityTypeVar2,
        entity3: EntityTypeVar3,
        entity4: EntityTypeVar4,
        entity5: EntityTypeVar5,
        *,
        pure: bool = False,
        **kwargs: Any,
    ) -> Select[
        tuple[
            EntityTypeVar1,
            EntityTypeVar2,
            EntityTypeVar3,
            EntityTypeVar4,
            EntityTypeVar5,
        ]
    ]: ...

    @overload
    def select(
        self,
        entity1: EntityTypeVar1,
        entity2: EntityTypeVar2,
        entity3: EntityTypeVar3,
        entity4: EntityTypeVar4,
        entity5: EntityTypeVar5,
        entity6: EntityTypeVar6,
        *,
        pure: bool = False,
        **kwargs: Any,
    ) -> Select[
        tuple[
            EntityTypeVar1,
            EntityTypeVar2,
            EntityTypeVar3,
            EntityTypeVar4,
            EntityTypeVar5,
            EntityTypeVar6,
        ]
    ]: ...

    @overload
    def select(
        self,
        entity1: EntityTypeVar1,
        entity2: EntityTypeVar2,
        entity3: EntityTypeVar3,
        entity4: EntityTypeVar4,
        entity5: EntityTypeVar5,
        entity6: EntityTypeVar6,
        entity7: EntityTypeVar7,
        *,
        pure: bool = False,
        **kwargs: Any,
    ) -> Select[
        tuple[
            EntityTypeVar1,
            EntityTypeVar2,
            EntityTypeVar3,
            EntityTypeVar4,
            EntityTypeVar5,
            EntityTypeVar6,
            EntityTypeVar7,
        ]
    ]: ...

    @overload
    def select(
        self,
        entity1: EntityTypeVar1,
        entity2: EntityTypeVar2,
        entity3: EntityTypeVar3,
        entity4: EntityTypeVar4,
        entity5: EntityTypeVar5,
        entity6: EntityTypeVar6,
        entity7: EntityTypeVar7,
        entity8: EntityTypeVar8,
        *,
        pure: bool = False,
        **kwargs: Any,
    ) -> Select[
        tuple[
            EntityTypeVar1,
            EntityTypeVar2,
            EntityTypeVar3,
            EntityTypeVar4,
            EntityTypeVar5,
            EntityTypeVar6,
            EntityTypeVar7,
            EntityTypeVar8,
        ]
    ]: ...

    def select(
        self,
        *entities: Any,
        pure: bool = False,
        **kwargs: Any,
    ) -> Select[Any]:
        statement = sa_select(*entities) if entities else sa_select(self._model)
        if not pure:
            if self._instance_default_kwargs:
                statement = statement.filter_by(**self._instance_default_kwargs)
            if self._apply_global_filters:
                if self._base_filter_exprs:
                    statement = statement.where(*self._base_filter_exprs)
                if self._base_filter_kwargs:
                    statement = statement.filter_by(**self._base_filter_kwargs)
        if kwargs:
            statement = statement.filter_by(**kwargs)
        return cast(Select[Any], statement)

    async def execute(
        self,
        statement: TypedReturnsRows[RowTypeVar],
        *args: Any,
        **kwargs: Any,
    ) -> Result[RowTypeVar]:
        session = self._require_session()
        return await session.execute(statement, *args, **kwargs)

    async def scalars(
        self,
        statement: TypedReturnsRows[tuple[ScalarTypeVar]],
        *args: Any,
        **kwargs: Any,
    ) -> ScalarResult[ScalarTypeVar]:
        session = self._require_session()
        return await session.scalars(statement, *args, **kwargs)

    async def scalar(
        self,
        statement: TypedReturnsRows[tuple[ScalarTypeVar]],
        *args: Any,
        **kwargs: Any,
    ) -> ScalarTypeVar | None:
        session = self._require_session()
        return await session.scalar(statement, *args, **kwargs)

    async def first(
        self, stmt: Select[tuple[ModelTypeVar]] | None = None
    ) -> ModelTypeVar | None:
        effective_stmt = stmt if stmt is not None else self.select()
        result = await self.scalars(effective_stmt)
        return result.first()

    async def all(
        self, stmt: Select[tuple[ModelTypeVar]] | None = None
    ) -> list[ModelTypeVar]:
        effective_stmt = stmt if stmt is not None else self.select()
        result = await self.scalars(effective_stmt)
        return list(result.all())

    async def update(
        self,
        instance: ModelTypeVar | None = None,
        *,
        stmt: Select[tuple[ModelTypeVar]] | None = None,
        **kwargs: Any,
    ) -> ModelTypeVar | None:
        try:
            target_instance = (
                instance if instance is not None else await self.first(stmt=stmt)
            )
            if target_instance is None:
                self.status = SQLStatus.NOT_FOUND
                return None

            session = self._require_session()
            await self._ensure_nested_txn()
            target = await self._merge_if_needed(session, target_instance)
            self._apply_updates(session, target, kwargs)
            self._need_commit = True
            return target
        except SQLAlchemyError as exc:
            await self._on_sql_error(exc)
        except Exception as exc:
            self.error = exc
            self.status = SQLStatus.INTERNAL_ERR
        return None

    async def delete(
        self,
        instance: ModelTypeVar | None = None,
        *,
        stmt: Select[tuple[ModelTypeVar]] | None = None,
        all_records: bool = False,
    ) -> bool:
        try:
            session = self._require_session()
            if instance is not None:
                await self._ensure_nested_txn()
                await session.delete(instance)
            else:
                effective_stmt = stmt if stmt is not None else self.select()
                if all_records:
                    mapper = cast(Mapper[ModelTypeVar] | None, sa_inspect(self._model))
                    if mapper is None:
                        self.status = SQLStatus.INTERNAL_ERR
                        return False

                    primary_keys: list[ColumnElement[Any]] = [
                        col for col in mapper.primary_key
                    ]
                    if not primary_keys:
                        self.status = SQLStatus.INTERNAL_ERR
                        return False

                    primary_key_names: list[str] = []
                    for pk in primary_keys:
                        pk_key = getattr(pk, "key", None)
                        if not isinstance(pk_key, str):
                            self.status = SQLStatus.INTERNAL_ERR
                            return False
                        primary_key_names.append(pk_key)

                    pk_source = effective_stmt.with_only_columns(
                        *primary_keys
                    ).subquery()
                    if len(primary_keys) == 1:
                        pk = primary_keys[0]
                        source_pk = cast(
                            ColumnElement[Any], pk_source.c[primary_key_names[0]]
                        )
                        delete_condition = pk.in_(sa_select(source_pk))
                    else:
                        model_pk = sa_tuple(*primary_keys)
                        source_pk_cols: list[ColumnElement[Any]] = [
                            cast(ColumnElement[Any], pk_source.c[pk_name])
                            for pk_name in primary_key_names
                        ]
                        delete_condition = model_pk.in_(sa_select(*source_pk_cols))

                    await self._ensure_nested_txn()
                    delete_stmt = sa_delete(self._model).where(delete_condition)
                    delete_result = cast(
                        CursorResult[Any], await session.execute(delete_stmt)
                    )
                    deleted_rows = delete_result.rowcount or 0
                    if deleted_rows == 0:
                        self.status = SQLStatus.NOT_FOUND
                        return False
                else:
                    result = await self.scalars(effective_stmt)
                    target = result.first()
                    if target is None:
                        self.status = SQLStatus.NOT_FOUND
                        return False
                    await self._ensure_nested_txn()
                    await session.delete(target)

            self._need_commit = True
            return True
        except SQLAlchemyError as exc:
            await self._on_sql_error(exc)
        except Exception as exc:
            self.error = exc
            self.status = SQLStatus.INTERNAL_ERR
        return False

    async def mark_for_commit(self) -> None:
        await self._ensure_nested_txn()
        self._need_commit = True

    async def commit(self) -> None:
        try:
            session = self._require_session()
            if self._nested_txn and getattr(self._nested_txn, "is_active", False):
                await self._nested_txn.commit()
            else:
                await session.commit()
            self._explicit_committed = True
            self._need_commit = False
        except Exception as exc:
            self._logger("CRUD commit failed: %s", exc)
            if self._session is not None:
                await self._session.rollback()

    async def discard(self) -> None:
        try:
            session = self._require_session()
            if self._nested_txn and getattr(self._nested_txn, "is_active", False):
                await self._nested_txn.rollback()
            else:
                await session.rollback()
        finally:
            self._need_commit = False
            self._discarded = True

    @property
    def logger(self) -> ErrorLogger:
        return self._logger

    def _log(self, error: Exception, status: SQLStatus = SQLStatus.INTERNAL_ERR) -> None:
        model_name = getattr(self._model, "__name__", str(self._model))
        self._logger(
            "CRUD[%s]: <catch: %s> <except: (%s)>",
            model_name,
            error,
            status,
        )

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self.error and not isinstance(self.error, SQLAlchemyError):
            raise self.error
        try:
            has_exc = bool(exc_type or exc_val or exc_tb)
            should_rollback = has_exc or self.error is not None or self._discarded

            if should_rollback:
                if has_exc or self.error:
                    model_name = getattr(self._model, "__name__", str(self._model))
                    self._logger(
                        "CRUD[%s]: <catch: %s> <except: (%s: %s)>",
                        model_name,
                        self.error,
                        exc_type,
                        exc_val,
                    )
                if self._nested_txn and getattr(self._nested_txn, "is_active", False):
                    try:
                        await self._nested_txn.rollback()
                    except Exception:
                        self._logger("CRUD sub-txn rollback failed", exc_info=True)
                self._need_commit = False
            elif self._need_commit and not self._explicit_committed:
                try:
                    if self._nested_txn and getattr(
                        self._nested_txn, "is_active", False
                    ):
                        await self._nested_txn.commit()
                except Exception as exc:
                    self._logger("CRUD sub-txn commit failed: %s", exc)
                    raise

            if self._session is not None:
                session = self._session
                state = get_txn_state(session)
                joined_existing = getattr(self, "_joined_existing", False)

                if state is not None and state.active:
                    state.depth -= 1
                    is_outermost = state.depth <= 0
                    if is_outermost:
                        state.active = False
                        try:
                            if should_rollback and not joined_existing:
                                await session.rollback()
                            elif (
                                self._need_commit
                                and not self._explicit_committed
                                and not joined_existing
                            ):
                                await session.commit()
                        except Exception as exc:
                            self._logger("CRUD commit/rollback failed: %s", exc)
                            try:
                                await session.rollback()
                            except Exception:
                                pass
                            raise
        finally:
            self._session = None

    async def _ensure_nested_txn(self) -> None:
        if not (self._nested_txn and self._nested_txn.is_active):
            try:
                session = self._require_session()
                self._nested_txn = await session.begin_nested()
            except Exception:
                self._nested_txn = None

    async def _merge_if_needed(
        self, session: AsyncSessionLike, instance: ModelTypeVar
    ) -> ModelTypeVar:
        insp = cast(Any, sa_inspect(instance))
        bound_sess = object_session(instance)
        session_sync = getattr(session, "sync_session", None)
        need_merge = (not insp.transient) or (
            bound_sess is not None and bound_sess is not session_sync
        )
        if need_merge:
            return await session.merge(instance)
        return instance

    def _validate_update_fields(
        self, instance: ModelTypeVar, updates: dict[str, Any]
    ) -> None:
        model_type = type(instance)
        for key in updates:
            if not hasattr(model_type, key):
                raise AttributeError(f"{model_type.__name__} has no attribute '{key}'")

    def _apply_updates(
        self, session: AsyncSessionLike, instance: ModelTypeVar, updates: dict[str, Any]
    ) -> None:
        if not updates:
            return
        self._validate_update_fields(instance, updates)
        with session.no_autoflush:
            for key, value in updates.items():
                setattr(instance, key, value)

    async def _on_sql_error(self, e: Exception) -> None:
        self.error = e
        self.status = SQLStatus.SQL_ERR
        try:
            session = self._require_session()
            if self._nested_txn and getattr(self._nested_txn, "is_active", False):
                await self._nested_txn.rollback()
            else:
                await session.rollback()
        except Exception:
            self._logger("CRUD SQL rollback failed", exc_info=True)
        self._need_commit = False
        if self.resolve_error_policy() == "raise":
            raise e

    @classmethod
    def transaction(
        cls,
        *,
        error_policy: ErrorPolicy | None = None,
        join_existing: bool = True,
        existing_txn_policy: ExistingTxnPolicy | None = None,
    ) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
        resolved_policy: ErrorPolicy = (
            error_policy if error_policy is not None else cls._default_error_policy
        )
        resolved_existing_txn_policy: ExistingTxnPolicy = (
            existing_txn_policy
            if existing_txn_policy is not None
            else cls._existing_txn_policy
        )

        def session_factory() -> AsyncSessionLike:
            provider = cls._get_session_provider()
            return provider()

        return _txn_transaction(
            session_factory,
            join_existing=join_existing,
            error_policy=resolved_policy,
            existing_txn_policy=resolved_existing_txn_policy,
        )
