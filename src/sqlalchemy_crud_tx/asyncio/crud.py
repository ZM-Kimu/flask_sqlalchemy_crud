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

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import CursorResult, Result, ScalarResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction
from sqlalchemy.orm import Session, object_session
from sqlalchemy.orm.state import InstanceState
from sqlalchemy.sql import Select
from sqlalchemy.sql.selectable import TypedReturnsRows

from .._internal.crud_helpers import (
    apply_updates,
    build_bulk_delete_statement,
    build_instance_payload,
    build_select_statement,
    log_model_error,
    needs_merge,
    resolve_error_policy,
)
from ..status import SQLStatus
from ..types import AsyncSessionLike, AsyncSessionProvider, ErrorLogger, ORMModel
from ._internal.crud_runtime import enter_crud_scope, exit_crud_scope
from ._internal.session_proxy import AsyncSessionProxy
from .transaction import (
    ErrorPolicy,
    ExistingTxnPolicy,
    get_current_error_policy,
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
        self._owns_provider_session = False

    def resolve_error_policy(self) -> ErrorPolicy:
        return resolve_error_policy(
            get_current_error_policy(),
            self._error_policy,
            self._default_error_policy,
        )

    async def __aenter__(self) -> Self:
        session = self._get_session()
        runtime_state = await enter_crud_scope(
            session=session,
            existing_txn_policy=type(self)._existing_txn_policy,
        )

        self._session = session
        self._joined_existing = runtime_state.joined_existing
        self._explicit_committed = False
        self._discarded = False
        self._nested_txn = runtime_state.nested_txn
        self._owns_provider_session = runtime_state.owns_provider_session
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
        payload = build_instance_payload(self._kwargs, kwargs)
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
                apply_updates(
                    instance=target,
                    updates=kwargs,
                    no_autoflush=session.no_autoflush,
                )

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
                apply_updates(
                    instance=target,
                    updates=kwargs,
                    no_autoflush=session.no_autoflush,
                )
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
        return build_select_statement(
            model=self._model,
            entities=entities,
            pure=pure,
            instance_default_kwargs=self._instance_default_kwargs,
            apply_global_filters=self._apply_global_filters,
            base_filter_exprs=self._base_filter_exprs,
            base_filter_kwargs=self._base_filter_kwargs,
            runtime_kwargs=kwargs,
        )

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
            apply_updates(
                instance=target,
                updates=kwargs,
                no_autoflush=session.no_autoflush,
            )
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
                    try:
                        delete_stmt = build_bulk_delete_statement(
                            self._model, effective_stmt
                        )
                    except ValueError:
                        self.status = SQLStatus.INTERNAL_ERR
                        return False
                    await self._ensure_nested_txn()
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
            if self._nested_txn is not None and self._nested_txn.is_active:
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
            if self._nested_txn is not None and self._nested_txn.is_active:
                await self._nested_txn.rollback()
            else:
                await session.rollback()
        finally:
            self._need_commit = False
            self._discarded = True

    @property
    def logger(self) -> ErrorLogger:
        return self._logger

    def _log(
        self, error: Exception, status: SQLStatus = SQLStatus.INTERNAL_ERR
    ) -> None:
        log_model_error(self._logger, self._model.__name__, error, status)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self.error and not isinstance(self.error, SQLAlchemyError):
            raise self.error
        try:
            await exit_crud_scope(
                model_name=self._model.__name__,
                logger=self._logger,
                session=self._session,
                nested_txn=self._nested_txn,
                need_commit=self._need_commit,
                explicit_committed=self._explicit_committed,
                joined_existing=self._joined_existing,
                owns_provider_session=self._owns_provider_session,
                discarded=self._discarded,
                error=self.error,
                exc_type=exc_type,
                exc_val=exc_val,
                exc_tb=exc_tb,
                close_session=self._close_managed_session,
            )
        finally:
            self._owns_provider_session = False
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
        insp = cast(InstanceState[ModelTypeVar], sa_inspect(instance))
        bound_sess = object_session(instance)
        session_sync = self._resolve_sync_session(session)
        if needs_merge(
            state=insp,
            bound_session=bound_sess,
            current_session=session_sync,
        ):
            return await session.merge(instance)
        return instance

    def _resolve_sync_session(self, session: AsyncSessionLike) -> Session:
        if isinstance(session, AsyncSession):
            return session.sync_session
        return session().sync_session

    async def _on_sql_error(self, e: Exception) -> None:
        self.error = e
        self.status = SQLStatus.SQL_ERR
        try:
            session = self._require_session()
            if self._nested_txn is not None and self._nested_txn.is_active:
                await self._nested_txn.rollback()
            else:
                await session.rollback()
        except Exception:
            self._logger("CRUD SQL rollback failed", exc_info=True)
        self._need_commit = False
        if self.resolve_error_policy() == "raise":
            raise e

    async def _close_managed_session(self, session: AsyncSessionLike) -> None:
        try:
            if isinstance(session, AsyncSession):
                await session.close()
                return
            await session.remove()
            return
        except Exception:
            self._logger("CRUD session close failed", exc_info=True)

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
