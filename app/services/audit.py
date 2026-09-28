"""审计：用例保存/删除、运行、密钥变更全记一笔（§12）。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AuditLog


def log(db: Session, user, action: str, object_type: str = "", object_id: object = "",
        detail: dict | None = None) -> None:
    db.add(AuditLog(
        user_id=getattr(user, "id", None),
        user_name=getattr(user, "account", "") or "",
        action=action,
        object_type=object_type,
        object_id=str(object_id or ""),
        detail=detail or {},
    ))
