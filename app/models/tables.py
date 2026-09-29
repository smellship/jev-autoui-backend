"""平台数据表（spec §5.1）。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Seq(Base):
    """编号分配器：sc-0001 / sc-0001-01。"""

    __tablename__ = "seq"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class User(Base):
    __tablename__ = "user"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    pwd_salt: Mapped[str] = mapped_column(String(64), default="")
    pwd_hash: Mapped[str] = mapped_column(String(128), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Node(Base):
    """分类树：一级节点（百望云）→ 二级节点（查验）。"""

    __tablename__ = "node"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("node.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(128))
    sort: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str] = mapped_column(Text, default="")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Scenario(Base):
    __tablename__ = "scenario"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # sc-0001
    node_id: Mapped[int] = mapped_column(ForeignKey("node.id"))
    name: Mapped[str] = mapped_column(String(128))
    priority: Mapped[str] = mapped_column(String(8), default="P2")
    owner: Mapped[str] = mapped_column(String(64), default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    env_id: Mapped[int | None] = mapped_column(ForeignKey("env.id"), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Case(Base):
    __tablename__ = "case"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(48), unique=True, index=True)  # sc-0001-01
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenario.id"))
    name: Mapped[str] = mapped_column(String(128))
    priority: Mapped[str] = mapped_column(String(8), default="P2")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    owner: Mapped[str] = mapped_column(String(64), default="")
    yaml_text: Mapped[str] = mapped_column(Text, default="")
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CaseVersion(Base):
    __tablename__ = "case_version"
    __table_args__ = (UniqueConstraint("case_id", "version", name="uq_case_version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("case.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    yaml_text: Mapped[str] = mapped_column(Text)
    comment: Mapped[str] = mapped_column(String(255), default="")
    author: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Suite(Base):
    """场景套件：命名 + 有序用例列表（场景管理）。"""

    __tablename__ = "suite"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # su-0001
    name: Mapped[str] = mapped_column(String(128))
    owner: Mapped[str] = mapped_column(String(64), default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SuiteCase(Base):
    """套件 ↔ 用例的有序引用（sort 即串跑顺序）。"""

    __tablename__ = "suite_case"
    __table_args__ = (UniqueConstraint("suite_id", "case_id", name="uq_suite_case"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    suite_id: Mapped[int] = mapped_column(ForeignKey("suite.id"), index=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("case.id"), index=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class Env(Base):
    __tablename__ = "env"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # 192.0.2.10
    label: Mapped[str] = mapped_column(String(128), default="")
    base_url: Mapped[str] = mapped_column(String(255), default="")
    auth: Mapped[str] = mapped_column(String(16), default="unknown")  # unknown|none|login_form
    secret_keys: Mapped[list] = mapped_column(JSON, default=list)  # 只登记键名
    timeout_ms: Mapped[int] = mapped_column(Integer, default=15000)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)  # 非敏感附加项（如 样例目录、last_probe）
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Run(Base):
    __tablename__ = "run"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)  # r-20260924-150211-83票
    case_id: Mapped[int] = mapped_column(ForeignKey("case.id"), index=True)
    env_id: Mapped[int | None] = mapped_column(ForeignKey("env.id"), nullable=True)
    case_code: Mapped[str] = mapped_column(String(48), default="")
    case_name: Mapped[str] = mapped_column(String(128), default="")
    scenario_name: Mapped[str] = mapped_column(String(128), default="")
    env_name: Mapped[str] = mapped_column(String(64), default="")
    mode: Mapped[str] = mapped_column(String(8), default="ci")  # debug|ci
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    kernel_status: Mapped[str] = mapped_column(String(24), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    steps: Mapped[int] = mapped_column(Integer, default=0)
    checks: Mapped[list] = mapped_column(JSON, default=list)
    plan_json: Mapped[str] = mapped_column(Text, default="")  # 本次运行编译出的 TestPlan（同一份）
    artifact_dir: Mapped[str] = mapped_column(String(255), default="")
    triggered_by: Mapped[str] = mapped_column(String(64), default="")
    batch_code: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)  # b-20260929-150211
    suite_id: Mapped[int | None] = mapped_column(ForeignKey("suite.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)


class PageInventory(Base):
    __tablename__ = "page_inventory"
    __table_args__ = (UniqueConstraint("env_id", "url", name="uq_env_url"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    env_id: Mapped[int] = mapped_column(ForeignKey("env.id"), index=True)
    url: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(255), default="")
    needs_login: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # 是 / 否 / 未知
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    source_case_id: Mapped[int | None] = mapped_column(ForeignKey("case.id"), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_name: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(64), index=True)
    object_type: Mapped[str] = mapped_column(String(32), default="")
    object_id: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
