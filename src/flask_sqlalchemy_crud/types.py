from __future__ import annotations

from typing import (
    Any,
    Callable,
    ContextManager,
    Iterable,
    Protocol,
    TypeVar,
    runtime_checkable,
)

from flask_sqlalchemy.model import Model


ModelTypeVar = TypeVar("ModelTypeVar", bound=Model)
ResultTypeVar = TypeVar("ResultTypeVar", covariant=True)
EntityTypeVar = TypeVar("EntityTypeVar", covariant=True)

ErrorLogger = Callable[[str], None]


@runtime_checkable
class SessionLike(Protocol):
    """最小化约束的 Session 协议，用于静态类型检查。

    兼容 Flask‑SQLAlchemy 提供的 scoped_session / Session 等对象，
    只声明本库实际用到的方法与属性。
    """

    def begin(self):  # pragma: no cover - 类型签名
        ...

    def begin_nested(self):  # pragma: no cover - 类型签名
        ...

    def commit(self) -> None:  # pragma: no cover - 类型签名
        ...

    def rollback(self) -> None:  # pragma: no cover - 类型签名
        ...

    def close(self) -> None:  # pragma: no cover - 类型签名
        ...

    def remove(self) -> None:  # pragma: no cover - 类型签名
        ...

    def add_all(self, instances: Iterable[Any]) -> None:  # pragma: no cover
        ...

    def delete(self, instance: Any) -> None:  # pragma: no cover
        ...

    def merge(self, instance: Any) -> Any:  # pragma: no cover
        ...

    @property
    def no_autoflush(self) -> ContextManager[Any]:  # pragma: no cover
        ...
