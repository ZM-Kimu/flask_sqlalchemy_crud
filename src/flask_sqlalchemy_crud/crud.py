from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Generic, Optional, Type, cast, overload

from flask import g, has_request_context
from flask_sqlalchemy.model import Model
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import object_session
from sqlalchemy.sql import _orm_types

from .query import CRUDQuery
from .status import SQLStatus
from .types import ErrorLogger, ModelTypeVar, SessionLike


_error_logger: ErrorLogger = logging.getLogger("CRUD").error


def _get_session_for_cls(crud_cls: type["CRUD[Any]"]) -> SessionLike:
    """根据 CRUD 类获取当前配置的会话对象。

    依赖外部通过 CRUD.configure 预先设置 session。
    """
    session = getattr(crud_cls, "session", None)
    if session is None:
        raise RuntimeError(
            "CRUD session is not configured. "
            "Please call CRUD.configure(session=...) before using CRUD."
        )
    return cast(SessionLike, session)


class _TransactionScope:
    """简化 transaction 装饰器内部的事务管理。"""

    __slots__ = ("_crud_cls", "_is_request", "_sub_txn")

    def __init__(self, crud_cls: type["CRUD[Any]"], is_request: bool) -> None:
        self._crud_cls = crud_cls
        self._is_request = is_request
        self._sub_txn = None

    def __enter__(self):
        if self._is_request:
            self._crud_cls._ensure_root_txn_cls()
            return self
        try:
            session = _get_session_for_cls(self._crud_cls)
            self._sub_txn = session.begin_nested()
        except Exception:
            self._sub_txn = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self._is_request:
            try:
                session: SessionLike | None = _get_session_for_cls(self._crud_cls)
            except Exception:
                session = None
            ctx = self._crud_cls._get_request_ctx_cls(create=False)
            if exc_type is not None and ctx is not None:
                ctx["error"] = True
            if exc_type is not None and session is not None:
                try:
                    session.rollback()
                except Exception:
                    pass
            if ctx is not None:
                ctx["depth"] = max(0, ctx.get("depth", 0) - 1)
                if ctx["depth"] == 0:
                    try:
                        if session is not None:
                            if ctx.get("error"):
                                session.rollback()
                            else:
                                session.commit()
                    except Exception as exc:
                        _error_logger(f"CRUD root commit failed: {exc}")
                        if session is not None:
                            try:
                                session.rollback()
                            except Exception:
                                pass
                        ctx["error"] = True
                    finally:
                        try:
                            delattr(g, self._crud_cls._CTX_KEY)
                        except Exception:
                            pass
            return False

        success = exc_type is None
        try:
            session = _get_session_for_cls(self._crud_cls)
        except Exception:
            session = None
        try:
            if success:
                if self._sub_txn is not None and getattr(
                    self._sub_txn, "is_active", False
                ):
                    try:
                        self._sub_txn.commit()
                    except Exception:
                        pass
                if session is not None:
                    session.commit()
            else:
                if self._sub_txn is not None and getattr(
                    self._sub_txn, "is_active", False
                ):
                    try:
                        self._sub_txn.rollback()
                    except Exception:
                        pass
                if session is not None:
                    session.rollback()
        except Exception:
            if session is not None:
                try:
                    session.rollback()
                except Exception:
                    pass
            if success:
                raise
        finally:
            if session is not None:
                for name in ("close", "remove"):
                    func = getattr(session, name, None)
                    if callable(func):
                        try:
                            func()
                        except Exception:
                            pass
        return False


