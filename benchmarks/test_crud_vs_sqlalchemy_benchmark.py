from __future__ import annotations

import os
from collections.abc import Callable

import pytest
from sqlalchemy import func
from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session

pytest.importorskip("pytest_benchmark")

RUN_BENCHMARKS = os.getenv("RUN_BENCHMARKS") == "1"
pytestmark = pytest.mark.skipif(
    not RUN_BENCHMARKS,
    reason="Set RUN_BENCHMARKS=1 to run benchmark cases.",
)

from sqlalchemy_crud_tx import CRUD

BATCH_SIZE = 50
PAGE_SIZE = 20


@pytest.mark.benchmark(group="add_flush")
def test_sa_add_flush(
    benchmark,
    sa_session: Session,
    email_factory: Callable[[str], str],
    bench_user_model,
):
    def run() -> None:
        row = bench_user_model(email=email_factory("sa-add"))
        sa_session.add(row)
        sa_session.flush()
        sa_session.rollback()

    benchmark(run)


@pytest.mark.benchmark(group="add_flush")
def test_crud_add_flush(
    benchmark,
    configured_crud: None,
    email_factory: Callable[[str], str],
    bench_user_model,
):
    def run() -> None:
        with CRUD(bench_user_model) as crud:
            row = crud.add(email=email_factory("crud-add"))
            if row is None:
                raise RuntimeError("CRUD add returned None")
            crud.discard()

    benchmark(run)


@pytest.mark.benchmark(group="get_by_pk")
def test_sa_get_by_pk(
    benchmark,
    sa_session: Session,
    seeded_user_id: str,
    bench_user_model,
):
    def run() -> None:
        row = sa_session.query(bench_user_model).filter_by(id=seeded_user_id).first()
        if row is None:
            raise RuntimeError("Seed row missing")
        sa_session.rollback()

    benchmark(run)


@pytest.mark.benchmark(group="get_by_pk")
def test_crud_get_by_pk(
    benchmark,
    configured_crud: None,
    seeded_user_id: str,
    bench_user_model,
):
    def run() -> None:
        with CRUD(bench_user_model, id=seeded_user_id) as crud:
            row = crud.first()
            if row is None:
                raise RuntimeError("Seed row missing")
            crud.discard()

    benchmark(run)


@pytest.mark.benchmark(group="get_by_email")
def test_sa_get_by_email(
    benchmark,
    sa_session: Session,
    seeded_user_email: str,
    bench_user_model,
):
    def run() -> None:
        row = sa_session.query(bench_user_model).filter_by(email=seeded_user_email).first()
        if row is None:
            raise RuntimeError("Seed row missing")
        sa_session.rollback()

    benchmark(run)


@pytest.mark.benchmark(group="get_by_email")
def test_crud_get_by_email(
    benchmark,
    configured_crud: None,
    seeded_user_email: str,
    bench_user_model,
):
    def run() -> None:
        with CRUD(bench_user_model) as crud:
            stmt = crud.select(email=seeded_user_email)
            row = crud.first(stmt=stmt)
            if row is None:
                raise RuntimeError("Seed row missing")
            crud.discard()

    benchmark(run)


@pytest.mark.benchmark(group="count_rows")
def test_sa_count_rows(
    benchmark,
    sa_session: Session,
    seeded_many_users: list[str],
    bench_user_model,
):
    expected = len(seeded_many_users)

    def run() -> None:
        total = sa_session.query(bench_user_model).count()
        if total != expected:
            raise RuntimeError(f"Expected {expected} rows, got {total}")
        sa_session.rollback()

    benchmark(run)


@pytest.mark.benchmark(group="count_rows")
def test_crud_count_rows(
    benchmark,
    configured_crud: None,
    seeded_many_users: list[str],
    bench_user_model,
):
    expected = len(seeded_many_users)

    def run() -> None:
        with CRUD(bench_user_model) as crud:
            count_stmt = sa_select(func.count(bench_user_model.id))
            total = crud.scalar(count_stmt)
            if total != expected:
                raise RuntimeError(f"Expected {expected} rows, got {total}")
            crud.discard()

    benchmark(run)


@pytest.mark.benchmark(group="all_rows")
def test_sa_all_rows(
    benchmark,
    sa_session: Session,
    seeded_many_users: list[str],
    bench_user_model,
):
    expected = len(seeded_many_users)

    def run() -> None:
        rows = sa_session.query(bench_user_model).all()
        if len(rows) != expected:
            raise RuntimeError(f"Expected {expected} rows, got {len(rows)}")
        sa_session.rollback()

    benchmark(run)


