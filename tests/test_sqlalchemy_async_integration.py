from __future__ import annotations

import pathlib
import sys
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import Integer, String, func, insert, text
from sqlalchemy import select as sa_select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sqlalchemy_crud_tx import CRUD as SyncCRUD
from sqlalchemy_crud_tx import SQLStatus
from sqlalchemy_crud_tx.asyncio import CRUD


class Base(DeclarativeBase):
    pass


class SAUser(Base):
    __tablename__ = "sa_async_user"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)


class TrackingAsyncSession(AsyncSession):
    closed_session_ids: set[int] = set()

    async def close(self) -> None:
        type(self).closed_session_ids.add(id(self))
        await super().close()


@pytest_asyncio.fixture(scope="function")
async def async_sa_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    session = SessionLocal()
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


def test_sync_and_async_crud_are_distinct_classes() -> None:
    assert SyncCRUD is not CRUD


@pytest.mark.asyncio
async def test_async_crud_closes_provider_owned_session() -> None:
    TrackingAsyncSession.closed_session_ids.clear()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    SessionLocal = async_sessionmaker(
        bind=engine,
        class_=TrackingAsyncSession,
        expire_on_commit=False,
    )

    created_session_ids: list[int] = []

    def provider() -> TrackingAsyncSession:
        session = SessionLocal()
        created_session_ids.append(id(session))
        return session

    CRUD.configure(session_provider=provider, error_policy="raise")

    async with CRUD(SAUser) as crud:
        created = await crud.add(email="owned-close-1@example.com")
        assert created is not None

    async with CRUD(SAUser) as crud:
        created = await crud.add(email="owned-close-2@example.com")
        assert created is not None

    assert created_session_ids
    assert set(created_session_ids).issubset(TrackingAsyncSession.closed_session_ids)

    await engine.dispose()


@pytest.mark.asyncio
async def test_async_crud_join_does_not_close_external_session() -> None:
    TrackingAsyncSession.closed_session_ids.clear()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    SessionLocal = async_sessionmaker(
        bind=engine,
        class_=TrackingAsyncSession,
        expire_on_commit=False,
    )
    external_session = SessionLocal()

    try:
        CRUD.configure(
            session_provider=lambda: external_session,
            existing_txn_policy="join",
            error_policy="raise",
        )

        outer_tx = await external_session.begin()
        try:
            async with CRUD(SAUser) as crud:
                created = await crud.add(email="join-no-close@example.com")
                assert created is not None
            assert id(external_session) not in TrackingAsyncSession.closed_session_ids
        finally:
            if outer_tx.is_active:
                await outer_tx.rollback()
    finally:
        await external_session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_async_transaction_closes_provider_owned_session() -> None:
    TrackingAsyncSession.closed_session_ids.clear()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    SessionLocal = async_sessionmaker(
        bind=engine,
        class_=TrackingAsyncSession,
        expire_on_commit=False,
    )
    created_session_ids: list[int] = []

    def provider() -> TrackingAsyncSession:
        session = SessionLocal()
        created_session_ids.append(id(session))
        return session

    CRUD.configure(session_provider=provider, error_policy="raise")

    @CRUD.transaction()
    async def noop() -> None:
        return None

    await noop()
    await noop()

    assert created_session_ids
    assert set(created_session_ids).issubset(TrackingAsyncSession.closed_session_ids)
    await engine.dispose()


