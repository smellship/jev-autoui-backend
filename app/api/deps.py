"""FastAPI 依赖：当前登录用户。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.security import decode_token
from app.db import get_db
from app.models import User

Db = Annotated[Session, Depends(get_db)]


def current_user(authorization: Annotated[str | None, Header()] = None,
                 db: Session = Depends(get_db)) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "unauthorized", "缺少 Authorization: Bearer <token>")
    payload = decode_token(get_settings(), authorization.split(" ", 1)[1].strip())
    if not payload:
        raise ApiError(401, "token_invalid", "token 无效或已过期")
    user = db.get(User, int(payload.get("sub") or 0))
    if user is None or not user.active:
        raise ApiError(401, "account_disabled", "账号不存在或已停用")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def require_admin(user: CurrentUser) -> User:
    if not user.is_admin:
        raise ApiError(403, "forbidden", "需要管理员权限")
    return user


CurrentAdmin = Annotated[User, Depends(require_admin)]