@pytest.mark.benchmark(group="all_rows")
def test_crud_all_rows(
    benchmark,
    configured_crud: None,
    seeded_many_users: list[str],
    bench_user_model,
):
    expected = len(seeded_many_users)

    def run() -> None:
        with CRUD(bench_user_model) as crud:
            rows = crud.all()
            if len(rows) != expected:
                raise RuntimeError(f"Expected {expected} rows, got {len(rows)}")
            crud.discard()

    benchmark(run)


@pytest.mark.benchmark(group="paginate_page1")
def test_sa_paginate_page1(
    benchmark,
    sa_session: Session,
    seeded_many_users: list[str],
    bench_user_model,
):
    expected = len(seeded_many_users)
    expected_first_page = min(expected, PAGE_SIZE)

    def run() -> None:
        total = sa_session.scalar(sa_select(func.count(bench_user_model.id)))
        items = sa_session.scalars(
            sa_select(bench_user_model)
            .order_by(bench_user_model.email)
            .limit(PAGE_SIZE)
            .offset(0)
        )
        if total != expected:
            raise RuntimeError("Unexpected total")
        if len(items.all()) != expected_first_page:
            raise RuntimeError("Unexpected page size")
        sa_session.rollback()

    benchmark(run)


@pytest.mark.benchmark(group="paginate_page1")
def test_crud_paginate_page1(
    benchmark,
    configured_crud: None,
    seeded_many_users: list[str],
    bench_user_model,
):
    expected = len(seeded_many_users)
    expected_first_page = min(expected, PAGE_SIZE)

    def run() -> None:
        with CRUD(bench_user_model) as crud:
            total = crud.scalar(sa_select(func.count(bench_user_model.id)))
            items_stmt = crud.select().order_by(bench_user_model.email).limit(PAGE_SIZE)
            items = crud.scalars(items_stmt).all()
            if total != expected:
                raise RuntimeError("Unexpected total")
            if len(items) != expected_first_page:
                raise RuntimeError("Unexpected page size")
            crud.discard()

    benchmark(run)


@pytest.mark.benchmark(group="add_instance_flush")
def test_sa_add_instance_flush(
    benchmark,
    sa_session: Session,
    email_factory: Callable[[str], str],
    bench_user_model,
):
    def run() -> None:
        row = bench_user_model(email=email_factory("sa-add-inst"))
        sa_session.add(row)
        sa_session.flush()
        sa_session.rollback()

    benchmark(run)


@pytest.mark.benchmark(group="add_instance_flush")
def test_crud_add_instance_flush(
    benchmark,
    configured_crud: None,
    email_factory: Callable[[str], str],
    bench_user_model,
):
    def run() -> None:
        row = bench_user_model(email=email_factory("crud-add-inst"))
        with CRUD(bench_user_model) as crud:
            inserted = crud.add(instance=row)
            if inserted is None:
                raise RuntimeError("CRUD add(instance=...) returned None")
            crud.discard()

    benchmark(run)


@pytest.mark.benchmark(group="update_flush")
def test_sa_update_flush(
    benchmark,
    sa_session: Session,
    seeded_user_id: str,
    email_factory: Callable[[str], str],
    bench_user_model,
):
    def run() -> None:
        row = sa_session.query(bench_user_model).filter_by(id=seeded_user_id).first()
        if row is None:
            raise RuntimeError("Seed row missing")
        row.email = email_factory("sa-update")
        sa_session.flush()
        sa_session.rollback()

    benchmark(run)


@pytest.mark.benchmark(group="update_flush")
def test_crud_update_flush(
    benchmark,
    configured_crud: None,
    seeded_user_id: str,
    email_factory: Callable[[str], str],
    bench_user_model,
):
    def run() -> None:
        with CRUD(bench_user_model, id=seeded_user_id) as crud:
            row = crud.first()
            if row is None:
                raise RuntimeError("Seed row missing")
            updated = crud.update(row, email=email_factory("crud-update"))
            if updated is None:
                raise RuntimeError("CRUD update returned None")
            # Keep SQL effects comparable to SA baseline (explicit UPDATE flush).
            crud.session.flush()
            crud.discard()

    benchmark(run)


