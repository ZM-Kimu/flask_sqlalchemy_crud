"""Basic async CRUD + transaction usage with pure SQLAlchemy."""

from __future__ import annotations

import asyncio

from sqlalchemy import Integer, String
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlalchemy_crud_tx.asyncio import CRUD


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "example_async_user"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)


async def init_db() -> tuple[
    AsyncSession,
    AsyncEngine,
]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///./crud_async_example.db", echo=False
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    SessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    session = SessionLocal()
    return session, engine


async def basic_flow() -> None:
    session, engine = await init_db()
    CRUD.configure(
        session_provider=lambda: session,
        error_policy="raise",
        existing_txn_policy="adopt_autobegin",
    )

    try:
        async with CRUD(User) as crud:
            user = await crud.add(email="demo@example.com")
            print("created:", user)

        async with CRUD(User, email="demo@example.com") as crud:
            row = await crud.first()
            print("fetched:", row)

        async with CRUD(User) as crud:
            updated = await crud.update(row, email="updated@example.com")
            print("updated:", updated)

        async with CRUD(User, email="updated@example.com") as crud:
            await crud.delete()
            print("deleted via condition")

        @CRUD.transaction()
        async def create_two() -> None:
            async with CRUD(User) as crud_a:
                await crud_a.add(email="a@example.com")
            async with CRUD(User) as crud_b:
                await crud_b.add(email="b@example.com")

        await create_two()

        async with CRUD(User) as crud:
            stmt = crud.select().order_by(User.email)
            emails = [u.email for u in await crud.all(stmt=stmt)]
            print("after transaction:", emails)

        rows = (await session.scalars(sa_select(User).order_by(User.id))).all()
        print("raw check:", [u.email for u in rows])
    finally:
        await session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(basic_flow())
