import pathlib
import sys
from typing import Generator

import pytest
from sqlalchemy import Integer, String, create_engine, func
from sqlalchemy import select as sa_select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)

ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sqlalchemy_crud_tx import CRUD, SQLStatus


class Base(DeclarativeBase):
    pass


class SAUser(Base):
    __tablename__ = "sa_user"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)


@pytest.fixture(scope="function")
def sa_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:", echo=False, future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_pure_sqlalchemy_crud_basic(sa_session: Session) -> None:
    """Basic CRUD behaviour in a pure SQLAlchemy (non-Flask) setup."""
    CRUD.configure(session_provider=lambda: sa_session)

    with CRUD(SAUser) as crud:
        user = crud.add(email="sa@example.com")
        assert user is not None
        assert user.id is not None

    with CRUD(SAUser, email="sa@example.com") as crud:
        found = crud.first()
        assert found is not None
        assert found.email == "sa@example.com"

        updated = crud.update(found, email="sa-updated@example.com")
        assert updated is not None
        assert updated.email == "sa-updated@example.com"

    with CRUD(SAUser, email="sa-updated@example.com") as crud:
        ok = crud.delete()
        assert ok is True
        assert crud.status == SQLStatus.OK

    with CRUD(SAUser, email="sa-updated@example.com") as crud:
        assert crud.first() is None


def test_pure_sqlalchemy_transaction_join(sa_session: Session) -> None:
    """CRUD.transaction join semantics in a pure SQLAlchemy setup."""
    CRUD.configure(session_provider=lambda: sa_session)

    @CRUD.transaction()
    def create_two() -> None:
        with CRUD(SAUser) as c1:
            c1.add(email="join-a@example.com")
        with CRUD(SAUser) as c2:
            c2.add(email="join-b@example.com")

    create_two()

    with CRUD(SAUser) as crud:
        emails = {u.email for u in crud.all()}
        assert "join-a@example.com" in emails
        assert "join-b@example.com" in emails


def test_session_view_commit_and_rollback_redirect(sa_session: Session) -> None:
    """Session view should allow advanced operations but redirect commit/rollback."""
    CRUD.configure(session_provider=lambda: sa_session)

    with CRUD(SAUser) as crud:
        crud.add(email="view-commit@example.com")
        crud.session.commit()

    assert sa_session.query(SAUser).count() == 1
    sa_session.rollback()

    with CRUD(SAUser) as crud:
        crud.add(email="view-rollback@example.com")
        crud.session.rollback()

    emails = {u.email for u in sa_session.query(SAUser).all()}
    assert "view-commit@example.com" in emails
    assert "view-rollback@example.com" not in emails


def test_existing_txn_policy_adopt_autobegin() -> None:
    """CRUD should tolerate AUTOBEGIN transactions when policy allows adoption."""
    engine = create_engine("sqlite:///:memory:", echo=False, future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=True)
    session = SessionLocal()
    try:
        CRUD.configure(
            session_provider=lambda: session,
            existing_txn_policy="adopt_autobegin",
        )

        with CRUD(SAUser) as crud:
            user = crud.add(email="autobegin@example.com")
            assert user is not None

        _ = user.email
        assert session.in_transaction()

        with CRUD(SAUser) as crud:
            found = crud.first()
            assert found is not None
    finally:
        session.close()
        engine.dispose()


def test_add_twice_same_crud_inserts_two_rows(sa_session: Session) -> None:
    """Repeated add() on the same CRUD object should insert new rows."""
    CRUD.configure(session_provider=lambda: sa_session, error_policy="raise")

    with CRUD(SAUser) as crud:
        first = crud.add(email="multi-1@example.com")
        second = crud.add(email="multi-2@example.com")
        assert first is not None
        assert second is not None
        assert first.id != second.id
        assert first.email == "multi-1@example.com"
        assert second.email == "multi-2@example.com"

    rows = sa_session.query(SAUser).order_by(SAUser.id).all()
    assert [r.email for r in rows] == ["multi-1@example.com", "multi-2@example.com"]


def test_reuse_crud_object_across_contexts_inserts_two_rows(sa_session: Session) -> None:
    """Reusing one CRUD instance across contexts should not reuse ORM rows."""
    CRUD.configure(session_provider=lambda: sa_session, error_policy="raise")

    crud = CRUD(SAUser)
    with crud:
        first = crud.add(email="reuse-1@example.com")
        assert first is not None

    with crud:
        second = crud.add(email="reuse-2@example.com")
        assert second is not None

    assert first.id != second.id
    rows = sa_session.query(SAUser).order_by(SAUser.id).all()
    assert [r.email for r in rows] == ["reuse-1@example.com", "reuse-2@example.com"]


