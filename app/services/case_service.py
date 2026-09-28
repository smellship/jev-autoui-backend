"""用例服务：校验编译、版本保存/回滚、自然语言草稿（§6/§8.4）。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError, not_found
from app.importers import midscene
from app.models import Case, CaseVersion, Env, utcnow
from app.services import audit, kernel_client
from app.services.kernel_client import KernelError

SKELETON = (
    "steps:\n"
    "  - goal: \"把这一步改写成：打开哪个页面、做什么、达成什么\"\n"
    "    checks:\n"
    "      - title_contains: \"填一个能在页面上验证到的关键词\"\n"
)


def yaml_scalar(value: object) -> str:
    """JSON 字符串是合法 YAML 标量：避免中文/冒号/引号把 YAML 写坏。"""
    return json.dumps("" if value is None else str(value), ensure_ascii=False)


def actor(user) -> str:
    return getattr(user, "name", "") or getattr(user, "account", "") or ""


def compile_yaml(yaml_text: str, secret_keys: list[str] | None = None) -> dict:
    """内核 `case compile --in <yaml> --json`：返回 {ok, errors, warnings, plan}。"""
    with tempfile.TemporaryDirectory(prefix="uiweb-case-") as tmp:
        path = Path(tmp) / "case.yaml"
        path.write_text(yaml_text or "", encoding="utf-8")
        args = ["case", "compile", "--in", str(path), "--json"]
        if secret_keys:
            args += ["--secret-keys", ",".join(secret_keys)]
        try:
            data = kernel_client.call_json(args, timeout=60)
        except KernelError as exc:
            raise ApiError(502, exc.code, "内核校验调用失败", exc.detail) from exc
    errors = list(data.get("errors") or [])
    warnings = list(data.get("warnings") or [])
    return {"ok": not errors, "errors": errors, "warnings": warnings, "plan": data.get("plan") or None}


def guard(yaml_text: str, secret_keys: list[str] | None = None) -> dict:
    """校验不过直接 422（保存与运行都走这里）。"""
    result = compile_yaml(yaml_text, secret_keys)
    if not result["ok"]:
        raise ApiError(422, "lint_failed", "用例校验未通过",
                       {"errors": result["errors"], "warnings": result["warnings"]})
    return result


def brief(yaml_text: str) -> dict:
    """从 YAML 里取回元信息（容错：解析失败就返回空）。"""
    try:
        data = yaml.safe_load(yaml_text or "") or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    tags = data.get("tags")
    steps = data.get("steps")
    return {
        "id": str(data.get("id") or ""),
        "title": str(data.get("title") or ""),
        "priority": str(data.get("priority") or ""),
        "owner": str(data.get("owner") or ""),
        "env": str(data.get("env") or ""),
        "tags": [str(item) for item in tags] if isinstance(tags, list) else [],
        "steps": len(steps) if isinstance(steps, list) else 0,
    }


def draft(desc: str, url: str = "") -> dict:
    """内核 `case draft --desc … --url … --json`：自然语言 → 用例 YAML 草稿（不落库）。"""
    args = ["case", "draft", "--desc", desc, "--json"]
    if url:
        args += ["--url", url]
    try:
        data = kernel_client.call_json(args, timeout=300)
    except KernelError as exc:
        raise ApiError(502, exc.code, "内核草稿调用失败", exc.detail) from exc
    yaml_text = str(data.get("yaml") or data.get("yaml_text") or "")
    if not yaml_text.strip():
        raise ApiError(502, "kernel_bad_payload", "内核没有返回 YAML 草稿", {})
    return {"yaml_text": yaml_text, "warnings": list(data.get("warnings") or []),
            "meta": data.get("meta") or {}}


def import_script(script_text: str, *, code: str = "", title: str = "", priority: str = "P2",
                  owner: str = "", env_name: str = "", tags: list[str] | None = None,
                  keep_url: bool = False) -> dict:
    """老脚本（midscene.js）→ 用例 YAML 草稿（离线：不调模型、不起浏览器、不落库）。"""
    result = midscene.convert(script_text, code=code, title=title, priority=priority,
                              owner=owner, env_name=env_name, tags=tags, keep_url=keep_url)
    return {"yaml_text": result.yaml_text, "report": result.report.as_dict()}


def skeleton(code: str, name: str, priority: str, owner: str, env_name: str = "") -> str:
    head = (
        f"id: {yaml_scalar(code)}\n"
        f"title: {yaml_scalar(name)}\n"
        f"priority: {yaml_scalar(priority)}\n"
        "tags: []\n"
        f"owner: {yaml_scalar(owner)}\n"
        f"env: {yaml_scalar(env_name)}\n"
        "\n"
    )
    return head + SKELETON


def env_secret_keys(db: Session, env_name: str) -> list[str]:
    if not env_name:
        return []
    env = db.scalar(select(Env).where(Env.name == env_name))
    return list(env.secret_keys or []) if env else []


def save(db: Session, case: Case, yaml_text: str, expected_version: int, user, comment: str = "") -> dict:
    """乐观锁保存：版本不符 409；校验不过 422；通过则前滚一个新版本。"""
    if case.current_version != expected_version:
        raise ApiError(409, "version_conflict", "用例已被他人更新，请先对比版本再合并",
                       {"current_version": case.current_version, "your_version": expected_version})
    meta = brief(yaml_text)
    result = guard(yaml_text, env_secret_keys(db, meta.get("env", "")))
    version = case.current_version + 1
    db.add(CaseVersion(case_id=case.id, version=version, yaml_text=yaml_text,
                       comment=comment, author=actor(user)))
    case.yaml_text = yaml_text
    case.current_version = version
    case.updated_by = actor(user)
    case.updated_at = utcnow()
    if meta.get("title"):
        case.name = meta["title"]
    if meta.get("priority"):
        case.priority = meta["priority"]
    if meta.get("owner"):
        case.owner = meta["owner"]
    if meta.get("tags"):
        case.tags = meta["tags"]
    audit.log(db, user, "case.save", "case", case.code, {"version": version, "comment": comment})
    db.commit()
    return {"version": version, "warnings": result["warnings"], "plan": result["plan"],
            "meta": meta, "updated_at": case.updated_at.isoformat(timespec="seconds")}


def rollback(db: Session, case: Case, version: int, user) -> dict:
    row = db.scalar(select(CaseVersion).where(CaseVersion.case_id == case.id,
                                               CaseVersion.version == version))
    if row is None:
        raise not_found("用例版本", version)
    return save(db, case, row.yaml_text, case.current_version, user, comment=f"回滚到版本 {version}")


def _reidentify(yaml_text: str, code: str, name: str) -> str:
    """复制用例时换掉 id/title（解析不了就原样保留，交给校验报错）。"""
    try:
        data = yaml.safe_load(yaml_text or "")
    except yaml.YAMLError:
        return yaml_text
    if not isinstance(data, dict):
        return yaml_text
    data["id"] = code
    data["title"] = name
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)


def new_case(db: Session, scenario, name: str, priority: str | None = None, owner: str = "",
             template: str = "blank", copy_from: str | None = None, env_name: str = "") -> Case:
    """在场景下新建用例，同时落下 version=1（§8.3 模板：空白 / 复制现有）。"""
    from app.db import next_seq

    code = f"{scenario.code}-{next_seq(db, f'case:{scenario.code}'):02d}"
    owner = owner or scenario.owner or ""
    priority = priority or scenario.priority or "P2"
    yaml_text = skeleton(code, name, priority, owner, env_name)
    if template == "copy" and copy_from:
        source = db.scalar(select(Case).where(Case.code == copy_from, Case.deleted_at.is_(None)))
        if source is None:
            raise not_found("被复制的用例", copy_from)
        yaml_text = _reidentify(source.yaml_text, code, name)
    case = Case(code=code, scenario_id=scenario.id, name=name, priority=priority, owner=owner,
                yaml_text=yaml_text, current_version=1, updated_by=owner)
    db.add(case)
    db.flush()
    db.add(CaseVersion(case_id=case.id, version=1, yaml_text=yaml_text, comment="创建", author=owner))
    return case
