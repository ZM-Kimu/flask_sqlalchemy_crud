"""类型别名与 Session 协议定义。"""

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
EntityTypeVar = TypeVar("EntityTypeVar")

ErrorLogger = Callable[..., None]


@runtime_checkable
class SessionLike(Protocol):
    """最小化约束的 Session 协议，用于静态类型检查。

    兼容 Flask‑SQLAlchemy 提供的 scoped_session / Session 等对象，
    只声明本库实际用到的方法与属性。
    """

    def begin(self) -> None:  # pragma: no cover - 类型签名
        """开始一个事务。"""

    def begin_nested(self) -> Any:  # pragma: no cover - 类型签名
        """开始一个嵌套事务（如 SAVEPOINT）。"""

    def commit(self) -> None:  # pragma: no cover - 类型签名
        """提交当前事务。"""

    def rollback(self) -> None:  # pragma: no cover - 类型签名
        """回滚当前事务。"""

    def close(self) -> None:  # pragma: no cover - 类型签名
        """关闭底层连接或会话。"""

    def remove(self) -> None:  # pragma: no cover - 类型签名
        """从 scoped_session 中移除当前会话。"""

    def add_all(self, instances: Iterable[Any]) -> None:  # pragma: no cover
        """批量添加对象到当前会话。"""

    def delete(self, instance: Any) -> None:  # pragma: no cover
        """从当前会话中删除对象。"""

    def merge(self, instance: Any) -> Any:  # pragma: no cover
        """合并一个可能游离的对象到当前会话。"""

    @property
    def no_autoflush(self) -> ContextManager[Any]:  # pragma: no cover
        """用于暂时禁用 autoflush 的上下文管理器。"""
