# sqlalchemy-crud-tx

English | [中文](README_zh.md)

A lightweight CRUD + transaction helper for SQLAlchemy:
- Context-managed CRUD with nested savepoints: `with CRUD(Model) as crud:`
- Function-level transactions via `@CRUD.transaction()` with join semantics
- Configurable error policy (`error_policy="raise"|"status_only"`) and pluggable logger
- SQLAlchemy 2.x typed query helpers (`select/execute/scalars/scalar`)

## Install

```bash
pip install sqlalchemy-crud-tx
# or local editable install
pip install -e .

# async extras (driver + async test tooling)
pip install "sqlalchemy-crud-tx[asyncio]"
```

Requires Python 3.11+ with `sqlalchemy>=2.0`.

## Async Namespace (`sqlalchemy_crud_tx.asyncio`)

The top-level import synchronous:
`from sqlalchemy_crud_tx import CRUD`

For async usage, import CRUD from the asyncio namespace:
`from sqlalchemy_crud_tx.asyncio import CRUD`

```python
import asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy_crud_tx.asyncio import CRUD

engine = create_async_engine("sqlite+aiosqlite:///./async_demo.db")
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
# assume User is a normal SQLAlchemy declarative model

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

## Quick Start (pure SQLAlchemy)

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

## SQLAlchemy 2.x Typed Queries

```python
from sqlalchemy import select, func

with CRUD(User, email="demo@example.com") as crud:
    # Build Select from CRUD defaults
    stmt = crud.select().order_by(User.id)
    users = crud.scalars(stmt).all()  # list[User]

    # Project columns -> row objects (not ORM model instances)
    rows = crud.execute(crud.select(User.id, User.email)).all()
    first_email = rows[0].email

    # Scalar helper
    total = crud.scalar(select(func.count(User.id)))
```

## 2.0 Breaking Changes

Legacy Query-based APIs were removed in `2.0.0`:
- `CRUD.query()` removed
- `CRUDQuery` removed
- `configure(query_builder=...)` removed
- Built-in `paginate(...)` removed

Migration quick map:

| Before                                                  | After                                                  |
| ------------------------------------------------------- | ------------------------------------------------------ |
| `crud.query().all()`                                    | `crud.all()` or `crud.scalars(crud.select()).all()`    |
| `crud.query().filter(...).first()`                      | `crud.first(crud.select().where(...))`                 |
| `crud.query().with_entities(User.id, User.email).all()` | `crud.execute(crud.select(User.id, User.email)).all()` |
| `crud.query().order_by(...).paginate(...)`              | explicit `count + limit + offset` in user code         |

Typing notes:
- Runtime row attribute access like `row.email` may work.
- Static hard guarantee is based on tuple-position types (`row[0]`, `row[1]`).

## Function-Level Transactions

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

- The outermost call commits or rolls back; inner CRUD contexts only mark status when exceptions occur.
- With `error_policy="status_only"`, SQLAlchemyError is rolled back and caught; check `crud.status` / `crud.error` instead.

## Docs & Examples

- Full example: `src/sqlalchemy_crud_tx/examples/basic_crud.py`
- Async example: `src/sqlalchemy_crud_tx/examples/async_basic_crud.py`
- Mixed API example: `src/sqlalchemy_crud_tx/examples/mixed_full_api.py`
- Transaction patterns: `src/sqlalchemy_crud_tx/examples/transaction_patterns.py`
- Archived transaction refactor notes: `docs/archive/crud_refactor_todo.md`
- Archived typing directions: `docs/archive/todo.md`

## Testing

1. Provide a DB URI via env or `.env`: `TEST_DB=sqlite:///./test.db` (or another driver).
2. Install test deps, then:
   ```bash
   pytest -q
   ```

## Notes

- Always call `CRUD.configure(session_provider=...)` before using CRUD instances.
- Type checking baseline is defined by Pylance/Pyright `strict` (`pyrightconfig.json`).
- If a Session may already be in a transaction (for example, AUTOBEGIN after
  `expire_on_commit`), set `existing_txn_policy` in `CRUD.configure(...)`
  to control behaviour (`error`, `join`, `savepoint`, `adopt_autobegin`, `reset`).
