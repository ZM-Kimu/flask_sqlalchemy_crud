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

from sqlalchemy import delete as sa_delete
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select as sa_select
from sqlalchemy import tuple_ as sa_tuple
from sqlalchemy.engine import CursorResult, Result, ScalarResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapper, SessionTransaction, object_session
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import TypedReturnsRows

from .status import SQLStatus
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


class SessionProxy:
    """Session facade exposed to callers.

    - Delegates most methods directly to the underlying Session.
    - commit/rollback calls are redirected to CRUD.commit / CRUD.discard
      to avoid bypassing the CRUD transaction state machine.
    """

    __slots__ = ("_crud", "_session")

    def __init__(self, crud: "CRUD[Any]", session: SessionLike) -> None:
        self._crud = crud
        self._session = session

    def commit(self) -> None:
        """Redirect to CRUD.commit with a warning."""
        self._crud.logger(
            "CRUD.session.commit() is redirected to CRUD.commit(); "
            "consider calling CRUD.commit() explicitly.",
        )
        self._crud.commit()

    def rollback(self) -> None:
        """Redirect to CRUD.discard with a warning."""
        self._crud.logger(
            "CRUD.session.rollback() is redirected to CRUD.discard(); "
            "consider calling CRUD.discard() explicitly.",
        )
        self._crud.discard()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


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

    def resolve_error_policy(self) -> ErrorPolicy:
        """Resolve the effective ``error_policy`` for this CRUD instance.

        Priority order:
        1. Error policy from the current transaction decorator context;
        2. Per-instance configuration (``config``);
        3. Class-level default configuration (``_default_error_policy``).
        """
        from_ctx = get_current_error_policy()
        if from_ctx is not None:
            return from_ctx
        if self._error_policy is not None:
            return self._error_policy
        return self._default_error_policy

    def __enter__(self) -> Self:
        """Enter the context manager and join or create a transaction scope."""
        session = self._get_session()

        state = get_txn_state(session)
        joined_existing = bool(state is not None and state.active)
        in_txn = in_transaction(session)
        origin_name = get_txn_origin_name(session) if in_txn else None

        if joined_existing and not in_txn and state is not None:
            # Stale internal state; reset so policy can re-evaluate.
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
                    self._nested_txn = session.begin_nested()
                elif policy == "adopt_autobegin":
                    if origin_name != "AUTOBEGIN":
                        raise_existing_txn_error(policy=policy, origin=origin_name)
                elif policy == "reset":
                    reset_existing_txn(
                        session,
                        policy=policy,
                        origin=origin_name,
                    )
                    in_txn = False
                else:
                    raise ValueError(f"Unsupported existing_txn_policy: {policy}")

            state = activate_txn_state(session)
            if not (joined_existing or in_txn):
                begin_session(session, state)

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
        payload = dict(self._kwargs)
        payload.update(kwargs)
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
                self._apply_updates(session, target, kwargs)

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
                self._apply_updates(session, target, kwargs)
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
            self._apply_updates(session, target, kwargs)
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

                    self._ensure_nested_txn()
                    delete_stmt = sa_delete(self._model).where(delete_condition)
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
            if self._nested_txn and getattr(self._nested_txn, "is_active", False):
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
            if self._nested_txn and getattr(self._nested_txn, "is_active", False):
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
        model_name = getattr(self._model, "__name__", str(self._model))
        self._logger(
            "CRUD[%s]: <catch: %s> <except: (%s)>",
            model_name,
            error,
            status,
        )

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
                        self._nested_txn.rollback()
                    except Exception:
                        # Log and continue to top-level rollback handling.
                        self._logger("CRUD sub-txn rollback failed", exc_info=True)
                self._need_commit = False
            elif self._need_commit and not self._explicit_committed:
                try:
                    if self._nested_txn and getattr(
                        self._nested_txn, "is_active", False
                    ):
                        self._nested_txn.commit()
                except Exception as exc:
                    self._logger("CRUD sub-txn commit failed: %s", exc)
                    raise

            # Adjust depth via the shared transaction state; only the outermost
            # scope performs commit/rollback on the Session.
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
                                session.rollback()
                            elif (
                                self._need_commit
                                and not self._explicit_committed
                                and not joined_existing
                            ):
                                session.commit()
                        except Exception as exc:
                            self._logger("CRUD commit/rollback failed: %s", exc)
                            try:
                                session.rollback()
                            except Exception:
                                pass
                            raise
        finally:
            # Session lifecycle is owned by the outer application/framework.
            self._session = None

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
        insp = cast(Any, sa_inspect(instance))
        bound_sess = object_session(instance)
        need_merge = (not insp.transient) or (
            bound_sess is not None and bound_sess is not session
        )
        if need_merge:
            return session.merge(instance)
        return instance

    def _validate_update_fields(
        self, instance: ModelTypeVar, updates: dict[str, Any]
    ) -> None:
        """Fail fast on unknown attributes to avoid silent no-op writes."""
        model_type = type(instance)
        for key in updates:
            if not hasattr(model_type, key):
                raise AttributeError(f"{model_type.__name__} has no attribute '{key}'")

    def _apply_updates(
        self, session: SessionLike, instance: ModelTypeVar, updates: dict[str, Any]
    ) -> None:
        """Apply field updates under no_autoflush to avoid premature flushes."""
        if not updates:
            return
        self._validate_update_fields(instance, updates)
        with session.no_autoflush:
            for key, value in updates.items():
                setattr(instance, key, value)

    def _on_sql_error(self, e: Exception) -> None:
        """Handle a ``SQLAlchemyError`` and optionally re-raise it."""
        self.error = e
        self.status = SQLStatus.SQL_ERR
        try:
            session = self._require_session()
            if self._nested_txn and getattr(self._nested_txn, "is_active", False):
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
