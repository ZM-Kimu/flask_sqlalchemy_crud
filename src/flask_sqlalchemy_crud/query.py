from __future__ import annotations

from functools import wraps
from typing import Any, Generic, Iterator, Optional, TYPE_CHECKING, cast, overload

from flask_sqlalchemy.query import Query
from sqlalchemy.engine import ScalarResult

from .types import EntityTypeVar, ModelTypeVar, ResultTypeVar

if TYPE_CHECKING:  # 仅用于类型检查，避免运行时循环依赖
    from .crud import CRUD


class CRUDQuery(Generic[ModelTypeVar, ResultTypeVar]):
    """Query 包装器。

    - 保留 SQLAlchemy 原生 Query 功能，同时增加类型提示与链式体验。
    - 通过 __getattr__ 委托未覆盖的方法，确保与既有代码兼容。
    - 终结方法（first/all/...）直接调用底层 Query。
    """

    __slots__ = ("_crud", "_query")

    def __init__(self, crud: "CRUD[ModelTypeVar]", query: Query) -> None:
        self._crud = crud
        self._query = query

    @property
    def query(self) -> Query:
        """返回底层 SQLAlchemy Query。"""
        return self._query

    def _wrap(self, query: Query) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return cast(
            "CRUDQuery[ModelTypeVar, ResultTypeVar]", CRUDQuery(self._crud, query)
        )

    def join(self, *args, **kwargs) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.join(*args, **kwargs))

    def outerjoin(self, *args, **kwargs) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.outerjoin(*args, **kwargs))

    def filter(self, *criterion) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.filter(*criterion))

    def filter_by(self, **kwargs) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.filter_by(**kwargs))

    def distinct(self, *criterion) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.distinct(*criterion))

    def options(self, *options) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.options(*options))

    @overload
    def with_entities(
        self, entity: EntityTypeVar, /
    ) -> "CRUDQuery[ModelTypeVar, EntityTypeVar]": ...

    @overload
    def with_entities(
        self, __entity: Any, __other: Any, *entities: Any
    ) -> "CRUDQuery[ModelTypeVar, tuple[Any, ...]]": ...

    def with_entities(self, *entities: Any):
        new_query = self._query.with_entities(*entities)
        wrapper: "CRUDQuery[ModelTypeVar, Any]" = CRUDQuery(self._crud, new_query)
        if len(entities) == 1:
            return cast("CRUDQuery[ModelTypeVar, EntityTypeVar]", wrapper)
        return cast("CRUDQuery[ModelTypeVar, tuple[Any, ...]]", wrapper)

    def order_by(self, *clauses) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.order_by(*clauses))

    def group_by(self, *clauses) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.group_by(*clauses))

    def having(self, *criterion) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.having(*criterion))

    def limit(self, limit: int | None) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.limit(limit))

    def offset(self, offset: int | None) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.offset(offset))

    def select_from(self, *entities) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.select_from(*entities))

    def execution_options(
        self, *args, **kwargs
    ) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.execution_options(*args, **kwargs))

    def enable_eagerloads(
        self, value: bool
    ) -> "CRUDQuery[ModelTypeVar, ResultTypeVar]":
        return self._wrap(self._query.enable_eagerloads(value))

    def all(self) -> list[ResultTypeVar]:
        return self._query.all()

    def first(self) -> ResultTypeVar | None:
        return self._query.first()

    def one(self) -> ResultTypeVar:
        return self._query.one()

    def one_or_none(self) -> ResultTypeVar | None:
        return self._query.one_or_none()

    def scalar(self) -> Optional[ResultTypeVar]:
        result = self._query.scalar()
        return cast(Optional[ResultTypeVar], result)

    def scalar_one(self) -> ResultTypeVar:
        return cast(ResultTypeVar, self._query.scalar_one())

    def scalars(self) -> ScalarResult[ResultTypeVar]:
        return cast(ScalarResult[ResultTypeVar], self._query.scalars())

    def count(self) -> int:
        return self._query.count()

    def paginate(self, *args, **kwargs):
        return self._query.paginate(*args, **kwargs)

    def raw(self) -> Query:
        return self._query

    @property
    def session(self):
        return self._query.session

    def __iter__(self) -> Iterator[ResultTypeVar]:
        return iter(self._query)

    def __getitem__(self, item):
        return self._query[item]

    def __getattr__(self, item):
        attr = getattr(self._query, item)
        if callable(attr):

            @wraps(attr)
            def wrapper(*args, **kwargs):
                result = attr(*args, **kwargs)
                if isinstance(result, Query):
                    return CRUDQuery(self._crud, result)
                return result

            return wrapper
        return attr

    def __repr__(self) -> str:
        return f"CRUDQuery({self._query!r})"
