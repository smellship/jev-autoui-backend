"""登录态 profile 管理：列清单 / 清单个 / 清全部。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, Db
from app.services import audit, profile_service

router = APIRouter(prefix="/profiles", tags=["profiles"])


@router.get("")
def list_profiles(user: CurrentUser) -> dict:
    return {"items": profile_service.list_slots()}


@router.delete("/{slot:path}")
def clear_profile(slot: str, user: CurrentUser, db: Db) -> dict:
    result = profile_service.clear_slot(slot)
    audit.log(db, user, "profile.clear", "profile", slot, {"freed": result["freed"]})
    db.commit()
    return result


@router.delete("")
def clear_all_profiles(user: CurrentUser, db: Db) -> dict:
    result = profile_service.clear_all()
    audit.log(db, user, "profile.clear_all", "profile", "*", result)
    db.commit()
    return result
