import pathlib
import sys

import pytest
from sqlalchemy import func
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import object_session

ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sqlalchemy.exc import SQLAlchemyError

from flask_sqlalchemy_crud import CRUD, SQLStatus  # noqa: E402


def _cleanup_all(app, db, model) -> None:
    with app.app_context():
        db.session.query(model).delete()
        db.session.commit()


def _cleanup_multi(app, db, *models) -> None:
    with app.app_context():
        for model in models:
            db.session.query(model).delete()
        db.session.commit()


def test_crud_add_query_update_delete(app_and_db) -> None:
    """End-to-end CRUD flow (create / query / update / delete) with Flask-SQLAlchemy."""
    app, db, User, _Profile = app_and_db
    _cleanup_all(app, db, User)

    with app.app_context():
        CRUD.configure(session=db.session, error_policy="raise")

        email = "user@example.com"

        # 创建记录
        with CRUD(User) as crud:
            created = crud.add(email=email)
            assert created is not None
            assert created.id is not None

        # 查询与更新
        with CRUD(User, email=email) as crud:
            user = crud.first()
            assert user is not None
            assert user.email == email

            updated = crud.update(user, email="new@example.com")
            assert updated is not None
            assert updated.email == "new@example.com"

        # 删除
        with CRUD(User, email="new@example.com") as crud:
            deleted = crud.delete()
            assert deleted is True
            assert crud.status == SQLStatus.OK

        # 确认已删除
        with CRUD(User, email="new@example.com") as crud:
            assert crud.first() is None


def test_crud_transaction_decorator_join(app_and_db) -> None:
    """Function-level transaction decorator should join inner CRUD scopes."""
    app, db, User, _Profile = app_and_db
    _cleanup_all(app, db, User)

    with app.app_context():
        CRUD.configure(session=db.session, error_policy="raise")

        @CRUD.transaction()
        def create_two_users() -> None:
            with CRUD(User) as crud1:
                crud1.add(email="a@example.com")
            with CRUD(User) as crud2:
                crud2.add(email="b@example.com")

        create_two_users()

        # 两条记录应在同一事务内成功提交
        count = db.session.query(User).count()
        assert count >= 2


def test_on_sql_error_respects_error_policy(app_and_db) -> None:
    """CRUD._on_sql_error should respect per-instance error_policy."""
    app, db, User, _Profile = app_and_db

    with app.app_context():
        CRUD.configure(session=db.session)

        # status 策略：不抛出异常，只设置状态
        crud_status = CRUD(User).config(error_policy="status")
        crud_status._on_sql_error(SQLAlchemyError("test"))  # type: ignore[attr-defined]  # noqa: SLF001
        assert crud_status.status == SQLStatus.SQL_ERR

        # raise 策略：应抛出 SQLAlchemyError
        crud_raise = CRUD(User).config(error_policy="raise")
        with pytest.raises(SQLAlchemyError):
            crud_raise._on_sql_error(SQLAlchemyError("test"))  # type: ignore[attr-defined]  # noqa: SLF001


def test_transaction_rollback_on_runtime_exception(app_and_db) -> None:
    """Non-SQLAlchemy exceptions should cause full rollback of the outer transaction."""
    app, db, User, _Profile = app_and_db
    _cleanup_all(app, db, User)

    with app.app_context():
        CRUD.configure(session=db.session)

        @CRUD.transaction()
        def create_then_fail() -> None:
            with CRUD(User) as crud:
                crud.add(email="rollback@example.com")
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            create_then_fail()

        assert db.session.query(User).count() == 0


def test_nested_transaction_join_and_rollback(app_and_db) -> None:
    """Nested transaction decorators join the same transaction; outer failure rolls back all."""
    app, db, User, Profile = app_and_db
    _cleanup_multi(app, db, User, Profile)

    with app.app_context():
        CRUD.configure(session=db.session)

        @CRUD.transaction()
        def inner_create(email: str) -> None:
            with CRUD(User) as crud_user:
                user = crud_user.add(email=email)
                assert user is not None
            with CRUD(Profile) as crud_profile:
                crud_profile.add(user_id=user.id, bio="nested")  # type: ignore[arg-type]

        @CRUD.transaction()
        def outer() -> None:
            inner_create("nested@example.com")
            raise RuntimeError("outer failed")

        with pytest.raises(RuntimeError):
            outer()

        assert db.session.query(User).count() == 0
        assert db.session.query(Profile).count() == 0


