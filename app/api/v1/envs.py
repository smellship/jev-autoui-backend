"""环境管理：地址 / 密钥（只写不读）/ 探针 / 页清单（§7/§8.6）。"""

from __future__ import annotations

from fastapi import APIRouter, Response
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, Db
from app.api.serializers import iso
from app.core.errors import ApiError, not_found
from app.models import Env, PageInventory, Run, Scenario, iso_utc, utcnow
from app.schemas import EnvIn, EnvPatch, ProbeIn, SecretsIn
from app.services import audit, kernel_client, secret_store
from app.services.kernel_client import KernelError

router = APIRouter(prefix="/envs", tags=["envs"])


def get_env(db: Session, env_id: int) -> Env:
    env = db.get(Env, env_id)
    if env is None:
        raise not_found("环境", env_id)
    return env


def env_brief(env: Env) -> dict:
    keys = list(env.secret_keys or [])
    status = secret_store.key_status(env.name, keys)
    return {
        "id": env.id, "name": env.name, "label": env.label, "base_url": env.base_url,
        "configured": bool(secret_store.base_url(env)), "auth": env.auth,
        "secret_keys": [{"key": key, "set": status.get(key, False)} for key in keys],
        "timeout_ms": env.timeout_ms, "extra": env.extra or {}, "note": env.note,
        "last_probe": (env.extra or {}).get("last_probe"),
        "updated_at": iso(env.updated_at),
    }


def upsert_page(db: Session, env: Env, url: str, title: str, needs_login: bool | None,
                case_id: int | None = None) -> None:
    page = db.scalar(select(PageInventory).where(PageInventory.env_id == env.id,
                                                 PageInventory.url == url))
    if page is None:
        db.add(PageInventory(env_id=env.id, url=url, title=title, needs_login=needs_login,
                             source_case_id=case_id))
    else:
        page.title = title or page.title
        if needs_login is not None:
            page.needs_login = needs_login
        page.last_seen = utcnow()


@router.get("")
def list_envs(user: CurrentUser, db: Db) -> dict:
    envs = db.scalars(select(Env).order_by(Env.id)).all()
    return {"items": [env_brief(env) for env in envs]}


@router.post("", status_code=201)
def create_env(body: EnvIn, user: CurrentUser, db: Db) -> dict:
    if db.scalar(select(Env).where(Env.name == body.name)) is not None:
        raise ApiError(409, "env_exists", f"环境名已存在：{body.name}")
    env = Env(name=body.name, label=body.label, base_url=body.base_url, auth=body.auth,
              secret_keys=body.secret_keys, timeout_ms=body.timeout_ms, extra=body.extra,
              note=body.note)
    db.add(env)
    db.flush()
    audit.log(db, user, "env.create", "env", env.name, {"label": env.label})
    db.commit()
    return env_brief(env)


@router.get("/{env_id}")
def get_env_detail(env_id: int, user: CurrentUser, db: Db) -> dict:
    return env_brief(get_env(db, env_id))


@router.patch("/{env_id}")
def patch_env(env_id: int, body: EnvPatch, user: CurrentUser, db: Db) -> dict:
    env = get_env(db, env_id)
    for field in ("label", "base_url", "auth", "timeout_ms", "note"):
        value = getattr(body, field)
        if value is not None:
            setattr(env, field, value)
    if body.secret_keys is not None:
        env.secret_keys = body.secret_keys
    if body.extra is not None:
        env.extra = body.extra
    audit.log(db, user, "env.update", "env", env.name, {})
    db.commit()
    return env_brief(env)


@router.delete("/{env_id}", status_code=204)
def delete_env(env_id: int, user: CurrentUser, db: Db) -> Response:
    env = get_env(db, env_id)
    scenario = db.scalar(select(func.count()).select_from(Scenario)
                         .where(Scenario.env_id == env.id, Scenario.deleted_at.is_(None)))
    if scenario:
        raise ApiError(409, "env_in_use", "还有场景默认用这个环境，先改掉再删", {"scenarios": scenario})
    runs = db.scalar(select(func.count()).select_from(Run).where(Run.env_id == env.id))
    if runs:
        raise ApiError(409, "env_in_use", "已有运行记录引用这个环境（台账要保留），不能删",
                       {"runs": runs})
    audit.log(db, user, "env.delete", "env", env.name, {})
    # 页清单是探针缓存：随环境一起清（MySQL 外键会挡住带缓存的删除，SQLite 下则会留孤儿行）
    db.execute(delete(PageInventory).where(PageInventory.env_id == env.id))
    db.delete(env)
    db.commit()
    return Response(status_code=204)


