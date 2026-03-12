"""CRUD helper utilities and transaction integration."""

from __future__ import annotations

import logging
from collections.abc import Callable
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
from sqlalchemy.orm import Session, SessionTransaction, object_session
from sqlalchemy.orm.state import InstanceState
from sqlalchemy.sql import Select
from sqlalchemy.sql.selectable import TypedReturnsRows

from ._internal.crud_helpers import (
    apply_updates,
    build_bulk_delete_statement,
    build_instance_payload,
    build_select_statement,
    log_model_error,
    needs_merge,
    resolve_error_policy,
)
from ._internal.crud_runtime import enter_crud_scope, exit_crud_scope
from ._internal.session_proxy import SessionProxy
from .status import SQLStatus
from .transaction import (
    ErrorPolicy,
    ExistingTxnPolicy,
    get_current_error_policy,
)
from .transaction import transaction as _txn_transaction
from .types import ErrorLogger, ORMModel, SessionLike, SessionProvider

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
    SessionViewType: TypeAlias = SessionLike
else:
    SessionViewType = SessionProxy


class CRUD(Generic[ModelTypeVar]):
    """Generic CRUD wrapper.

    - Uses a context manager for commit / rollback.
    - Provides unified error state management via ``SQLStatus``.
    - Supports global and per-instance default filter conditions.
    """

    _global_filter_conditions: ClassVar[tuple[list[Any], dict[str, Any]]] = ([], {})
    _session_provider: ClassVar[tuple[SessionProvider] | None] = None
    _default_error_policy: ClassVar[ErrorPolicy] = "raise"
    _existing_txn_policy: ClassVar[ExistingTxnPolicy] = "error"
    _logger: ClassVar[ErrorLogger] = _DEFAULT_LOGGER

    @classmethod
    def register_global_filters(cls, *base_exprs: Any, **base_kwargs: Any) -> None:
        """Register global base filters applied to all models.

        Args:
            *base_exprs: Positional filter expressions passed to ``Select.where``.
            **base_kwargs: Keyword-style filters passed to ``Select.filter_by``.
        """
        cls._global_filter_conditions = (list(base_exprs) or []), (base_kwargs or {})

    def __init__(self, model: type[ModelTypeVar], **kwargs: Any) -> None:
        """Initialize a CRUD instance bound to a model.

        Args:
            model: ORM model class this CRUD instance operates on.
            **kwargs: Default filter/initialization kwargs bound to this
                instance (used by ``select()`` and ``create_instance()``).
        """
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
        self._nested_txn: SessionTransaction | None = None
        self._explicit_committed = False
        self._discarded = False
        self._session: SessionLike | None = None
        self._owns_provider_session = False

    def resolve_error_policy(self) -> ErrorPolicy:
        """Resolve the effective ``error_policy`` for this CRUD instance.

        Priority order:
        1. Error policy from the current transaction decorator context;
        2. Per-instance configuration (``config``);
        3. Class-level default configuration (``_default_error_policy``).
        """
        return resolve_error_policy(
            get_current_error_policy(),
            self._error_policy,
            self._default_error_policy,
        )

    def __enter__(self) -> Self:
        """Enter the context manager and join or create a transaction scope."""
        session = self._get_session()
        runtime_state = enter_crud_scope(
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
        session_provider: SessionProvider | None = None,
        logger: ErrorLogger | None = None,
        error_policy: ErrorPolicy | None = None,
        existing_txn_policy: ExistingTxnPolicy | None = None,
    ) -> None:
        """Configure session provider, logger and defaults.

        This is a class-level configuration and must be called before using
        ``CRUD(...)``.

        Args:
            session_provider: Callable that returns a ``SessionLike``. This is
                the preferred way to integrate with ``sessionmaker`` or custom
                session management.
            logger: Optional logger callable used by CRUD to report internal
                errors.
            error_policy: Default error policy
                (``\"raise\"`` or ``\"status_only\"``)
                applied when no transaction-scoped policy or per-instance
                override is present.
            existing_txn_policy: How to handle sessions that already have an
                active transaction (``\"error\"``, ``\"join\"``,
                ``\"savepoint\"``, ``\"adopt_autobegin\"``, ``\"reset\"``).
                See ``transaction(...)`` docstring for the detailed semantics.
        Raises:
            ValueError: If ``session_provider`` is not provided.
        """
        if session_provider is None:
            raise ValueError(
                "session_provider is required for CRUD.configure; "
                "pass a callable that returns an active Session."
            )

        cls._session_provider = (session_provider,)

        if logger is not None:
            cls._logger = logger
        if error_policy is not None:
            cls._default_error_policy = error_policy
        if existing_txn_policy is not None:
            cls._existing_txn_policy = existing_txn_policy

    @classmethod
    def _get_session_provider(cls) -> SessionProvider:
        if cls._session_provider is None:
            raise RuntimeError(
                "CRUD session is not configured. Please call "
                "CRUD.configure(session_provider=...) before using CRUD."
            )
        return cls._session_provider[0]

    def _get_session(self) -> SessionLike:
        provider = self._get_session_provider()
        return provider()

    def _require_session(self) -> SessionLike:
        if self._session is None:
            raise RuntimeError("CRUD session is not bound to current context.")
        return self._session

    @property
    def session(self) -> SessionViewType:
        """Return a controlled Session view.

        The returned object exposes most Session APIs but redirects
        ``commit``/``rollback`` to ``CRUD.commit``/``CRUD.discard``.
        It should only be used inside a ``with CRUD(...)`` context.
        """
        session = self._require_session()
        return cast(SessionViewType, SessionProxy(self, session))

    def config(
        self,
        error_policy: ErrorPolicy | None = None,
        disable_global_filter: bool | None = None,
    ) -> Self:
        """Configure behaviour for this CRUD instance.

        Args:
            error_policy: Override the effective error policy for this instance.
                When ``None``, the class-level default or transaction-scoped
                setting is used.
            disable_global_filter: When ``True``, skip any global filters
                registered via ``register_global_filters`` for this instance's
                queries. When ``False`` or ``None``, global filters remain
                enabled.
        Returns:
            The same CRUD instance, to allow fluent-style configuration.
        """
        if error_policy is not None:
            self._error_policy = error_policy
        if disable_global_filter is not None:
            self._apply_global_filters = not disable_global_filter
        return self

    def create_instance(self, **kwargs: Any) -> ModelTypeVar:
        """Create a fresh model instance from default and override kwargs.

        This method is intentionally stateless: every call returns a new,
        unattached model instance.
        """
        payload = build_instance_payload(self._kwargs, kwargs)
        return self._model(**payload)

    def add(
        self,
        instance: ModelTypeVar | None = None,
        **kwargs: Any,
    ) -> ModelTypeVar | None:
        """Insert a new record and optionally update fields.

        Behaviour:
        - When ``instance`` is ``None``, create an instance using the default
          kwargs provided when constructing the CRUD object;
        - Otherwise merge the given ``instance`` into the current Session and
          apply any additional field updates.

        Args:
            instance: Optional existing model instance to persist. If omitted,
                a new instance is created from the CRUD default kwargs.
            **kwargs: Field updates applied to the target instance before it
                is flushed.
        Returns:
            The persisted instance with any database-generated fields populated,
            or ``None`` when an error occurred and was handled according to
            the configured ``error_policy``.
        """
        try:
            session = self._require_session()
            self._ensure_nested_txn()

            if instance is None:
                target = self.create_instance(**kwargs)
            else:
                target = self._merge_if_needed(session, instance)
                apply_updates(
                    instance=target,
                    updates=kwargs,
                    no_autoflush=session.no_autoflush,
                )

            session.add(target)
            session.flush()
            self._need_commit = True
            return target
        except SQLAlchemyError as exc:
            self._on_sql_error(exc)
        except Exception as exc:
            self.error = exc
            self.status = SQLStatus.INTERNAL_ERR
        return None

    def add_many(
        self,
        instances: list[ModelTypeVar],
        **kwargs: Any,
    ) -> list[ModelTypeVar] | None:
        """Bulk-insert multiple records, applying shared field updates.

        Args:
            instances: List of model instances to be persisted.
            **kwargs: Field updates applied to each instance before flushing.
        Returns:
            A list of managed instances after flush (potentially merged) when
            successful, an empty list when ``instances`` is empty, or ``None``
            when an error occurred and was handled according to the configured
            ``error_policy``.
        """
        try:
            if not instances:
                return []

            session = self._require_session()
            self._ensure_nested_txn()

            managed_instances: list[ModelTypeVar] = []
            for instance in instances:
                target = self._merge_if_needed(session, instance)
                apply_updates(
                    instance=target,
                    updates=kwargs,
                    no_autoflush=session.no_autoflush,
                )
                managed_instances.append(target)

            session.add_all(managed_instances)
            session.flush()
            self._need_commit = True
            return managed_instances
        except SQLAlchemyError as exc:
            self._on_sql_error(exc)
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
        """Build a SQLAlchemy 2.x ``select`` statement.

        When ``entities`` is empty, this builds ``select(self._model)``.
        By default, instance-level and global base filters are applied; pass
        ``pure=True`` to skip those defaults.
        """
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

    def execute(
        self,
        statement: TypedReturnsRows[RowTypeVar],
        *args: Any,
        **kwargs: Any,
    ) -> Result[RowTypeVar]:
        """Execute a typed SQLAlchemy statement via the bound Session."""
        session = self._require_session()
        return session.execute(statement, *args, **kwargs)

    def scalars(
        self,
        statement: TypedReturnsRows[tuple[ScalarTypeVar]],
        *args: Any,
        **kwargs: Any,
    ) -> ScalarResult[ScalarTypeVar]:
        """Execute a statement and return typed scalar results."""
        session = self._require_session()
        return session.scalars(statement, *args, **kwargs)

    def scalar(
        self,
        statement: TypedReturnsRows[tuple[ScalarTypeVar]],
        *args: Any,
        **kwargs: Any,
    ) -> ScalarTypeVar | None:
        """Execute a statement and return a single typed scalar."""
        session = self._require_session()
        return session.scalar(statement, *args, **kwargs)

    def first(
        self, stmt: Select[tuple[ModelTypeVar]] | None = None
    ) -> ModelTypeVar | None:
        """Return the first model instance matched by ``stmt`` or default filters."""
        effective_stmt = stmt if stmt is not None else self.select()
        return self.scalars(effective_stmt).first()

    def all(
        self, stmt: Select[tuple[ModelTypeVar]] | None = None
    ) -> list[ModelTypeVar]:
        """Return all model instances matched by ``stmt`` or default filters."""
        effective_stmt = stmt if stmt is not None else self.select()
        return list(self.scalars(effective_stmt).all())

    def update(
        self,
        instance: ModelTypeVar | None = None,
        *,
        stmt: Select[tuple[ModelTypeVar]] | None = None,
        **kwargs: Any,
    ) -> ModelTypeVar | None:
        """Update one record by instance or by a model ``Select`` statement."""
        try:
            target_instance = (
                instance if instance is not None else self.first(stmt=stmt)
            )
            if target_instance is None:
                self.status = SQLStatus.NOT_FOUND
                return None

            session = self._require_session()
            self._ensure_nested_txn()
            target = self._merge_if_needed(session, target_instance)
            apply_updates(
                instance=target,
                updates=kwargs,
                no_autoflush=session.no_autoflush,
            )
            self._need_commit = True
            return target
        except SQLAlchemyError as exc:
            self._on_sql_error(exc)
        except Exception as exc:
            self.error = exc
            self.status = SQLStatus.INTERNAL_ERR
        return None

    def delete(
        self,
        instance: ModelTypeVar | None = None,
        *,
        stmt: Select[tuple[ModelTypeVar]] | None = None,
        all_records: bool = False,
    ) -> bool:
        """Delete one or many records by instance or a model ``Select`` statement."""
        try:
            session = self._require_session()
            if instance is not None:
                self._ensure_nested_txn()
                session.delete(instance)
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
                    self._ensure_nested_txn()
                    delete_result = cast(
                        CursorResult[Any], session.execute(delete_stmt)
                    )
                    deleted_rows = delete_result.rowcount or 0
                    if deleted_rows == 0:
                        self.status = SQLStatus.NOT_FOUND
                        return False
                else:
                    target = self.scalars(effective_stmt).first()
                    if target is None:
                        self.status = SQLStatus.NOT_FOUND
                        return False
                    self._ensure_nested_txn()
                    session.delete(target)

            self._need_commit = True
            return True
        except SQLAlchemyError as exc:
            self._on_sql_error(exc)
        except Exception as exc:
            self.error = exc
            self.status = SQLStatus.INTERNAL_ERR
        return False

    def mark_for_commit(self) -> None:
        """Mark the current context as needing commit on exit.

        This is useful when changes are made through other means (for example
        via ``crud.session``) and you still want the CRUD context manager to
        commit when leaving the ``with`` block.
        """
        self._ensure_nested_txn()
        self._need_commit = True

    def commit(self) -> None:
        """Explicitly commit the current sub-transaction or Session.

        Normally, commit is handled automatically on context manager exit; this
        method is provided for advanced scenarios where you want to take
        explicit control. It also clears the internal ``_need_commit`` flag.
        """
        try:
            session = self._require_session()
            if self._nested_txn is not None and self._nested_txn.is_active:
                self._nested_txn.commit()
            else:
                session.commit()
            self._explicit_committed = True
            self._need_commit = False
        except Exception as exc:
            self._logger("CRUD commit failed: %s", exc)
            if self._session is not None:
                self._session.rollback()

    def discard(self) -> None:
        """Explicitly roll back the current transaction and discard changes.

        - Never raises an exception; callers can continue their logic.
        - Uses the internal ``_discarded`` flag so that ``__exit__`` knows to
          roll back.
        """
        try:
            session = self._require_session()
            if self._nested_txn is not None and self._nested_txn.is_active:
                self._nested_txn.rollback()
            else:
                session.rollback()
        finally:
            self._need_commit = False
            self._discarded = True

    @property
    def logger(self) -> ErrorLogger:
        """Expose the configured error logger for integration helpers."""
        return self._logger

    def _log(self, error: Exception, status: SQLStatus = SQLStatus.INTERNAL_ERR):
        """Log an error related to the current model."""
        log_model_error(self._logger, self._model.__name__, error, status)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        # Non-SQLAlchemy exceptions are always re-raised.
        # Whether SQLAlchemyError is re-raised is controlled by ``error_policy``
        # and handled by the transaction decorator or ``_on_sql_error``.
        if self.error and not isinstance(self.error, SQLAlchemyError):
            raise self.error
        try:
            exit_crud_scope(
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

    def _close_managed_session(self, session: SessionLike) -> None:
        try:
            if isinstance(session, Session):
                session.close()
                return
            session.remove()
            return
        except Exception:
            self._logger("CRUD session close failed", exc_info=True)

    def _ensure_nested_txn(self) -> None:
        """Ensure there is an active SAVEPOINT / nested transaction if possible."""
        if not (self._nested_txn and self._nested_txn.is_active):
            try:
                session = self._require_session()
                self._nested_txn = session.begin_nested()
            except Exception:
                self._nested_txn = None

    def _merge_if_needed(
        self, session: SessionLike, instance: ModelTypeVar
    ) -> ModelTypeVar:
        """Attach an instance to the current Session when necessary."""
        insp = cast(InstanceState[ModelTypeVar], sa_inspect(instance))
        bound_sess = object_session(instance)
        if needs_merge(
            state=insp,
            bound_session=bound_sess,
            current_session=session,
        ):
            return session.merge(instance)
        return instance

    def _on_sql_error(self, e: Exception) -> None:
        """Handle a ``SQLAlchemyError`` and optionally re-raise it."""
        self.error = e
        self.status = SQLStatus.SQL_ERR
        try:
            session = self._require_session()
            if self._nested_txn is not None and self._nested_txn.is_active:
                self._nested_txn.rollback()
            else:
                session.rollback()
        except Exception:
            self._logger("CRUD SQL rollback failed", exc_info=True)
        self._need_commit = False
        # Only re-raise SQLAlchemy errors when ``error_policy == "raise"``;
        # the transaction decorator or caller will handle the exception.
        if self.resolve_error_policy() == "raise":
            raise e

    @classmethod
    def transaction(
        cls,
        *,
        error_policy: ErrorPolicy | None = None,
        join_existing: bool = True,
        existing_txn_policy: ExistingTxnPolicy | None = None,
    ) -> Callable[[Callable[P, R]], Callable[P, R]]:
        """Function-level transaction decorator.

        - One function call == one CRUD-related transaction scope.
        - Uses the generic ``transaction(...)`` helper to implement join
          semantics and commit/rollback behaviour.
        - ``existing_txn_policy`` can override how to handle an already-active
          transaction for this decorator invocation.
        """

        resolved_policy: ErrorPolicy = (
            error_policy if error_policy is not None else cls._default_error_policy
        )
        resolved_existing_txn_policy: ExistingTxnPolicy = (
            existing_txn_policy
            if existing_txn_policy is not None
            else cls._existing_txn_policy
        )

        def session_factory() -> SessionLike:
            provider = cls._get_session_provider()
            return provider()

        return _txn_transaction(
            session_factory,
            join_existing=join_existing,
            # nested=nested,
            error_policy=resolved_policy,
            existing_txn_policy=resolved_existing_txn_policy,
        )
