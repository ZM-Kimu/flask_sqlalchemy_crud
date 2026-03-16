"""Shared pure helper functions for sync/async CRUD implementations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Any, TypeVar, cast

from sqlalchemy import delete as sa_delete
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select as sa_select
from sqlalchemy import tuple_ as sa_tuple
from sqlalchemy.orm import Mapper, Session
from sqlalchemy.orm.state import InstanceState
from sqlalchemy.sql import Select
from sqlalchemy.sql.dml import Delete
from sqlalchemy.sql.elements import ColumnElement

from ..status import SQLStatus
from ..types import ErrorLogger, ORMModel
from .transaction_common import ErrorPolicy

ModelTypeVar = TypeVar("ModelTypeVar", bound=ORMModel)


def resolve_error_policy(
    from_ctx: ErrorPolicy | None,
    instance_policy: ErrorPolicy | None,
    default_policy: ErrorPolicy,
) -> ErrorPolicy:
    """Resolve effective CRUD error policy."""
    if from_ctx is not None:
        return from_ctx
    if instance_policy is not None:
        return instance_policy
    return default_policy


def build_instance_payload(
    default_kwargs: Mapping[str, Any], overrides: Mapping[str, Any]
) -> dict[str, Any]:
    """Build instance constructor kwargs from defaults and runtime overrides."""
    payload = dict(default_kwargs)
    payload.update(overrides)
    return payload


def build_select_statement(
    *,
    model: type[ORMModel],
    entities: Sequence[Any],
    pure: bool,
    instance_default_kwargs: Mapping[str, Any],
    apply_global_filters: bool,
    base_filter_exprs: Sequence[Any],
    base_filter_kwargs: Mapping[str, Any],
    runtime_kwargs: Mapping[str, Any],
) -> Select[Any]:
    """Build a SQLAlchemy 2.x select statement with CRUD default filters."""
    statement = sa_select(*entities) if entities else sa_select(model)
    if not pure:
        if instance_default_kwargs:
            statement = statement.filter_by(**instance_default_kwargs)
        if apply_global_filters:
            if base_filter_exprs:
                statement = statement.where(*base_filter_exprs)
            if base_filter_kwargs:
                statement = statement.filter_by(**base_filter_kwargs)
    if runtime_kwargs:
        statement = statement.filter_by(**runtime_kwargs)
    return cast(Select[Any], statement)


def validate_update_fields(instance: ORMModel, updates: Mapping[str, Any]) -> None:
    """Fail fast on unknown attributes to avoid silent no-op writes."""
    model_type = type(instance)
    for key in updates:
        if not hasattr(model_type, key):
            raise AttributeError(f"{model_type.__name__} has no attribute '{key}'")


def apply_updates(
    *,
    instance: ORMModel,
    updates: Mapping[str, Any],
    no_autoflush: AbstractContextManager[object],
) -> None:
    """Apply updates under ``no_autoflush`` after validating attribute names."""
    if not updates:
        return
    validate_update_fields(instance, updates)
    with no_autoflush:
        for key, value in updates.items():
            setattr(instance, key, value)


def needs_merge(
    *,
    state: InstanceState[ModelTypeVar],
    bound_session: Session | None,
    current_session: object,
) -> bool:
    """Return True when an instance should be merged into current session."""
    return (not state.transient) or (
        bound_session is not None and bound_session is not current_session
    )


def build_bulk_delete_statement(
    model: type[ModelTypeVar], effective_stmt: Select[Any]
) -> Delete:
    """Build a bulk delete statement targeting the rows selected by stmt."""
    mapper = cast(Mapper[ModelTypeVar] | None, sa_inspect(model))
    if mapper is None:
        raise ValueError("Model mapper is not available.")

    primary_keys: list[ColumnElement[Any]] = [col for col in mapper.primary_key]
    if not primary_keys:
        raise ValueError("Model primary keys are not available.")

    primary_key_names: list[str] = []
    for pk in primary_keys:
        pk_key = pk.key
        if not isinstance(pk_key, str):
            raise ValueError("Primary key column has no valid string key.")
        primary_key_names.append(pk_key)

    pk_source = effective_stmt.with_only_columns(*primary_keys).subquery()
    if len(primary_keys) == 1:
        pk = primary_keys[0]
        source_pk = cast(ColumnElement[Any], pk_source.c[primary_key_names[0]])
        delete_condition = pk.in_(sa_select(source_pk))
    else:
        model_pk = sa_tuple(*primary_keys)
        source_pk_cols: list[ColumnElement[Any]] = [
            cast(ColumnElement[Any], pk_source.c[pk_name])
            for pk_name in primary_key_names
        ]
        delete_condition = model_pk.in_(sa_select(*source_pk_cols))

    return sa_delete(model).where(delete_condition)


def log_model_error(
    logger: ErrorLogger,
    model_name: str,
    error: Exception,
    status: SQLStatus = SQLStatus.INTERNAL_ERR,
) -> None:
    """Log a model-scoped CRUD error with consistent formatting."""
    logger(
        "CRUD[%s]: <catch: %s> <except: (%s)>",
        model_name,
        error,
        status,
    )