@pytest.mark.benchmark(group="update_first_flush")
def test_sa_update_first_flush(
    benchmark,
    sa_session: Session,
    seeded_many_users: list[str],
    email_factory: Callable[[str], str],
    bench_user_model,
):
    if not seeded_many_users:
        raise RuntimeError("Seed dataset missing")

    def run() -> None:
        row = sa_session.query(bench_user_model).first()
        if row is None:
            raise RuntimeError("Seed row missing")
        row.email = email_factory("sa-update-first")
        sa_session.flush()
        sa_session.rollback()

    benchmark(run)


@pytest.mark.benchmark(group="update_first_flush")
def test_crud_update_first_flush(
    benchmark,
    configured_crud: None,
    seeded_many_users: list[str],
    email_factory: Callable[[str], str],
    bench_user_model,
):
    if not seeded_many_users:
        raise RuntimeError("Seed dataset missing")

    def run() -> None:
        with CRUD(bench_user_model) as crud:
            updated = crud.update(email=email_factory("crud-update-first"))
            if updated is None:
                raise RuntimeError("CRUD update(instance=None) returned None")
            crud.session.flush()
            crud.discard()

    benchmark(run)


@pytest.mark.benchmark(group="delete_flush")
def test_sa_delete_flush(
    benchmark,
    sa_session: Session,
    seeded_user_id: str,
    bench_user_model,
):
    def run() -> None:
        row = sa_session.query(bench_user_model).filter_by(id=seeded_user_id).first()
        if row is None:
            raise RuntimeError("Seed row missing")
        sa_session.delete(row)
        sa_session.flush()
        sa_session.rollback()

    benchmark(run)


@pytest.mark.benchmark(group="delete_flush")
def test_crud_delete_flush(
    benchmark,
    configured_crud: None,
    seeded_user_id: str,
    bench_user_model,
):
    def run() -> None:
        with CRUD(bench_user_model, id=seeded_user_id) as crud:
            ok = crud.delete()
            if not ok:
                raise RuntimeError("CRUD delete returned False")
            # Keep SQL effects comparable to SA baseline (explicit DELETE flush).
            crud.session.flush()
            crud.discard()

    benchmark(run)


@pytest.mark.benchmark(group="delete_by_instance_flush")
def test_sa_delete_by_instance_flush(
    benchmark,
    sa_session: Session,
    seeded_many_users: list[str],
    bench_user_model,
):
    if not seeded_many_users:
        raise RuntimeError("Seed dataset missing")

    def run() -> None:
        row = sa_session.query(bench_user_model).first()
        if row is None:
            raise RuntimeError("Seed row missing")
        sa_session.delete(row)
        sa_session.flush()
        sa_session.rollback()

    benchmark(run)


@pytest.mark.benchmark(group="delete_by_instance_flush")
def test_crud_delete_by_instance_flush(
    benchmark,
    configured_crud: None,
    seeded_many_users: list[str],
    bench_user_model,
):
    if not seeded_many_users:
        raise RuntimeError("Seed dataset missing")

    def run() -> None:
        with CRUD(bench_user_model) as crud:
            row = crud.first()
            if row is None:
                raise RuntimeError("Seed row missing")
            ok = crud.delete(instance=row)
            if not ok:
                raise RuntimeError("CRUD delete(instance=...) returned False")
            crud.session.flush()
            crud.discard()

    benchmark(run)


@pytest.mark.benchmark(group="add_many_flush")
def test_sa_add_many_flush(
    benchmark,
    sa_session: Session,
    email_factory: Callable[[str], str],
    bench_user_model,
):
    def run() -> None:
        rows = [bench_user_model(email=email_factory("sa-bulk")) for _ in range(BATCH_SIZE)]
        sa_session.add_all(rows)
        sa_session.flush()
        sa_session.rollback()

    benchmark(run)


@pytest.mark.benchmark(group="add_many_flush")
def test_crud_add_many_flush(
    benchmark,
    configured_crud: None,
    email_factory: Callable[[str], str],
    bench_user_model,
):
    def run() -> None:
        rows = [
            bench_user_model(email=email_factory("crud-bulk"))
            for _ in range(BATCH_SIZE)
        ]
        with CRUD(bench_user_model) as crud:
            inserted = crud.add_many(rows)
            if inserted is None:
                raise RuntimeError("CRUD add_many returned None")
            crud.discard()

    benchmark(run)