@pytest.mark.asyncio
async def test_async_transaction_join_does_not_close_external_session() -> None:
    TrackingAsyncSession.closed_session_ids.clear()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    SessionLocal = async_sessionmaker(
        bind=engine,
        class_=TrackingAsyncSession,
        expire_on_commit=False,
    )
    external_session = SessionLocal()

    try:
        CRUD.configure(
            session_provider=lambda: external_session,
            existing_txn_policy="join",
            error_policy="raise",
        )

        @CRUD.transaction()
        async def noop_in_join_txn() -> None:
            return None

        outer_tx = await external_session.begin()
        try:
            await noop_in_join_txn()
            assert id(external_session) not in TrackingAsyncSession.closed_session_ids
            assert outer_tx.is_active
        finally:
            if outer_tx.is_active:
                await outer_tx.rollback()
    finally:
        await external_session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_async_crud_basic(async_sa_session: AsyncSession) -> None:
    CRUD.configure(session_provider=lambda: async_sa_session, error_policy="raise")

    async with CRUD(SAUser) as crud:
        user = await crud.add(email="sa@example.com")
        assert user is not None
        assert user.id is not None

    async with CRUD(SAUser, email="sa@example.com") as crud:
        found = await crud.first()
        assert found is not None
        assert found.email == "sa@example.com"

        updated = await crud.update(found, email="sa-updated@example.com")
        assert updated is not None
        assert updated.email == "sa-updated@example.com"

    async with CRUD(SAUser, email="sa-updated@example.com") as crud:
        ok = await crud.delete()
        assert ok is True
        assert crud.status == SQLStatus.OK

    async with CRUD(SAUser, email="sa-updated@example.com") as crud:
        assert await crud.first() is None


@pytest.mark.asyncio
async def test_async_transaction_join(async_sa_session: AsyncSession) -> None:
    CRUD.configure(session_provider=lambda: async_sa_session, error_policy="raise")

    @CRUD.transaction()
    async def create_two() -> None:
        async with CRUD(SAUser) as c1:
            await c1.add(email="join-a@example.com")
        async with CRUD(SAUser) as c2:
            await c2.add(email="join-b@example.com")

    await create_two()

    async with CRUD(SAUser) as crud:
        emails = {u.email for u in await crud.all()}
        assert "join-a@example.com" in emails
        assert "join-b@example.com" in emails


@pytest.mark.asyncio
async def test_async_session_view_commit_and_rollback_redirect(
    async_sa_session: AsyncSession,
) -> None:
    CRUD.configure(session_provider=lambda: async_sa_session, error_policy="raise")

    async with CRUD(SAUser) as crud:
        await crud.add(email="view-commit@example.com")
        await crud.session.commit()

    count = await async_sa_session.scalar(sa_select(func.count(SAUser.id)))
    assert count == 1
    await async_sa_session.rollback()

    async with CRUD(SAUser) as crud:
        await crud.add(email="view-rollback@example.com")
        await crud.session.rollback()

    rows = (await async_sa_session.scalars(sa_select(SAUser))).all()
    emails = {u.email for u in rows}
    assert "view-commit@example.com" in emails
    assert "view-rollback@example.com" not in emails


@pytest.mark.asyncio
async def test_async_existing_txn_policy_error(async_sa_session: AsyncSession) -> None:
    CRUD.configure(
        session_provider=lambda: async_sa_session,
        existing_txn_policy="error",
        error_policy="raise",
    )

    outer_tx = await async_sa_session.begin()
    try:
        with pytest.raises(InvalidRequestError):
            async with CRUD(SAUser):
                pass
    finally:
        if outer_tx.is_active:
            await outer_tx.rollback()


@pytest.mark.asyncio
async def test_async_existing_txn_policy_join(async_sa_session: AsyncSession) -> None:
    CRUD.configure(
        session_provider=lambda: async_sa_session,
        existing_txn_policy="join",
        error_policy="raise",
    )

    outer_tx = await async_sa_session.begin()
    try:
        async with CRUD(SAUser) as crud:
            created = await crud.add(email="join-policy@example.com")
            assert created is not None

        count_in_tx = await async_sa_session.scalar(sa_select(func.count(SAUser.id)))
        assert count_in_tx == 1
        assert outer_tx.is_active
    finally:
        if outer_tx.is_active:
            await outer_tx.rollback()


@pytest.mark.asyncio
async def test_async_existing_txn_policy_savepoint(async_sa_session: AsyncSession) -> None:
    CRUD.configure(
        session_provider=lambda: async_sa_session,
        existing_txn_policy="savepoint",
        error_policy="raise",
    )

    outer_tx = await async_sa_session.begin()
    try:
        with pytest.raises(RuntimeError):
            async with CRUD(SAUser) as crud:
                created = await crud.add(email="savepoint-policy@example.com")
                assert created is not None
                raise RuntimeError("boom")

        count = await async_sa_session.scalar(sa_select(func.count(SAUser.id)))
        assert count == 0
    finally:
        if outer_tx.is_active:
            await outer_tx.rollback()


