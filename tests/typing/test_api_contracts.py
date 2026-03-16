from __future__ import annotations

import pathlib
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, assert_type

from sqlalchemy import Integer, String, func, insert, text
from sqlalchemy.engine import Result, ScalarResult
from sqlalchemy import select as sa_select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import Select

ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sqlalchemy_crud_tx import CRUD


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "typing_contract_user"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)


if TYPE_CHECKING:
    crud = CRUD(User)

    stmt_users = crud.select()
    assert_type(stmt_users, Select[tuple[User]])

    users = crud.scalars(stmt_users).all()
    assert_type(users, Sequence[User])

    count_stmt = sa_select(func.count(User.id))
    count_value = crud.scalar(count_stmt)
    assert_type(count_value, int | None)

    insert_stmt = insert(User).values(email="typed@example.com")
    insert_result = crud.execute(insert_stmt)
    assert_type(insert_result, Result[Any])

    text_scalars = crud.scalars(text("select 1"))
    assert_type(text_scalars, ScalarResult[Any])

    text_scalar = crud.scalar(text("select 1"))
    assert_type(text_scalar, Any)
