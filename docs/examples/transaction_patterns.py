"""Advanced transaction patterns with nested CRUD scopes."""

from __future__ import annotations

from sqlalchemy import Integer, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from sqlalchemy_crud_tx import CRUD


class Base(DeclarativeBase):
    pass


class ModelA(Base):
    __tablename__ = "tx_model_a"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    age: Mapped[int] = mapped_column(Integer, nullable=False)


class ModelB(Base):
    __tablename__ = "tx_model_b"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    a_id: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)


def build_sessionmaker() -> tuple[sessionmaker[Session], Engine]:
    engine = create_engine("sqlite:///./crud_tx_patterns.db", echo=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False), engine


def dump_rows(session: Session) -> tuple[list[tuple[int, int]], list[tuple[int, int, int]]]:
    a_stmt = select(ModelA).order_by(ModelA.id)
    b_stmt = select(ModelB).order_by(ModelB.id)
    a_rows = [(row.id, row.age) for row in session.scalars(a_stmt).all()]
    b_rows = [(row.id, row.a_id, row.score) for row in session.scalars(b_stmt).all()]
    return a_rows, b_rows


def quiet_logger(*_args: object, **_kwargs: object) -> None:
    """Silence internal CRUD log lines in demo output."""
    return


def scenario_nested_with_blocks_success() -> None:
    print("\n[1] nested with-blocks (success)")
    SessionLocal, engine = build_sessionmaker()
    session = SessionLocal()
    try:
        CRUD.configure(
            session_provider=lambda: session,
            error_policy="raise",
            logger=quiet_logger,
        )

        # exactly your style: A then nested B
        with CRUD(ModelA).config(error_policy="raise") as table_a_op:
            a = table_a_op.add(age=13)
            assert a is not None
            with CRUD(ModelB).config(error_policy="raise") as table_b_op:
                table_b_op.add(a_id=a.id, score=50)

        a_rows, b_rows = dump_rows(session)
        print("A rows :", a_rows)
        print("B rows :", b_rows)
        print("expect : one A row + one B row linked by a_id")
    finally:
        session.close()
        engine.dispose()


def scenario_nested_with_blocks_full_rollback() -> None:
    print("\n[2] nested with-blocks (inner error -> full rollback)")
    SessionLocal, engine = build_sessionmaker()
    session = SessionLocal()
    try:
        CRUD.configure(
            session_provider=lambda: session,
            error_policy="raise",
            logger=quiet_logger,
        )

        try:
            with CRUD(ModelA) as table_a_op:
                a = table_a_op.add(age=18)
                assert a is not None
                with CRUD(ModelB) as table_b_op:
                    table_b_op.add(a_id=a.id, score=70)
                    raise RuntimeError("simulate downstream crash")
        except RuntimeError:
            pass

        a_rows, b_rows = dump_rows(session)
        print("A rows :", a_rows)
        print("B rows :", b_rows)
        print("expect : both empty (entire call chain rolled back)")
    finally:
        session.close()
        engine.dispose()


def scenario_cross_nested_and_mixed_discard() -> None:
    print("\n[3] cross nested decorators + mixed accept/reject")
    SessionLocal, engine = build_sessionmaker()
    session = SessionLocal()
    try:
        CRUD.configure(
            session_provider=lambda: session,
            error_policy="raise",
            logger=quiet_logger,
        )

        @CRUD.transaction(error_policy="raise")
        def write_pair(age: int, score: int, reject_b: bool = False) -> None:
            with CRUD(ModelA) as table_a_op:
                a = table_a_op.add(age=age)
                assert a is not None
                with CRUD(ModelB) as table_b_op:
                    table_b_op.add(a_id=a.id, score=score)
                    if reject_b:
                        # rollback inner B write only (via nested savepoint)
                        table_b_op.discard()

        write_pair(age=20, score=80, reject_b=False)
        write_pair(age=21, score=81, reject_b=True)

        a_rows, b_rows = dump_rows(session)
        print("A rows :", a_rows)
        print("B rows :", b_rows)
        print("expect : second A kept, second B discarded")
    finally:
        session.close()
        engine.dispose()


def scenario_chained_config_status_only() -> None:
    print("\n[4] chained config(error_policy='status_only')")
    SessionLocal, engine = build_sessionmaker()
    session = SessionLocal()
    try:
        CRUD.configure(
            session_provider=lambda: session,
            error_policy="raise",
            logger=quiet_logger,
        )

        with CRUD(ModelB).config(error_policy="status_only") as table_b_op:
            first = table_b_op.add(id=1, a_id=1, score=90)
            if first is not None:
                table_b_op.session.expunge(first)
            second = table_b_op.add(id=1, a_id=1, score=91)  # duplicate PK

            print("first add persisted?  ", first is not None)
            print("second add returned?  ", second is not None)
            print("status captured       :", table_b_op.status)
            print("error captured        :", table_b_op.error is not None)

        with CRUD(ModelB) as check_op:
            stmt = check_op.select().order_by(ModelB.id)
            rows = [(r.id, r.a_id, r.score) for r in check_op.all(stmt=stmt)]
        print("B rows after context  :", rows)
        print("expect                : [] (entire context rolled back)")
    finally:
        session.close()
        engine.dispose()


def main() -> None:
    scenario_nested_with_blocks_success()
    scenario_nested_with_blocks_full_rollback()
    scenario_cross_nested_and_mixed_discard()
    scenario_chained_config_status_only()


if __name__ == "__main__":
    main()

