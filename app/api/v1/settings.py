"""系统设置：模型 API Key（仅管理员）。密钥只写不读，响应只回键名与「已设置」。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentAdmin, Db
from app.core.errors import ApiError
from app.schemas import ModelKeysIn
from app.services import audit, model_keys

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/model-keys")
def get_model_keys(admin: CurrentAdmin, db: Db) -> dict:
    return {"items": model_keys.status()}


@router.put("/model-keys")
def put_model_keys(body: ModelKeysIn, admin: CurrentAdmin, db: Db) -> dict:
    """body 里 None=不改、空串=清除；写入 secrets/ 并即时对后续内核调用生效。"""
    updates = body.model_dump()
    sets, clears = [], []
    for key, value in updates.items():
        if value is None:
            continue
        if value.strip():
            if any(ch.isspace() for ch in value.strip()):
                raise ApiError(422, "bad_key", f"Key 不能含空白：{key}")
            sets.append(key)
        else:
            clears.append(key)
    if not sets and not clears:
        raise ApiError(422, "empty_payload", "至少要给一个 Key（空串表示清除）")
    changed = model_keys.write(updates)
    audit.log(db, admin, "settings.model_keys", "settings", "model-keys",
              {"set": sorted(sets), "clear": sorted(clears), "changed": changed})
    db.commit()
    return {"items": model_keys.status(), "changed": changed}
