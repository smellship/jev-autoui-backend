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
    """原子取号：一条 upsert 语句「插入或自增」后回读，并发下由行的排它锁串行化，不重不漏。

    MySQL 用 ON DUPLICATE KEY UPDATE、SQLite 用 ON CONFLICT DO UPDATE——插入与自增一步到位。
    不用「先查再插再 UPDATE」：并发补插时双方会先拿重复校验的 S 锁、再升级 X 锁互相等，实测会打出 1213 死锁。
    """
    from sqlalchemy import text

    from app.models import Seq

    dialect = db.get_bind().dialect
    quote = dialect.identifier_preparer.quote
    if dialect.name == "mysql":
        sql = (f"INSERT INTO seq ({quote('key')}, {quote('value')}) VALUES (:key, 1) "
               f"ON DUPLICATE KEY UPDATE {quote('value')} = {quote('value')} + 1")
    else:
        sql = (f"INSERT INTO seq ({quote('key')}, {quote('value')}) VALUES (:key, 1) "
               f"ON CONFLICT ({quote('key')}) DO UPDATE SET {quote('value')} = {quote('value')} + 1")
    db.execute(text(sql), {"key": key})
    return int(db.scalar(select(Seq.value).where(Seq.key == key)) or 0)


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
    """老库补列：run 缺 batch_code/suite_id/user_id 时补上（批次/套件/运行属主）。

    补丁列不带外键（SQLite/MySQL 老库都能跑）；新建库由 create_all 直接带全。
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "run" not in insp.get_table_names():
        return
    cols = {col["name"] for col in insp.get_columns("run")}
    added_owner = False
    with engine.begin() as conn:
        if "batch_code" not in cols:
            conn.execute(text("ALTER TABLE run ADD COLUMN batch_code VARCHAR(40) NULL"))
            conn.execute(text("CREATE INDEX ix_run_batch_code ON run (batch_code)"))
        if "suite_id" not in cols:
            conn.execute(text("ALTER TABLE run ADD COLUMN suite_id INT NULL"))
            conn.execute(text("CREATE INDEX ix_run_suite_id ON run (suite_id)"))
        if "user_id" not in cols:
            conn.execute(text("ALTER TABLE run ADD COLUMN user_id INT NULL"))
            conn.execute(text("CREATE INDEX ix_run_user_id ON run (user_id)"))
            added_owner = True
    if added_owner:
        _backfill_run_user_ids()


def _backfill_run_user_ids() -> None:
    """从审计日志补存量 run 的发起人：run.create 按 run code 对，批次内按 batch_code 归。"""
    from sqlalchemy import text

    from app.models import AuditLog, Run

    with session_scope() as db:
        by_code: dict[str, int] = {}
        by_batch: dict[str, int] = {}
        rows = db.scalars(select(AuditLog).where(AuditLog.action.in_(("run.create", "batch.create")),
                                                 AuditLog.user_id.is_not(None))
                          .order_by(AuditLog.id)).all()
        for row in rows:
            if row.action == "run.create":
                by_code.setdefault(row.object_id, row.user_id)
            else:
                by_batch.setdefault(row.object_id, row.user_id)
        updates: list[tuple[int, int]] = []
        for run in db.scalars(select(Run).where(Run.user_id.is_(None))).all():
            owner = by_code.get(run.code)
            if owner is None and run.batch_code:
                owner = by_batch.get(run.batch_code)
            if owner:
                updates.append((run.id, owner))
    if not updates:
        return
    with engine.begin() as conn:
        for run_id, owner in updates:
            conn.execute(text("UPDATE run SET user_id = :owner WHERE id = :run_id"),
                         {"owner": owner, "run_id": run_id})


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


def _patch_mutex_columns() -> None:
    """老库补列：case.mutex（挡位）、run.mutex/run.mutex_key（提交 run 时的挡位与键快照）。"""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        if "case" in tables:
            quoted = engine.dialect.identifier_preparer.quote("case")  # case 是 MySQL 保留字
            if "mutex" not in {col["name"] for col in insp.get_columns("case")}:
                conn.execute(text(
                    f"ALTER TABLE {quoted} ADD COLUMN mutex VARCHAR(10) NOT NULL DEFAULT 'none'"))
        if "run" in tables:
            cols = {col["name"] for col in insp.get_columns("run")}
            if "mutex" not in cols:
                conn.execute(text("ALTER TABLE run ADD COLUMN mutex VARCHAR(10) NOT NULL DEFAULT 'none'"))
            if "mutex_key" not in cols:
                conn.execute(text("ALTER TABLE run ADD COLUMN mutex_key VARCHAR(64) NULL"))


def _patch_run_policy_column() -> None:
    """老库补列：run.policy（提交 run 时的运行策略快照：决策/监督/缓存/严判）。

    补丁列可空（老 run 没有快照，读出来按默认全开执行）；SQLite 存 TEXT、MySQL 用 JSON。
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "run" not in insp.get_table_names():
        return
    if "policy" in {col["name"] for col in insp.get_columns("run")}:
        return
    col_type = "JSON" if engine.dialect.name == "mysql" else "TEXT"
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE run ADD COLUMN policy {col_type} NULL"))


def init_db() -> None:
    from app.models import Run, User

    Base.metadata.create_all(engine)
    _patch_user_table()
    _patch_run_table()
    _patch_actor_columns()
    _patch_mutex_columns()
    _patch_run_policy_column()
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
