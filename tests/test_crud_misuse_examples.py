"""Misuse examples for CRUD, intentionally xfailed to document bad patterns."""

from __future__ import annotations

import pathlib
import sys

import pytest
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from flask_sqlalchemy_crud import CRUD  # noqa: E402


def _init_db(create_tables: bool = True) -> tuple[Flask, SQLAlchemy, type]:
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db = SQLAlchemy(app)

    class User(db.Model):  # type: ignore[misc]
        __tablename__ = "misuse_user"
        id = db.Column(db.Integer, primary_key=True)
        email = db.Column(db.String(255), unique=True)

    if create_tables:
        with app.app_context():
            db.create_all()
    return app, db, User


@pytest.mark.xfail(strict=True, reason="未先 CRUD.configure 时 __enter__ 断言失败")
def test_without_configure_is_assert_error() -> None:
    """Using CRUD without configure should fail (documented misuse)."""
    app, _db, User = _init_db()
    with app.app_context():
        with CRUD(User) as crud:  # noqa: F841 - 故意触发
            crud.add(email="no-config@example.com")


@pytest.mark.xfail(
    strict=True, reason="未进入 app_context 时使用 db.session 会触发 WorkingOutside"
)
def test_outside_app_context_raises_runtime_error() -> None:
    """Using Flask-SQLAlchemy session without app context should raise RuntimeError."""
    app, db, User = _init_db()
    CRUD.configure(session=db.session)
    # 缺失 app_context，直接使用 CRUD 应抛出 RuntimeError
    with CRUD(User) as crud:  # noqa: F841 - 故意触发
        crud.add(email="outside@example.com")


@pytest.mark.xfail(
    strict=True, reason="表未创建即执行 CRUD，应触发底层数据库 OperationalError"
)
def test_missing_table_causes_operational_error() -> None:
    """Running CRUD against a missing table should surface OperationalError (misconfigured DB)."""
    app, db, User = _init_db(create_tables=False)
    with app.app_context():
        CRUD.configure(session=db.session, error_policy="raise")
        with CRUD(User) as crud:
            crud.add(email="table-missing@example.com")


@pytest.mark.xfail(strict=True, reason="外部已 begin，CRUD 再 begin 触发重复事务错误")
def test_manual_transaction_conflicts_with_crud() -> None:
    """External manual begin plus CRUD-managed transaction should conflict (double begin)."""
    app, db, User = _init_db()
    with app.app_context():
        CRUD.configure(session=db.session)
        txn = db.session.begin()
        try:
            with CRUD(User) as crud:  # noqa: F841 - 故意触发
                crud.add(email="conflict@example.com")
        finally:
            if txn.is_active:
                txn.rollback()