class CRUD(Generic[ModelTypeVar]):
    """通用 CRUD 封装。

    - 基于上下文管理器的事务提交/回滚。
    - 请求级根事务共享（同一请求内的多个 CRUD 实例共享事务）。
    - 统一错误状态管理（SQLStatus）。
    - 全局与实例级默认过滤条件。
    """

    _global_filter_conditions: tuple[list, dict] = ([], {})
    _CTX_KEY = "_crud_v3_ctx"
    session: SessionLike | None = None

    @classmethod
    def register_global_filters(cls, *base_exprs, **base_kwargs) -> None:
        """为所有模型注册全局基础过滤。"""
        cls._global_filter_conditions = (list(base_exprs) or []), (base_kwargs or {})

    def __init__(self, model: type[Model], **kwargs) -> None:
        self._txn = None
        self._model = model
        self._kwargs = kwargs

        self.instance: Model | None = None
        self._base_filter_exprs: list = list(self._global_filter_conditions[0])
        self._base_filter_kwargs: dict = dict(self._global_filter_conditions[1])
        self._instance_default_kwargs: dict = dict(kwargs)

        self.error: Exception | None = None
        self.status: SQLStatus = SQLStatus.OK

        self._need_commit = False
        self._raise_on_error = False
        self._apply_global_filters = True
        self._sub_txn = None
        self._explicit_committed = False
        self._discarded = False

    def __enter__(self) -> "CRUD[ModelTypeVar]":
        self._ensure_root_txn()
        self._explicit_committed = False
        self._discarded = False
        return self

    @classmethod
    def configure(
        cls,
        *,
        session: SessionLike | None = None,
        error_logger: ErrorLogger | None = None,
    ) -> None:
        """配置 CRUD 所依赖的会话与日志函数（类级别）。"""
        if session is not None:
            cls.session = session
        if error_logger is not None:
            global _error_logger
            _error_logger = error_logger

    def config(
        self,
        raise_on_error: bool | None = None,
        disable_global_filter: bool | None = None,
    ) -> "CRUD[ModelTypeVar]":
        if raise_on_error is not None:
            self._raise_on_error = raise_on_error
        if disable_global_filter is not None:
            self._apply_global_filters = not disable_global_filter
        return self

    def create_instance(self, no_attach: bool = False) -> ModelTypeVar:
        if no_attach:
            return cast(ModelTypeVar, self._model(**self._kwargs))
        if self.instance is None:
            self.instance = self._model(**self._kwargs)
        return cast(ModelTypeVar, self.instance)

    @overload
    def add(self, instances: None = None, **kwargs) -> Optional[ModelTypeVar]: ...

    @overload
    def add(
        self, instances: list[ModelTypeVar], **kwargs
    ) -> Optional[list[ModelTypeVar]]: ...

    @overload
    def add(self, instances: ModelTypeVar, **kwargs) -> Optional[ModelTypeVar]: ...

    def add(
        self, instances: ModelTypeVar | list[ModelTypeVar] | None = None, **kwargs
    ) -> list[ModelTypeVar] | ModelTypeVar | None:
        try:
            instances = instances or self.create_instance()
            if not isinstance(instances, list):
                instances = [instances]

            self._ensure_sub_txn()

            managed_instances: list[ModelTypeVar] = []
            for instance in instances:
                need_merge = False
                try:
                    if not (insp := sa_inspect(instance)):
                        raise ValueError()
                    bound_sess = object_session(instance)
                    need_merge = (not insp.transient) or (
                        bound_sess is not None and bound_sess is not self.session
                    )
                except Exception:
                    need_merge = True

                assert self.session is not None
                target = (
                    cast(ModelTypeVar, self.session.merge(instance))
                    if need_merge
                    else instance
                )
                if updated := self.update(target, **kwargs):
                    managed_instances.append(updated)
                else:
                    managed_instances.append(target)

            assert self.session is not None
            self.session.add_all(managed_instances)
            self._need_commit = True
            self._mark_dirty()
            return (
                managed_instances[0]
                if len(managed_instances) == 1
                else managed_instances
            )
        except SQLAlchemyError as e:
            self._on_sql_error(e)
        except Exception as e:
            self.error = e
            self.status = SQLStatus.INTERNAL_ERR
        return None

    def query(
        self, *args, pure: bool = False, **kwargs
    ) -> CRUDQuery[ModelTypeVar, ModelTypeVar]:
        query = self._model.query
        if not pure:
            if self._instance_default_kwargs:
                query = query.filter_by(**self._instance_default_kwargs)
            if self._apply_global_filters:
                if self._base_filter_exprs:
                    query = query.filter(*self._base_filter_exprs)
                if self._base_filter_kwargs:
                    query = query.filter_by(**self._base_filter_kwargs)

        final_query = query
        try:
            if args:
                final_query = final_query.filter(*args)
            if kwargs:
                final_query = final_query.filter_by(**kwargs)
        except SQLAlchemyError as e:
            self._on_sql_error(e)
            self._log(e, self.status)
        except Exception as e:
            self.error = e
            self.status = SQLStatus.INTERNAL_ERR
            self._log(e, self.status)
        return CRUDQuery(self, final_query)

    def first(
        self, query: CRUDQuery[ModelTypeVar, ModelTypeVar] | None = None
    ) -> ModelTypeVar | None:
        if query is None:
            query = self.query()
        return query.first()

    def all(
        self, query: CRUDQuery[ModelTypeVar, ModelTypeVar] | None = None
    ) -> list[ModelTypeVar]:
        if query is None:
            query = self.query()
        return query.all()

    def update(
        self, instance: ModelTypeVar | None = None, **kwargs
    ) -> ModelTypeVar | None:
        try:
            if instance is None:
                instance = self.query().first()

            if not instance:
                return None

            self._ensure_sub_txn()
            assert self.session is not None
            with self.session.no_autoflush:
                for k, v in kwargs.items():
                    setattr(instance, k, v)
            self._need_commit = True
            self._mark_dirty()
            return instance
        except SQLAlchemyError as e:
            self._on_sql_error(e)
        except Exception as e:
            self.error = e
            self.status = SQLStatus.INTERNAL_ERR
        return None

    def delete(
        self,
        instance: ModelTypeVar | None = None,
        query: CRUDQuery[ModelTypeVar, ModelTypeVar] | None = None,
        all_records: bool = False,
        sync: _orm_types.SynchronizeSessionArgument = "fetch",
    ) -> bool:
        try:
            assert self.session is not None
            if instance:
                self._ensure_sub_txn()
                self.session.delete(instance)
            else:
                if query is None:
                    query = self.query()

                first_inst = query.first()
                if not first_inst:
                    self.status = SQLStatus.NOT_FOUND
                    return False

                self._ensure_sub_txn()
                if all_records:
                    query.delete(synchronize_session=sync)
                else:
                    self.session.delete(first_inst)

            self._need_commit = True
            self._mark_dirty()
            return True
        except SQLAlchemyError as e:
            self._on_sql_error(e)
        except Exception as e:
            self.error = e
            self.status = SQLStatus.INTERNAL_ERR
        return False

    def need_commit(self) -> None:
        self._ensure_sub_txn()
        self._need_commit = True
        self._mark_dirty()

    def commit(self) -> None:
        try:
            assert self.session is not None
            if self._sub_txn and getattr(self._sub_txn, "is_active", False):
                self._sub_txn.commit()
            else:
                ctx = self._get_request_ctx(create=False)
                if not (has_request_context() and ctx and ctx.get("root_txn")):
                    self.session.commit()
            self._explicit_committed = True
            self._need_commit = False
        except Exception as e:
            _error_logger(f"CRUD commit failed: {e}")
            self.session.rollback()

    def discard(self) -> None:
        try:
            assert self.session is not None
            if self._sub_txn and getattr(self._sub_txn, "is_active", False):
                self._sub_txn.rollback()
            else:
                self.session.rollback()
        finally:
            self.error = AssertionError("User called rollback.")
            self._need_commit = False
            self._discarded = True

    def _log(self, error: Exception, status: SQLStatus = SQLStatus.INTERNAL_ERR):
        model_name = getattr(self._model, "__name__", str(self._model))
        depth = None
        try:
            ctx = self._get_request_ctx(create=False)
            depth = ctx.get("depth") if ctx else None
        except Exception:
            pass
        _error_logger(
            f"CRUD[{model_name}]: <catch: {error}> <except: ({status})> <depth: {depth}>"
        )

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.error and self._raise_on_error:
            raise self.error
        try:
            has_exc = bool(exc_type or exc_val or exc_tb)
            should_rollback = has_exc or self.error is not None or self._discarded

            assert self.session is not None
            if should_rollback:
                if has_exc or self.error:
                    model_name = getattr(self._model, "__name__", str(self._model))
                    depth = None
                    try:
                        ctx_tmp = self._get_request_ctx(create=False)
                        depth = ctx_tmp.get("depth") if ctx_tmp else None
                    except Exception:
                        pass
                    _error_logger(
                        f"CRUD[{model_name}]: <catch: {self.error}> "
                        f"<except: ({exc_type}: {exc_val})> <depth: {depth}>"
                    )
                try:
                    if self._sub_txn and getattr(self._sub_txn, "is_active", False):
                        self._sub_txn.rollback()
                    else:
                        self.session.rollback()
                except Exception:
                    pass
                self._need_commit = False
            elif self._need_commit and not self._explicit_committed:
                try:
                    if self._sub_txn and getattr(self._sub_txn, "is_active", False):
                        self._sub_txn.commit()
                    else:
                        ctx_tmp = self._get_request_ctx(create=False)
                        if ctx_tmp is None or not ctx_tmp.get("root_txn"):
                            self.session.commit()
                except Exception as e:
                    _error_logger(f"CRUD commit failed: {e}")
                    self.session.rollback()
                    if self._raise_on_error:
                        raise e

            ctx = self._get_request_ctx(create=False)
            if ctx is not None:
                ctx["depth"] = max(0, ctx.get("depth", 0) - 1)
                if ctx["depth"] == 0:
                    try:
                        if ctx.get("error"):
                            self.session.rollback()
                        else:
                            self.session.commit()
                    except Exception as e:
                        _error_logger(f"CRUD root commit failed: {e}")
                        self.session.rollback()
                        if self._raise_on_error:
                            raise e
                    finally:
                        try:
                            delattr(g, self._CTX_KEY)
                        except Exception:
                            pass
        finally:
            if not has_request_context():
                try:
                    self.session.close()
                except Exception:
                    pass
                try:
                    self.session.remove()
                except Exception:
                    pass

    def _get_request_ctx(self, create: bool = True):
        if not has_request_context():
            return None
        ctx = getattr(g, self._CTX_KEY, None)
        if ctx is None and create:
            ctx = {"root_txn": None, "depth": 0, "error": False, "dirty": False}
            setattr(g, self._CTX_KEY, ctx)
        return ctx

    def _ensure_root_txn(self) -> None:
        ctx = self._get_request_ctx(create=True)
        if ctx is None:
            return
        if ctx.get("root_txn") is None or not getattr(
            ctx.get("root_txn"), "is_active", True
        ):
            try:
                assert self.session is not None
                ctx["root_txn"] = self.session.begin()
            except Exception:
                ctx["root_txn"] = None
        ctx["depth"] = ctx.get("depth", 0) + 1

    def _ensure_sub_txn(self) -> None:
        if not (self._sub_txn and self._sub_txn.is_active):
            try:
                assert self.session is not None
                self._sub_txn = self.session.begin_nested()
            except Exception:
                self._sub_txn = None

    def _mark_dirty(self) -> None:
        ctx = self._get_request_ctx(create=False)
        if ctx is not None:
            ctx["dirty"] = True

    def _on_sql_error(self, e: Exception) -> None:
        self.error = e
        self.status = SQLStatus.SQL_ERR
        ctx = self._get_request_ctx(create=False)
        if ctx is not None:
            ctx["error"] = True
        try:
            assert self.session is not None
            if self._sub_txn and getattr(self._sub_txn, "is_active", False):
                self._sub_txn.rollback()
            else:
                self.session.rollback()
        except Exception:
            pass
        self._need_commit = False

    @classmethod
    def _get_request_ctx_cls(cls, create: bool = True):
        if not has_request_context():
            return None
        ctx = getattr(g, cls._CTX_KEY, None)
        if ctx is None and create:
            ctx = {"root_txn": None, "depth": 0, "error": False, "dirty": False}
            setattr(g, cls._CTX_KEY, ctx)
        return ctx

    @classmethod
    def _ensure_root_txn_cls(cls) -> None:
        ctx = cls._get_request_ctx_cls(create=True)
        if ctx is None:
            return
        if ctx.get("root_txn") is None or not getattr(
            ctx.get("root_txn"), "is_active", True
        ):
            try:
                session = _get_session_for_cls(cls)
                ctx["root_txn"] = session.begin()
            except Exception:
                ctx["root_txn"] = None
        ctx["depth"] = ctx.get("depth", 0) + 1

    @classmethod
    def transaction(cls):
        """函数级事务装饰器。"""

        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                scope = _TransactionScope(cls, has_request_context())
                with scope:
                    return func(*args, **kwargs)

            return wrapper

        return decorator
