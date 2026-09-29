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


def init_db() -> None:
    from app.models import Run, User

    Base.metadata.create_all(engine)
    _patch_user_table()
    _patch_run_table()
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
