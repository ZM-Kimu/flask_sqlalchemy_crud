import os
import pathlib
from typing import Generator, Tuple, Type

import pytest
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]


def _load_test_db_uri() -> str | None:
    """Load TEST_DB URI from environment or .env file and normalize driver."""
    uri = os.getenv("TEST_DB")
    if uri is None:
        env_path = ROOT_DIR / ".env"
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("TEST_DB="):
                    _, value = line.split("=", 1)
                    uri = value.strip()
                    os.environ.setdefault("TEST_DB", uri)
                    break
    if not uri:
        return None
    # 如果是 mysql://，优先使用 PyMySQL 驱动
    if uri.startswith("mysql://"):
        uri = "mysql+pymysql://" + uri[len("mysql://") :]
    return uri


@pytest.fixture(scope="session")
def app_and_db() -> Generator[Tuple[Flask, SQLAlchemy, type], None, None]:
    """Create a Flask app + SQLAlchemy db bound to TEST_DB."""
    uri = _load_test_db_uri()
    if not uri:
        pytest.skip("TEST_DB is not configured in environment or .env")

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = uri
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db = SQLAlchemy(app)

    class TestUser(db.Model):  # type: ignore[valid-type]
        __tablename__ = "crud_test_user"
        id = db.Column(db.Integer, primary_key=True)
        email = db.Column(db.String(255), unique=True, nullable=False)

    with app.app_context():
        try:
            db.create_all()
        except Exception as exc:  # pragma: no cover - 环境依赖外部数据库
            pytest.skip(f"Cannot connect to TEST_DB: {exc}")

    try:
        yield app, db, TestUser
    finally:
        with app.app_context():
            try:
                TestUser.__table__.drop(bind=db.engine)  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - 防御性清理
                pass
