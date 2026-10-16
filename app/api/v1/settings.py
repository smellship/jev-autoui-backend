"""系统设置：模型 API Key + 运行并发（仅管理员）。密钥只写不读，响应只回键名与「已设置」。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentAdmin, Db
from app.core.errors import ApiError
from app.schemas import ModelKeysIn, PolicyIn, RunSettingsIn
from app.services import audit, model_keys, run_policy, run_service, run_settings

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/model-keys")
def get_model_keys(admin: CurrentAdmin, db: Db) -> dict:
    return {"items": model_keys.status(), "endpoints": model_keys.endpoints()}


@router.put("/model-keys")
def put_model_keys(body: ModelKeysIn, admin: CurrentAdmin, db: Db) -> dict:
    """body 里 None=不改、空串=清除；写入 secrets/ 并即时对后续内核调用生效。"""
    updates = body.model_dump()
    sets, clears = [], []
    for key, value in updates.items():
        if value is None:
            continue
        value = value.strip()
        if value:
            if any(ch.isspace() for ch in value):
                raise ApiError(422, "bad_key", f"值不能含空白：{key}")
            if key in model_keys.URL_KEYS and not value.lower().startswith(("http://", "https://")):
                raise ApiError(422, "bad_url", f"API 地址要以 http(s):// 开头：{key}")
            sets.append(key)
        else:
            clears.append(key)
    if not sets and not clears:
        raise ApiError(422, "empty_payload", "至少要给一个值（空串表示清除）")
    changed = model_keys.write(updates)
    audit.log(db, admin, "settings.model_keys", "settings", "model-keys",
              {"set": sorted(sets), "clear": sorted(clears), "changed": changed})
    db.commit()
    return {"items": model_keys.status(), "endpoints": model_keys.endpoints(), "changed": changed}


def _run_snapshot() -> dict:
    return run_service.queue.snapshot() | {"hard_max": run_settings.MAX_HARD}


@router.get("/run")
def get_run_settings(admin: CurrentAdmin, db: Db) -> dict:
    return _run_snapshot()


@router.put("/run")
def put_run_settings(body: RunSettingsIn, admin: CurrentAdmin, db: Db) -> dict:
    """改 max/k 立即生效：写完落文件并唤醒调度器；调小不杀在跑 run，只影响新派发。"""
    if body.max is None and body.k is None:
        raise ApiError(422, "empty_payload", "至少要给 max 或 k")
    if body.max is not None and not 1 <= body.max <= run_settings.MAX_HARD:
        raise ApiError(422, "bad_max", f"max 只能是 1..{run_settings.MAX_HARD} 的整数",
                       {"max": body.max})
    if body.k is not None and body.k < 1:
        raise ApiError(422, "bad_k", "k 至少是 1（每人同时在跑的上限）", {"k": body.k})
    current = run_settings.load()
    conf = run_settings.save(body.max if body.max is not None else current["max"],
                             body.k if body.k is not None else current["k"])
    audit.log(db, admin, "settings.run", "settings", "run", dict(conf))
    db.commit()
    run_service.queue.wake()
    return _run_snapshot()


@router.get("/policy")
def get_run_policy(admin: CurrentAdmin, db: Db) -> dict:
    return {"policy": run_policy.load()}


@router.put("/policy")
def put_run_policy(body: PolicyIn, admin: CurrentAdmin, db: Db) -> dict:
    """改策略只影响之后提交的 run：在跑/排队的按提交时的台账快照执行。"""
    updates = body.model_dump()
    if all(value is None for value in updates.values()):
        raise ApiError(422, "empty_payload", "至少要给一个开关")
    if body.decider is not None and body.decider not in run_policy.DECIDERS:
        raise ApiError(422, "bad_decider", "decider 只能是 jev / llm / off",
                       {"decider": body.decider})
    current = run_policy.load()
    conf = run_policy.save(
        body.decider if body.decider is not None else current["decider"],
        body.supervisor if body.supervisor is not None else current["supervisor"],
        body.cache if body.cache is not None else current["cache"],
        body.cache_strict if body.cache_strict is not None else current["cache_strict"],
    )
    audit.log(db, admin, "settings.policy", "settings", "policy", dict(conf))
    db.commit()
    return {"policy": conf}