def test_status_policy_rolls_back_without_raising(app_and_db) -> None:
    """With error_policy='status' SQLAlchemyError should be rolled back but not raised."""

    app, db, User, Profile = app_and_db
    _cleanup_multi(app, db, User, Profile)

    with app.app_context():
        CRUD.configure(session=db.session)

        @CRUD.transaction(error_policy="status")
        def create_then_force_sql_error() -> None:
            with CRUD(User).config(error_policy="status") as crud:
                crud.add(email="status@example.com")
                # 模拟一次 SQLAlchemyError，以触发 _on_sql_error 中的回滚逻辑
                crud._on_sql_error(SQLAlchemyError("forced"))  # type: ignore[attr-defined]  # noqa: SLF001

        # 不应抛出异常
        create_then_force_sql_error()

        # 记录应被回滚
        assert db.session.query(User).count() == 0


def test_discard_rolls_back_partial_operations(app_and_db) -> None:
    """discard() should roll back part of operations within the same transaction scope."""
    app, db, User, Profile = app_and_db
    _cleanup_multi(app, db, User, Profile)

    with app.app_context():
        CRUD.configure(session=db.session)

        @CRUD.transaction()
        def mixed_ops() -> None:
            # 第一段：应保留
            with CRUD(User) as crud_keep:
                user = crud_keep.add(email="keep@example.com")
                assert user is not None

            # 第二段：为同一用户创建 profile，并通过 discard 回滚
            with CRUD(Profile) as crud_profile:
                crud_profile.add(user_id=user.id, bio="to-be-discarded")  # type: ignore[arg-type]
                crud_profile.discard()

        mixed_ops()

        users = {u.email for u in db.session.query(User).all()}
        profiles = list(db.session.query(Profile).all())

        assert "keep@example.com" in users
        assert len(profiles) == 0


def test_query_count_order_by_group_by(app_and_db) -> None:
    """Exercise CRUDQuery count / order_by / group_by chaining behaviour."""
    app, db, User, Profile = app_and_db
    _cleanup_multi(app, db, User, Profile)

    with app.app_context():
        CRUD.configure(session=db.session, error_policy="raise")

        emails = [
            "alpha@example.com",
            "beta@example.com",
            "gamma@example.com",
            "delta@example.com",
        ]

        # 预置多条记录（每条记录使用独立 CRUD 实例，避免复用同一 instance）
        for email in emails:
            with CRUD(User) as crud_seed:
                crud_seed.add(email=email)

        with CRUD(User) as crud:
            # count：应与插入条数一致
            base_query = crud.query()
            assert base_query.count() == len(emails)

            # order_by：按 email 倒序排列
            ordered = crud.query().order_by(User.email.desc()).all()
            ordered_emails = [u.email for u in ordered]
            assert ordered_emails == sorted(emails, reverse=True)

            # group_by：按 id 的奇偶性分组，并统计每组数量
            group_query = (
                crud.query()
                .with_entities(func.mod(User.id, 2).label("grp"), func.count(User.id))
                .group_by("grp")
            )
            grouped = group_query.all()

            total = sum(count for _grp, count in grouped)
            assert total == len(emails)
            # 理论上会有 1~2 个分组（全偶 / 全奇 / 混合），只需要确认 group_by 正常工作
            assert 1 <= len(grouped) <= 2


def test_add_many_and_global_filters(app_and_db) -> None:
    """Cover add_many and global filter enable/disable behaviour."""
    app, db, User, _Profile = app_and_db
    _cleanup_all(app, db, User)

    with app.app_context():
        CRUD.configure(session=db.session, error_policy="raise")

        # add_many：应批量插入并返回带主键的实例列表
        first = User(email="first@example.com")
        second = User(email="second@test.com")
        with CRUD(User) as crud:
            added = crud.add_many([first, second])
            assert added is not None
            assert {u.email for u in added} == {
                "first@example.com",
                "second@test.com",
            }
            assert all(u.id is not None for u in added)

        # 启用全局过滤器，仅保留 example.com 邮箱
        CRUD.register_global_filters(User.email.like("%@example.com"))
        try:
            with CRUD(User) as crud_filtered:
                filtered = crud_filtered.all()
                assert {u.email for u in filtered} == {"first@example.com"}
                db.session.rollback()  # 结束只读事务，避免后续 begin 冲突

            # 禁用全局过滤器，验证可以取回全部记录
            with CRUD(User).config(disable_global_filter=True) as crud_unfiltered:
                all_users = crud_unfiltered.all()
                assert {u.email for u in all_users} == {
                    "first@example.com",
                    "second@test.com",
                }
        finally:
            CRUD.register_global_filters()  # 恢复默认
            _cleanup_all(app, db, User)