@router.put("/{env_id}/secrets")
def put_secrets(env_id: int, body: SecretsIn, user: CurrentUser, db: Db) -> dict:
    """写密钥文件（只写不读）；响应只回键名与"已设置"。"""
    env = get_env(db, env_id)
    if body.base_url is None and not body.secrets:
        raise ApiError(422, "empty_payload", "至少要给 base_url 或一个密钥")
    secret_store.write_env_file(env.name, base_url=body.base_url, secrets=body.secrets)
    env.secret_keys = sorted(set(env.secret_keys or []) | set(body.secrets.keys()))
    audit.log(db, user, "env.secrets", "env", env.name,
              {"keys": sorted(body.secrets.keys()), "base_url": body.base_url is not None})
    db.commit()
    return env_brief(env)


@router.delete("/{env_id}/secrets/{key}")
def delete_secret(env_id: int, key: str, user: CurrentUser, db: Db) -> dict:
    """删一个密钥：本地文件与键名列表一起移除（写密钥的对称操作）。"""
    env = get_env(db, env_id)
    if not key.strip() or "/" in key or "\\" in key:
        raise ApiError(422, "bad_key", "键名不能为空或含 / \\")
    declared = list(env.secret_keys or [])
    in_file = secret_store.delete_key(env.name, key)
    if not in_file and key not in declared:
        raise ApiError(404, "secret_not_found", f"没有这个密钥：{key}", {"env": env.name})
    env.secret_keys = [item for item in declared if item != key]
    audit.log(db, user, "env.secrets.delete", "env", env.name, {"key": key, "in_file": in_file})
    db.commit()
    return env_brief(env)


@router.post("/{env_id}/probe")
def probe(env_id: int, user: CurrentUser, db: Db, body: ProbeIn | None = None) -> dict:
    """探针：从服务器侧只读打开，返回标题 / 元素数 / 是否登录页（零模型调用）。"""
    env = get_env(db, env_id)
    url = ((body.url if body else "") or secret_store.base_url(env)).strip()
    if not url:
        raise ApiError(409, "env_unconfigured", "还没有地址：先设 base_url，或给探针直接传 url",
                       {"env": env.name})
    try:
        data = kernel_client.call_json(["snapshot", "--url", url, "--json"], timeout=120)
    except KernelError as exc:
        raise ApiError(502, exc.code, "探针调用失败", exc.detail) from exc
    summary = data.get("summary") or {}
    result = {
        "env": env.name, "input_url": url,
        "final_url": summary.get("final_url") or url,
        "title": summary.get("title") or "",
        "element_count": summary.get("element_count") or 0,
        "needs_login": summary.get("needs_login"),
        "has_password_field": summary.get("has_password_field"),
        "at": iso_utc(utcnow()),
    }
    upsert_page(db, env, result["final_url"], result["title"], result["needs_login"])
    extra = dict(env.extra or {})
    extra["last_probe"] = result
    env.extra = extra
    audit.log(db, user, "env.probe", "env", env.name, {"url": url})
    db.commit()
    return result


@router.get("/{env_id}/pages")
def list_pages(env_id: int, user: CurrentUser, db: Db) -> dict:
    env = get_env(db, env_id)
    rows = db.scalars(select(PageInventory).where(PageInventory.env_id == env.id)
                      .order_by(PageInventory.url)).all()
    return {"items": [{"url": row.url, "title": row.title, "needs_login": row.needs_login,
                       "first_seen": iso(row.first_seen), "last_seen": iso(row.last_seen),
                       "source_case_id": row.source_case_id} for row in rows],
            "total": len(rows)}
