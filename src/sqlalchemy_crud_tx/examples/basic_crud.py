"""Basic CRUD + transaction usage with pure SQLAlchemy."""

from __future__ import annotations

from sqlalchemy import Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from sqlalchemy_crud_tx import CRUD


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "example_user"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)


def init_db() -> tuple[Session, sessionmaker[Session]]:
    engine = create_engine("sqlite:///./crud_example.db", echo=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    session = SessionLocal()
    return session, SessionLocal


def basic_flow() -> None:
    session, SessionLocal = init_db()
    CRUD.configure(session_provider=SessionLocal, error_policy="raise")

    # Create
    with CRUD(User) as crud:
        user = crud.add(email="demo@example.com")
        print("created:", user)

    # Read
    with CRUD(User, email="demo@example.com") as crud:
        row = crud.first()
        print("fetched:", row)

    # Update
    with CRUD(User) as crud:
        updated = crud.update(row, email="updated@example.com")
        print("updated:", updated)

    # Delete by condition
    with CRUD(User, email="updated@example.com") as crud:
        crud.delete()
        print("deleted via condition")

    # Function-level transaction joins inner CRUD scopes
    @CRUD.transaction()
    def create_two() -> None:
        with CRUD(User) as crud_a:
            crud_a.add(email="a@example.com")
        with CRUD(User) as crud_b:
            crud_b.add(email="b@example.com")

    create_two()

    with CRUD(User) as crud:
        stmt = crud.select().order_by(User.email)
        emails = [u.email for u in crud.all(stmt=stmt)]
        print("after transaction:", emails)

    session.close()


if __name__ == "__main__":
    basic_flow()