def test_add_merges_before_updating_detached_source_object() -> None:
    """add(instance=..., **kwargs) should update managed row, not detached source."""
    engine = create_engine("sqlite:///:memory:", echo=False, future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    source_session = SessionLocal()
    target_session = SessionLocal()

    try:
        source_obj = SAUser(email="source@example.com")
        source_session.add(source_obj)
        source_session.commit()
        source_session.expunge(source_obj)

        CRUD.configure(session_provider=lambda: target_session, error_policy="raise")
        with CRUD(SAUser) as crud:
            merged = crud.add(instance=source_obj, email="managed@example.com")
            assert merged is not None
            assert merged.id == source_obj.id
            assert merged.email == "managed@example.com"

        assert source_obj.email == "source@example.com"
        saved = target_session.query(SAUser).filter_by(id=source_obj.id).first()
        assert saved is not None
        assert saved.email == "managed@example.com"
    finally:
        source_session.close()
        target_session.close()
        engine.dispose()


def test_add_unknown_field_raises_attribute_error(sa_session: Session) -> None:
    """Unknown update fields should fail fast instead of silently attaching attrs."""
    CRUD.configure(session_provider=lambda: sa_session, error_policy="raise")

    source = SAUser(email="known@example.com")
    with pytest.raises(AttributeError):
        with CRUD(SAUser) as crud:
            crud.add(instance=source, typo_field="x")


def test_first_and_all_with_stmt(sa_session: Session) -> None:
    CRUD.configure(session_provider=lambda: sa_session, error_policy="raise")

    with CRUD(SAUser) as crud:
        for idx in range(1, 4):
            created = crud.add(email=f"stmt-{idx}@example.com")
            assert created is not None

    with CRUD(SAUser) as crud:
        stmt = crud.select().where(SAUser.email.like("stmt-%")).order_by(SAUser.id)
        first = crud.first(stmt=stmt)
        all_rows = crud.all(stmt=stmt)
        assert first is not None
        assert first.email == "stmt-1@example.com"
        assert [u.email for u in all_rows] == [
            "stmt-1@example.com",
            "stmt-2@example.com",
            "stmt-3@example.com",
        ]


def test_update_and_delete_with_stmt(sa_session: Session) -> None:
    CRUD.configure(session_provider=lambda: sa_session, error_policy="raise")

    with CRUD(SAUser) as crud:
        row = crud.add(email="stmt-target@example.com")
        assert row is not None

    with CRUD(SAUser) as crud:
        update_stmt = crud.select().where(SAUser.email == "stmt-target@example.com")
        updated = crud.update(stmt=update_stmt, email="stmt-updated@example.com")
        assert updated is not None
        assert updated.email == "stmt-updated@example.com"

        missing = crud.update(
            stmt=crud.select().where(SAUser.email == "missing@example.com"),
            email="noop@example.com",
        )
        assert missing is None
        assert crud.status == SQLStatus.NOT_FOUND

    with CRUD(SAUser) as crud:
        delete_stmt = crud.select().where(SAUser.email == "stmt-updated@example.com")
        ok = crud.delete(stmt=delete_stmt)
        assert ok is True
        assert crud.first(delete_stmt) is None


def test_delete_all_records_with_stmt(sa_session: Session) -> None:
    CRUD.configure(session_provider=lambda: sa_session, error_policy="raise")

    with CRUD(SAUser) as crud:
        crud.add(email="bulk-1@example.com")
        crud.add(email="bulk-2@example.com")
        crud.add(email="keep@example.com")

    with CRUD(SAUser) as crud:
        delete_stmt = crud.select().where(SAUser.email.like("bulk-%"))
        ok = crud.delete(stmt=delete_stmt, all_records=True)
        assert ok is True

    rows = sa_session.query(SAUser).order_by(SAUser.id).all()
    assert [r.email for r in rows] == ["keep@example.com"]


def test_sqlalchemy2_style_select_execute_scalars(sa_session: Session) -> None:
    """CRUD should expose SQLAlchemy 2.x-style select/execute/scalars helpers."""
    CRUD.configure(
        session_provider=lambda: sa_session,
        error_policy="raise",
        existing_txn_policy="adopt_autobegin",
    )

    with CRUD(SAUser) as crud:
        first = crud.add(email="new-api-a@example.com")
        second = crud.add(email="new-api-b@example.com")
        assert first is not None
        assert second is not None

    with CRUD(SAUser, email="new-api-a@example.com") as crud:
        stmt_default = crud.select()
        users_default = crud.scalars(stmt_default).all()
        assert [u.email for u in users_default] == ["new-api-a@example.com"]

        stmt_pure = crud.select(pure=True).order_by(SAUser.email)
        users_pure = crud.scalars(stmt_pure).all()
        assert [u.email for u in users_pure] == [
            "new-api-a@example.com",
            "new-api-b@example.com",
        ]

        stmt_cols = crud.select(SAUser.id, SAUser.email).order_by(SAUser.id)
        rows = crud.execute(stmt_cols).all()
        assert len(rows) == 1
        assert rows[0].email == "new-api-a@example.com"
        assert isinstance(rows[0].id, int)

        count_stmt = sa_select(func.count(SAUser.id))
        scalar_val = crud.scalar(count_stmt)
        assert scalar_val == 2


def test_legacy_query_api_removed(sa_session: Session) -> None:
    """Legacy query exports and methods should be removed in 2.0."""
    CRUD.configure(session_provider=lambda: sa_session, error_policy="raise")

    with CRUD(SAUser) as crud:
        assert hasattr(crud, "query") is False

    with pytest.raises(ImportError):
        exec("from sqlalchemy_crud_tx import CRUDQuery", {})

    with pytest.raises(ImportError):
        exec("from sqlalchemy_crud_tx import PaginationResult", {})
