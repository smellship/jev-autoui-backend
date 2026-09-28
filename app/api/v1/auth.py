"""登录、当前用户与自助改密码/昵称（§11）。"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import CurrentUser, Db
from app.api.serializers import user_brief
from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.security import create_token, hash_password, password_problem, verify_password
from app.models import User
from app.schemas import LoginIn, PasswordChangeIn, ProfileIn
from app.services import audit

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
def login(body: LoginIn, db: Db) -> dict:
    user = db.scalar(select(User).where(User.account == body.account))
    if user is None or not user.active or not verify_password(body.password, user.pwd_salt, user.pwd_hash):
        raise ApiError(401, "bad_credentials", "账号或密码不正确")
    token, ttl = create_token(get_settings(), user.id, user.account)
    audit.log(db, user, "auth.login", "user", user.account, {})
    db.commit()
    return {"token": token, "expires_in": ttl, "user": user_brief(user)}


@router.get("/me")
def me(user: CurrentUser) -> dict:
    return user_brief(user)


@router.patch("/me")
def update_me(body: ProfileIn, user: CurrentUser, db: Db) -> dict:
    """改自己的昵称（账号不可改）。"""
    name = body.name.strip()
    if not name:
        raise ApiError(422, "bad_name", "昵称不能为空")
    user.name = name[:64]
    audit.log(db, user, "user.profile", "user", user.account, {"name": user.name})
    db.commit()
    return user_brief(user)


@router.put("/password")
def change_password(body: PasswordChangeIn, user: CurrentUser, db: Db) -> dict:
    """改自己的密码：先核对原密码。"""
    if not verify_password(body.old_password, user.pwd_salt, user.pwd_hash):
        raise ApiError(401, "bad_password", "原密码不对")
    problem = password_problem(body.new_password)
    if problem:
        raise ApiError(422, "weak_password", problem)
    user.pwd_salt, user.pwd_hash = hash_password(body.new_password)
    audit.log(db, user, "user.password", "user", user.account, {})
    db.commit()
    return {"ok": True}