def test_create_instance_and_explicit_commit(app_and_db) -> None:
    """Cover create_instance(no_attach) and explicit commit path."""
    app, db, User, _Profile = app_and_db
    _cleanup_all(app, db, User)

    with app.app_context():
        CRUD.configure(session=db.session, error_policy="raise")

        # create_instance(no_attach) 不应绑定 CRUD 实例或 Session
        crud_tmp = CRUD(User)
        detached = crud_tmp.create_instance(no_attach=True)
        assert crud_tmp.instance is None
        assert detached.id is None
        assert object_session(detached) is None

        @CRUD.transaction()
        def create_with_explicit_commit() -> None:
            with CRUD(User) as crud:
                user = crud.add(email="explicit@example.com")
                assert user is not None
                crud.commit()  # 覆盖显式提交路径

        create_with_explicit_commit()
        assert db.session.query(User).count() == 1


def test_pure_query_and_all_records_delete(app_and_db) -> None:
    """Cover query(pure=True) and delete(all_records=True) branches (ignore instance filters)."""
    app, db, User, _Profile = app_and_db
    _cleanup_all(app, db, User)

    with app.app_context():
        CRUD.configure(session=db.session)
        emails = ["keep@example.com", "drop@example.com"]
        for email in emails:
            with CRUD(User) as crud:
                crud.add(email=email)

        # 构造实例级过滤条件：email="keep@example.com"
        with CRUD(User, email="keep@example.com") as crud_filtered:
            # 默认 query 应只看到一条
            assert crud_filtered.query().count() == 1
            filtered_first = crud_filtered.first()
            assert filtered_first is not None
            assert filtered_first.email == "keep@example.com"

            # pure=True 应忽略实例过滤条件，返回两条
            pure_all = crud_filtered.query(pure=True).all()
            assert {u.email for u in pure_all} == set(emails)

        db.session.rollback()  # 结束读事务，避免后续 begin 冲突

        # all_records=True 删除全部
        with CRUD(User) as crud_delete:
            ok = crud_delete.delete(all_records=True)
            assert ok is True
            assert crud_delete.status == SQLStatus.OK

        assert db.session.query(User).count() == 0


def test_need_commit_marks_dirty_without_mutation(app_and_db) -> None:
    """need_commit() should cause commit on exit even without further mutations."""
    app, db, User, _Profile = app_and_db
    _cleanup_all(app, db, User)

    with app.app_context():
        CRUD.configure(session=db.session)

        with CRUD(User) as crud:
            user = crud.add(email="need-commit@example.com")
            assert user is not None
            crud.need_commit()

        assert db.session.query(User).count() == 1


def test_error_policy_inherits_from_transaction_decorator(app_and_db) -> None:
    """error_policy from transaction decorator should be visible to CRUD._resolve_error_policy."""
    app, db, User, _Profile = app_and_db
    _cleanup_all(app, db, User)

    with app.app_context():
        CRUD.configure(session=db.session, error_policy="raise")

        # 先插入一条记录，后续重复插入触发唯一约束
        with CRUD(User) as crud:
            crud.add(email="dup@example.com")

        @CRUD.transaction(error_policy="status")
        def create_duplicate() -> None:
            with CRUD(User) as crud_dup:
                # 触发唯一约束导致 SQLAlchemyError，但 error_policy=status 不应向外抛出
                crud_dup.add(email="dup@example.com")

        create_duplicate()
        # 事务应回滚，仍然只有一条记录
        assert db.session.query(User).count() == 1


def test_crud_conflicts_with_external_manual_transaction(app_and_db) -> None:
    """Manual external session.begin() that is invisible to CRUD should cause a conflict error."""
    app, db, User, _Profile = app_and_db
    _cleanup_all(app, db, User)

    with app.app_context():
        CRUD.configure(session=db.session)
        txn = db.session.begin()
        try:
            with pytest.raises(InvalidRequestError):
                with CRUD(User):
                    pass
        finally:
            if txn.is_active:
                txn.rollback()
        db.session.expunge_all()


def test_transaction_decorator_conflicts_with_external_manual_transaction(
    app_and_db,
) -> None:
    """Transaction decorator combined with external manual begin should also conflict."""
    app, db, User, _Profile = app_and_db
    _cleanup_all(app, db, User)

    with app.app_context():
        CRUD.configure(session=db.session)
        txn = db.session.begin()
        try:

            @CRUD.transaction()
            def create_user_inside() -> None:
                with CRUD(User) as crud:
                    crud.add(email="conflict@example.com")

            with pytest.raises(InvalidRequestError):
                create_user_inside()
        finally:
            if txn.is_active:
                txn.rollback()
        db.session.expunge_all()
