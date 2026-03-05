# sqlalchemy-crud-tx

一个面向 SQLAlchemy 的轻量级 CRUD/事务辅助库：
- `with CRUD(Model) as crud:` 提供上下文式 CRUD 与子事务
- `@CRUD.transaction()` 支持 join 语义的函数级事务
- 可配置错误策略（`error_policy="raise"|"status_only"`）和日志
- SQLAlchemy 2.x 强类型查询入口（`select/execute/scalars/scalar`）

## 安装

```bash
pip install sqlalchemy-crud-tx
# 或
pip install -e .

# 安装 async 可选依赖（驱动 + 测试工具）
pip install "sqlalchemy-crud-tx[asyncio]"
```

需要 Python 3.11+ 且 `sqlalchemy>=2.0`。

## Async 命名空间（`sqlalchemy_crud_tx.asyncio`）

顶层导入仍然是同步版：
`from sqlalchemy_crud_tx import CRUD`

异步版本请从 async 命名空间导入同名 `CRUD`：
`from sqlalchemy_crud_tx.asyncio import CRUD`

```python
import asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy_crud_tx.asyncio import CRUD

engine = create_async_engine("sqlite+aiosqlite:///./async_demo.db")
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
# 假设 User 已按 SQLAlchemy 声明为模型

async def run():
    CRUD.configure(session_provider=SessionLocal, error_policy="raise")
    async with CRUD(User) as crud:
        await crud.add(email="async@example.com")

    @CRUD.transaction()
    async def write_two() -> None:
        async with CRUD(User) as c1:
            await c1.add(email="a@example.com")
        async with CRUD(User) as c2:
            await c2.add(email="b@example.com")

    await write_two()

asyncio.run(run())
```

## 快速开始（纯 SQLAlchemy）

```python
from sqlalchemy import String, Integer, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy_crud_tx import CRUD

engine = create_engine("sqlite:///./crud_example.db", echo=False)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "example_user"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)


Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)

CRUD.configure(session_provider=SessionLocal, error_policy="raise")

with CRUD(User) as crud:
    user = crud.add(email="demo@example.com")
    print("created", user)

with CRUD(User, email="demo@example.com") as crud:
    row = crud.first()
    print("fetched", row)

with CRUD(User) as crud:
    updated = crud.update(row, email="updated@example.com")
    print("updated", updated)

with CRUD(User, email="updated@example.com") as crud:
    crud.delete()
```

## SQLAlchemy 2.x 查询风格（强类型）

```python
from sqlalchemy import select, func

with CRUD(User, email="demo@example.com") as crud:
    # 基于 CRUD 默认过滤条件构建 Select
    stmt = crud.select().order_by(User.id)
    users = crud.scalars(stmt).all()  # list[User]

    # 列投影返回行对象（不是 ORM 模型实例）
    rows = crud.execute(crud.select(User.id, User.email)).all()
    first_email = rows[0].email

    # 标量辅助
    total = crud.scalar(select(func.count(User.id)))
```

## 2.0 破坏性变更

`2.0.0` 已移除旧的 Query 路径：
- 删除 `CRUD.query()`
- 删除 `CRUDQuery`
- 删除 `configure(query_builder=...)`
- 删除内置分页 `paginate(...)`

迁移对照：

| 旧写法 | 新写法 |
| --- | --- |
| `crud.query().all()` | `crud.all()` 或 `crud.scalars(crud.select()).all()` |
| `crud.query().filter(...).first()` | `crud.first(crud.select().where(...))` |
| `crud.query().with_entities(User.id, User.email).all()` | `crud.execute(crud.select(User.id, User.email)).all()` |
| `crud.query().order_by(...).paginate(...)` | 业务侧显式实现 `count + limit + offset` |

类型说明：
- 运行时常见的 `row.email` 访问通常可用。
- 静态类型硬保证以 tuple 位置为准（如 `row[0]`, `row[1]`）。

## 函数级事务示例

```python
from sqlalchemy_crud_tx import CRUD

CRUD.configure(session_provider=SessionLocal, error_policy="raise")

@CRUD.transaction(error_policy="raise")
def create_two_users():
    with CRUD(User) as crud1:
        crud1.add(email="a@example.com")
    with CRUD(User) as crud2:
        crud2.add(email="b@example.com")

create_two_users()
```

- 最外层调用负责提交/回滚；内层 `CRUD` 上下文遇到异常仅标记状态，最终由装饰器处理。
- `error_policy="status_only"` 会在回滚后吞掉 SQLAlchemyError，由调用方检查 `crud.status` / `crud.error`。

## 示例与文档

- 完整示例：`docs/examples/basic_crud.py`
- Async 示例：`docs/examples/async_basic_crud.py`
- 事务重构设计与 TODO：`docs/crud_refactor_todo.md`
- 类型增强方向：`docs/todo.md`

## 运行测试

1. 在环境变量或 `.env` 中设置可访问的数据库 URI：`TEST_DB=sqlite:///./test.db`（或其他驱动）。
2. 安装测试依赖后执行：
   ```bash
   pytest -q
   ```

## 提示

- 使用前请先调用 `CRUD.configure(session_provider=...)` 配置会话。
- 类型检查基线以 Pylance/Pyright 的 `strict`（`pyrightconfig.json`）为准。
- 如果 Session 可能已处于事务中（例如 `expire_on_commit` 触发 AUTOBEGIN），
  可通过 `CRUD.configure(existing_txn_policy=...)` 配置处理策略
  （`error`、`join`、`savepoint`、`adopt_autobegin`、`reset`）。