@pytest.mark.asyncio
async def test_async_existing_txn_policy_adopt_autobegin(
    async_sa_session: AsyncSession,
) -> None:
    CRUD.configure(
        session_provider=lambda: async_sa_session,
        existing_txn_policy="adopt_autobegin",
        error_policy="raise",
    )

    _ = await async_sa_session.scalar(sa_select(func.count(SAUser.id)))
    assert async_sa_session.in_transaction()

    async with CRUD(SAUser) as crud:
        created = await crud.add(email="autobegin-policy@example.com")
        assert created is not None

    rows = (await async_sa_session.scalars(sa_select(SAUser))).all()
    assert [r.email for r in rows] == ["autobegin-policy@example.com"]


@pytest.mark.asyncio
async def test_async_existing_txn_policy_reset(async_sa_session: AsyncSession) -> None:
    CRUD.configure(
        session_provider=lambda: async_sa_session,
        existing_txn_policy="reset",
        error_policy="raise",
    )

    outer_tx = await async_sa_session.begin()
    try:
        async with CRUD(SAUser) as crud:
            created = await crud.add(email="reset-policy@example.com")
            assert created is not None
    finally:
        if outer_tx.is_active:
            await outer_tx.rollback()

    rows = (await async_sa_session.scalars(sa_select(SAUser))).all()
    assert [r.email for r in rows] == ["reset-policy@example.com"]


@pytest.mark.asyncio
async def test_async_status_only_captures_sql_error(
    async_sa_session: AsyncSession,
) -> None:
    CRUD.configure(session_provider=lambda: async_sa_session, error_policy="status_only")

    async with CRUD(SAUser) as crud:
        first = await crud.add(email="dup@example.com")
        assert first is not None

        second = await crud.add(email="dup@example.com")
        assert second is None
        assert crud.status == SQLStatus.SQL_ERR
        assert crud.error is not None

    count = await async_sa_session.scalar(sa_select(func.count(SAUser.id)))
    assert count == 0


@pytest.mark.asyncio
async def test_async_sqlalchemy2_style_select_execute_scalars(
    async_sa_session: AsyncSession,
) -> None:
    CRUD.configure(
        session_provider=lambda: async_sa_session,
        error_policy="raise",
        existing_txn_policy="adopt_autobegin",
    )

    async with CRUD(SAUser) as crud:
        first = await crud.add(email="new-api-a@example.com")
        second = await crud.add(email="new-api-b@example.com")
        assert first is not None
        assert second is not None

    async with CRUD(SAUser, email="new-api-a@example.com") as crud:
        stmt_default = crud.select()
        users_default = (await crud.scalars(stmt_default)).all()
        assert [u.email for u in users_default] == ["new-api-a@example.com"]

        stmt_pure = crud.select(pure=True).order_by(SAUser.email)
        users_pure = (await crud.scalars(stmt_pure)).all()
        assert [u.email for u in users_pure] == [
            "new-api-a@example.com",
            "new-api-b@example.com",
        ]

        stmt_cols = crud.select(SAUser.id, SAUser.email).order_by(SAUser.id)
        rows = (await crud.execute(stmt_cols)).all()
        assert len(rows) == 1
        assert rows[0].email == "new-api-a@example.com"
        assert isinstance(rows[0].id, int)

        count_stmt = sa_select(func.count(SAUser.id))
        scalar_val = await crud.scalar(count_stmt)
        assert scalar_val == 2


@pytest.mark.asyncio
async def test_new_api_execute_accepts_dml_and_text(
    async_sa_session: AsyncSession,
) -> None:
    """execute/scalars/scalar should accept generic Executable statements."""
    CRUD.configure(
        session_provider=lambda: async_sa_session,
        error_policy="raise",
    )

    async with CRUD(SAUser) as crud:
        insert_result = await crud.execute(
            insert(SAUser).values(email="execute-dml@example.com")
        )
        await crud.mark_for_commit()
        assert insert_result is not None

    async with CRUD(SAUser) as crud:
        text_scalars = await crud.scalars(text("select email from sa_async_user"))
        assert text_scalars.all() == ["execute-dml@example.com"]

        text_scalar = await crud.scalar(text("select count(*) from sa_async_user"))
        assert text_scalar == 1
