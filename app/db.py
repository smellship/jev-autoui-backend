"""数据库：引擎 / 会话 / 建表 / 序列号 / 种子管理员。

本地开发默认 SQLite；生产用 MySQL，只改 UI_WEB_DB_URL。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, select
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings
from app.core.security import hash_password


class Base(DeclarativeBase):
    pass


def _engine_kwargs(url: str) -> dict:
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {"pool_pre_ping": True, "pool_recycle": 3600}


_settings = get_settings()
engine = create_engine(_settings.db_url, **_engine_kwargs(_settings.db_url))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def next_seq(db: Session, key: str) -> int:
    from app.models import Seq

    row = db.get(Seq, key)
    if row is None:
        row = Seq(key=key, value=0)
        db.add(row)
    row.value += 1
    db.flush()
    return row.value


def _patch_user_table() -> None:
    """老库补列（无 Alembic 的轻量补丁）：user 缺 is_admin 时补上，并把配置里的管理员置真。"""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "user" not in insp.get_table_names():
        return
    if "is_admin" in {col["name"] for col in insp.get_columns("user")}:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE user ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"))
        conn.execute(text("UPDATE user SET is_admin = 1 WHERE account = :account"),
                     {"account": get_settings().admin_account})


def _patch_run_table() -> None:
    """老库补列：run 缺 batch_code/suite_id 时补上（批次/套件）。

    补丁列不带外键（SQLite/MySQL 老库都能跑）；新建库由 create_all 直接带全。
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "run" not in insp.get_table_names():
        return
    cols = {col["name"] for col in insp.get_columns("run")}
    with engine.begin() as conn:
        if "batch_code" not in cols:
            conn.execute(text("ALTER TABLE run ADD COLUMN batch_code VARCHAR(40) NULL"))
            conn.execute(text("CREATE INDEX ix_run_batch_code ON run (batch_code)"))
        if "suite_id" not in cols:
            conn.execute(text("ALTER TABLE run ADD COLUMN suite_id INT NULL"))
            conn.execute(text("CREATE INDEX ix_run_suite_id ON run (suite_id)"))


def _patch_actor_columns() -> None:
    """老库补列：scenario.created_by/updated_by、case.created_by（操作人展示用）。

    只在补列那一次回填存量：审计日志里存的是账号，回填时解析成显示名。
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    added = False
    with engine.begin() as conn:
        for table, names in (("scenario", ("created_by", "updated_by")), ("case", ("created_by",))):
            if table not in tables:
                continue
            quoted = engine.dialect.identifier_preparer.quote(table)  # case 是 MySQL 保留字
            cols = {col["name"] for col in insp.get_columns(table)}
            for name in names:
                if name not in cols:
                    conn.execute(text(
                        f"ALTER TABLE {quoted} ADD COLUMN {name} VARCHAR(64) NOT NULL DEFAULT ''"))
                    added = True
    if added:
        _backfill_actors()


def _backfill_actors() -> None:
    """从审计日志补存量行的操作人：created_by 取最早一笔，updated_by 取最近一笔。

    写回走裸 SQL：ORM 的 onupdate 会把 updated_at 顶成当前时间，污染「最后修改时间」。
    """
    from sqlalchemy import text

    from app.models import AuditLog, Case, Scenario, User

    quote = engine.dialect.identifier_preparer.quote
    updates: list[tuple[str, int, dict[str, str]]] = []
    with session_scope() as db:
        names = {user.account: (user.name or user.account)
                 for user in db.scalars(select(User)).all()}
        first: dict[tuple[str, str], str] = {}
        last: dict[tuple[str, str], str] = {}
        rows = db.scalars(select(AuditLog).where(AuditLog.action.in_(
            ("scenario.create", "scenario.update", "case.create", "case.copy")))
            .order_by(AuditLog.id)).all()
        for row in rows:
            who = names.get(row.user_name, row.user_name)
            key = (row.object_type, row.object_id)
            first.setdefault(key, who)
            last[key] = who
        for scenario in db.scalars(select(Scenario)).all():
            values = {}
            if not scenario.created_by:
                values["created_by"] = first.get(("scenario", scenario.code), "")
            if not scenario.updated_by:
                values["updated_by"] = last.get(("scenario", scenario.code), "")
            if values:
                updates.append(("scenario", scenario.id, values))
        for case in db.scalars(select(Case)).all():
            if not case.created_by:
                updates.append(("case", case.id, {"created_by": first.get(("case", case.code), "")}))
    with engine.begin() as conn:
        for table, row_id, values in updates:
            sets = ", ".join(f"{name} = :{name}" for name in values)
            conn.execute(text(f"UPDATE {quote(table)} SET {sets} WHERE id = :row_id"),
                         {**values, "row_id": row_id})


def init_db() -> None:
    from app.models import Run, User

    Base.metadata.create_all(engine)
    _patch_user_table()
    _patch_run_table()
    _patch_actor_columns()
    with session_scope() as db:
        settings = get_settings()
        user = db.scalar(select(User).where(User.account == settings.admin_account))
        if user is None:
            salt, digest = hash_password(settings.admin_password)
            db.add(User(account=settings.admin_account, name=settings.admin_name,
                        pwd_salt=salt, pwd_hash=digest, active=True, is_admin=True))
        # 服务重启后不可能还有活的子进程：把残留的 queued/running 记为中止
        stale = db.scalars(select(Run).where(Run.status.in_(("queued", "running")))).all()
        for run in stale:
            run.status = "aborted"
            run.detail = "服务重启，运行中断（产物可能不完整）"
            run.finished_at = run.finished_at or run.started_at
