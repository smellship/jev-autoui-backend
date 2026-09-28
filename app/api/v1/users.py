"""用户管理：列表 / 新建 / 改名停用 / 重置密码（仅管理员，§11）。"""

from __future__ import annotations

import re

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentAdmin, Db
from app.api.serializers import user_brief
from app.core.errors import ApiError, not_found
from app.core.security import hash_password, password_problem
from app.models import User
from app.schemas import PasswordResetIn, UserIn, UserPatch
from app.services import audit

router = APIRouter(prefix="/users", tags=["users"])

ACCOUNT_PATTERN = re.compile(r"^[^\s]{2,64}$")


def get_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise not_found("用户", user_id)
    return user


@router.get("")
def list_users(admin: CurrentAdmin, db: Db) -> dict:
    users = db.scalars(select(User).order_by(User.id)).all()
    return {"items": [user_brief(user) for user in users]}


@router.post("", status_code=201)
def create_user(body: UserIn, admin: CurrentAdmin, db: Db) -> dict:
    account = body.account.strip()
    if not ACCOUNT_PATTERN.match(account):
        raise ApiError(422, "bad_account", "账号 2–64 位、不能含空白")
    if db.scalar(select(User).where(User.account == account)) is not None:
        raise ApiError(409, "user_exists", f"账号已存在：{account}")
    problem = password_problem(body.password)
    if problem:
        raise ApiError(422, "weak_password", problem)
    salt, digest = hash_password(body.password)
    user = User(account=account, name=body.name.strip()[:64] or account,
                pwd_salt=salt, pwd_hash=digest, active=True, is_admin=False)
    db.add(user)
    db.flush()
    audit.log(db, admin, "user.create", "user", user.account, {"name": user.name})
    db.commit()
    return user_brief(user)


@router.patch("/{user_id}")
def update_user(user_id: int, body: UserPatch, admin: CurrentAdmin, db: Db) -> dict:
    user = get_user(db, user_id)
    changes: dict = {}
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise ApiError(422, "bad_name", "昵称不能为空")
        user.name = name[:64]
        changes["name"] = user.name
    if body.active is not None or body.is_admin is not None:
        if user.id == admin.id:
            raise ApiError(409, "self_guard", "不能停用自己或改自己的管理员标志")
        if body.active is not None:
            user.active = body.active
            changes["active"] = user.active
        if body.is_admin is not None:
            user.is_admin = body.is_admin
            changes["is_admin"] = user.is_admin
    audit.log(db, admin, "user.update", "user", user.account, changes)
    db.commit()
    return user_brief(user)


@router.post("/{user_id}/password")
def reset_password(user_id: int, body: PasswordResetIn, admin: CurrentAdmin, db: Db) -> dict:
    user = get_user(db, user_id)
    problem = password_problem(body.password)
    if problem:
        raise ApiError(422, "weak_password", problem)
    user.pwd_salt, user.pwd_hash = hash_password(body.password)
    audit.log(db, admin, "user.reset_password", "user", user.account, {})
    db.commit()
    return {"ok": True}
