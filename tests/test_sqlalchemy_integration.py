import pathlib
import sys
from typing import Generator

import pytest
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from flask_sqlalchemy_crud import CRUD, SQLStatus  # noqa: E402

Base = declarative_base()


class SAUser(Base):  # type: ignore[misc]
    __tablename__ = "sa_user"
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False)


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

    # 创建
    with CRUD(SAUser) as crud:
        user = crud.add(email="sa@example.com")
        assert user is not None
        assert user.id is not None

    # 查询与更新
    with CRUD(SAUser, email="sa@example.com") as crud:
        found = crud.first()
        assert found is not None
        assert found.email == "sa@example.com"

        updated = crud.update(found, email="sa-updated@example.com")
        assert updated is not None
        assert updated.email == "sa-updated@example.com"

    # 删除
    with CRUD(SAUser, email="sa-updated@example.com") as crud:
        ok = crud.delete()
        assert ok is True
        assert crud.status == SQLStatus.OK

    # 确认已删除
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
        emails = {u.email for u in crud.query().all()}
        assert "join-a@example.com" in emails
        assert "join-b@example.com" in emails


def test_session_view_commit_and_rollback_redirect(sa_session: Session) -> None:
    """Session view should allow advanced operations but redirect commit/rollback to CRUD."""

    CRUD.configure(session=sa_session)

    # 测试 commit 重定向
    with CRUD(SAUser) as crud:
        crud.add(email="view-commit@example.com")
        # 通过 session 视图调用 commit，应等价于 crud.commit()
        crud.session.commit()

    # 记录应已提交（直接通过 Session 查询验证）
    assert sa_session.query(SAUser).count() == 1
    # 确保没有遗留活动事务，避免下一次 begin 冲突
    sa_session.rollback()

    # 测试 rollback 重定向到 discard：在同一事务中撤销新增
    with CRUD(SAUser) as crud:
        crud.add(email="view-rollback@example.com")
        crud.session.rollback()

    # 第二条记录应被回滚，只剩一条（直接用 Session 查询）
    emails = {u.email for u in sa_session.query(SAUser).all()}
    assert "view-commit@example.com" in emails
    assert "view-rollback@example.com" not in emails
