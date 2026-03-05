"""Full mixed CRUD example that touches all public CRUD methods.

This script is intended for manual runtime checks and IDE type-hint inspection.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, create_engine, func, select as sa_select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from sqlalchemy_crud_tx import CRUD


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "full_api_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    age: Mapped[int] = mapped_column(Integer, nullable=False)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Score(Base):
    __tablename__ = "full_api_score"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)
    tenant_id: Mapped[int] = mapped_column(Integer, nullable=False)


def init_db() -> tuple[Session, Engine]:
    engine = create_engine("sqlite:///./crud_full_api_demo.db", echo=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    return session_factory(), engine


def demo_full_api() -> None:
    session, engine = init_db()

    # configure + register_global_filters
    CRUD.configure(
        session_provider=lambda: session,
        error_policy="raise",
        existing_txn_policy="join",
    )
    CRUD.register_global_filters(tenant_id=1)

    try:
        # config + create_instance + add_many + add
        with CRUD(User, tenant_id=1).config(error_policy="raise") as users:
            seed_a = users.create_instance(
                email="alpha@example.com", age=20, is_deleted=False
            )
            seed_b = users.create_instance(
                email="beta@example.com", age=21, is_deleted=False
            )
            users.add_many([seed_a, seed_b])
            users.add(email="gamma@example.com", age=22, is_deleted=False)

        # add a different tenant row (hidden by global filter in default select)
        with CRUD(User) as users:
            users.add(
                email="tenant2@example.com",
                age=90,
                tenant_id=2,
                is_deleted=False,
            )

        # select + execute (projection)
        # Hover `projected` and `rows` in IDE to inspect tuple-row inference.
        with CRUD(User) as users:
            projected = users.select(User.id, User.email).order_by(User.id)
            rows = users.execute(projected).all()
            print("projected rows:", [(row[0], row[1]) for row in rows])

        # select + scalars + pure
        with CRUD(User) as users:
            tenant1_users = list(users.scalars(users.select().order_by(User.id)).all())
            all_tenants = list(
                users.scalars(users.select(pure=True).order_by(User.id)).all()
            )
            print("tenant1 users:", [u.email for u in tenant1_users])
            print("all tenants :", [u.email for u in all_tenants])

        # scalar (aggregate)
        with CRUD(User) as users:
            tenant1_count = users.scalar(
                sa_select(func.count(User.id)).where(User.tenant_id == 1)
            )
            print("tenant1 count:", tenant1_count)

        # first + all
        with CRUD(User) as users:
            first_user = users.first()
            selected_users = users.all(
                stmt=users.select().where(User.age >= 21).order_by(User.age)
            )
            print("first user:", None if first_user is None else first_user.email)
            print("selected users:", [u.email for u in selected_users])

        # update by stmt
        with CRUD(User) as users:
            users.update(
                stmt=users.select().where(User.email == "alpha@example.com"),
                age=30,
            )

        # delete by instance
        with CRUD(User) as users:
            victim = users.first(users.select().where(User.email == "beta@example.com"))
            if victim is not None:
                users.delete(instance=victim)

        # delete by stmt + all_records
        with CRUD(User) as users:
            users.delete(stmt=users.select().where(User.age >= 30), all_records=True)

        # session proxy + mark_for_commit + explicit commit
        with CRUD(Score, tenant_id=1) as scores:
            scores.session.add(Score(user_id=1, value=88, tenant_id=1))
            scores.mark_for_commit()
            scores.commit()

        # discard
        with CRUD(Score, tenant_id=1) as scores:
            scores.add(user_id=1, value=99)
            scores.discard()

        # transaction decorator with mixed nested CRUD
        @CRUD.transaction(error_policy="raise")
        def write_pair(age: int, score: int) -> None:
            with CRUD(User, tenant_id=1) as users:
                user = users.add(email=f"tx-{age}@example.com", age=age, is_deleted=False)
                assert user is not None
                with CRUD(Score, tenant_id=1) as scores:
                    scores.add(user_id=user.id, value=score)

        write_pair(age=40, score=77)

        with CRUD(User) as users:
            final_users = users.all(stmt=users.select(pure=True).order_by(User.id))
            print("final users:", [(u.id, u.email, u.age, u.tenant_id) for u in final_users])

        with CRUD(Score) as scores:
            final_scores = scores.all(stmt=scores.select(pure=True).order_by(Score.id))
            print(
                "final scores:",
                [(s.id, s.user_id, s.value, s.tenant_id) for s in final_scores],
            )
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    demo_full_api()
