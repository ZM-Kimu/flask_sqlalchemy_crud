"""通用事务状态机与装饰器实现。"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Callable, Dict, Literal, ParamSpec, TypeAlias, TypeVar, cast

from sqlalchemy.exc import SQLAlchemyError

from .types import SessionLike

P = ParamSpec("P")
R = TypeVar("R")

ErrorPolicy = Literal["raise", "status"]

TransactionDecorator: TypeAlias = Callable[[Callable[P, R]], Callable[P, R]]

SessionFactory: TypeAlias = Callable[[], SessionLike]


class _TxnState:
    """与单个 Session 关联的事务状态。

    由通用事务状态机与 CRUD 上下文共同使用，用于维护：
    - join 深度（depth）
    - 是否存在活跃事务（active）
    """

    __slots__ = ("session", "depth", "active")

    def __init__(self, session: SessionLike) -> None:
        self.session: SessionLike = session
        self.depth: int = 0  # 当前 join 深度
        self.active: bool = False  # 是否存在活跃事务


_TxnMap: TypeAlias = Dict[int, _TxnState]

_current_txn_map: ContextVar[_TxnMap] = ContextVar("_current_txn_map")
_current_error_policy: ContextVar[ErrorPolicy | None] = ContextVar(
    "_current_error_policy"
)


def _get_txn_map() -> _TxnMap:
    """获取当前上下文下的事务状态映射。

    以 Session 对象的 id 为键，存储对应的 `_TxnState`。
    """
    try:
        return _current_txn_map.get()
    except LookupError:
        mapping: _TxnMap = {}
        _current_txn_map.set(mapping)
        return mapping


def _get_txn_state(session: SessionLike) -> _TxnState | None:
    """返回给定 Session 关联的事务状态（若存在）。"""
    return _get_txn_map().get(id(session))


def _get_or_create_txn_state(session: SessionLike) -> _TxnState:
    """获取或创建给定 Session 的事务状态。

    事务状态机基于该结构实现 join / 嵌套等语义。
    """
    mapping = _get_txn_map()
    key = id(session)
    state = mapping.get(key)
    if state is None:
        state = _TxnState(session)
        mapping[key] = state
    return state


def get_current_error_policy() -> ErrorPolicy | None:
    """返回当前上下文中的 error_policy（若由事务装饰器设置）。"""
    try:
        return _current_error_policy.get()
    except LookupError:
        return None


class _TxnContext:
    """事务上下文管理器的基础骨架。

    当前仅作为内部预留工具：
    - 通过 `SessionFactory` 获取 Session 对象；
    - 建立与该 Session 相关联的 `_TxnState`。

    提交 / 回滚等行为由 `transaction(...)` 中的事务状态机统一处理。
    """

    __slots__ = ("_session_factory", "_session", "_state")

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._session: SessionLike | None = None
        self._state: _TxnState | None = None

    @property
    def session(self) -> SessionLike:
        assert self._session is not None
        return self._session

    @property
    def state(self) -> _TxnState | None:
        return self._state

    def __enter__(self) -> "_TxnContext":
        session = self._session_factory()
        self._session = session
        self._state = _get_or_create_txn_state(session)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> Literal[False]:
        # 提交 / 回滚逻辑由通用 transaction 装饰器控制；
        # 该基础上下文本身不做任何数据库操作。
        return False


def transaction(
    session_factory: SessionFactory,
    *,
    join: bool = True,
    nested: bool | None = None,  # noqa: ARG001 - 预留参数，当前未使用
    error_policy: ErrorPolicy = "raise",
) -> TransactionDecorator[P, R]:
    """通用事务装饰器。

    - 每次函数调用对应一个“事务域”，除非根据 join 规则加入已有事务。
    - 默认遵循 join 语义：若当前上下文中已存在针对同一 Session 的事务，则加入该事务，仅由最外层调用负责最终 commit/rollback。
    - error_policy 仅对 SQLAlchemyError 生效：
        - "raise": 回滚后重新抛出数据库异常。
        - "status": 回滚后吞掉数据库异常，由调用方通过其他渠道检查状态。
        - 非数据库异常在任意策略下都会回滚并原样抛出。
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            # 获取 Session 与其事务状态
            session = session_factory()
            state = _get_txn_state(session)

            # 是否加入已有事务
            joining_existing = bool(join and state is not None and state.active)

            token = None

            try:
                # 若无活跃事务，则创建新的事务状态并开启数据库事务
                if not joining_existing:
                    state = _get_or_create_txn_state(session)
                    state.depth = 0
                    state.active = True
                    try:
                        session.begin()
                    except Exception:
                        state.active = False
                        raise
                    # 仅在最外层事务中设置 error_policy 上下文
                    token = _current_error_policy.set(error_policy)

                assert state is not None
                state.depth += 1

                captured_exc: BaseException | None = None
                result: R | None = None

                try:
                    result = func(*args, **kwargs)
                    return result  # type: ignore[return-value]
                except BaseException as exc:  # pylint: disable=broad-exception-caught
                    captured_exc = exc

                    # 仅最外层事务负责执行数据库 rollback
                    if not joining_existing:
                        try:
                            session.rollback()
                        except Exception:  # pylint: disable=broad-exception-caught
                            # 回滚失败时不屏蔽原始异常
                            pass

                    is_db_error = isinstance(exc, SQLAlchemyError)

                    # 非数据库异常：总是抛出
                    if not is_db_error:
                        raise

                    # 数据库异常：根据 error_policy 决定是否抛出
                    if error_policy == "raise":
                        raise

                    # error_policy == "status"：吞掉数据库异常，
                    # 由上层（例如 CRUD）通过其他途径记录错误状态。
                    return cast(R, None)
                finally:
                    # 只有在状态仍然活跃时才调整深度
                    if state.active:
                        state.depth -= 1
                        if state.depth <= 0:
                            state.active = False
                            # 最外层且无异常时提交
                            if captured_exc is None and not joining_existing:
                                try:
                                    session.commit()
                                except Exception as commit_exc:  # pylint: disable=broad-exception-caught
                                    # 提交失败时尝试回滚并抛出提交异常
                                    try:
                                        session.rollback()
                                    except Exception:  # pragma: no cover - 防御性回滚
                                        pass
                                    raise commit_exc
            finally:
                if token is not None:
                    _current_error_policy.reset(token)

        return wrapper

    return decorator
