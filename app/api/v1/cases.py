"""用例详情页：校验 / 保存 / 自然语言草稿 / 版本 / 回滚 / 复制 / 删除（§8.4）。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, Db
from app.api.serializers import case_brief, case_list_items, iso, last_run_by_case
from app.core.config import get_settings
from app.core.errors import ApiError, not_found
from app.models import Case, CaseVersion, Node, Scenario, utcnow
from app.schemas import CaseCopy, CaseDraft, CaseImport, CasePatch, CaseRollback, CaseSave, CaseValidate
from app.services import audit, case_service

router = APIRouter(prefix="/cases", tags=["cases"])
PAGE_MAX = 200


def get_case(db: Session, code: str) -> Case:
    case = db.scalar(select(Case).where(Case.code == code, Case.deleted_at.is_(None)))
    if case is None:
        raise not_found("用例", code)
    return case


def _env_name(db: Session, scenario: Scenario | None) -> str:
    if scenario is None or not scenario.env_id:
        return ""
    from app.models import Env

    env = db.get(Env, scenario.env_id)
    return env.name if env else ""


def _descendant_node_ids(db: Session, node_id: int) -> list[int]:
    """节点筛选含子孙：点父节点也能看到下面各级场景里的用例。"""
    nodes = db.scalars(select(Node).where(Node.deleted_at.is_(None))).all()
    children: dict[int | None, list[int]] = {}
    for node in nodes:
        children.setdefault(node.parent_id, []).append(node.id)
    out: list[int] = []
    stack = [node_id]
    while stack:
        current = stack.pop()
        out.append(current)
        stack.extend(children.get(current, []))
    return out


@router.get("")
def list_cases(user: CurrentUser, db: Db, node_id: int | None = None, scenario_id: str = "",
               q: str = "", page: int = 1, size: int = 50) -> dict:
    """全局用例列表（场景管理的用例池选择器）：跨场景，可按节点/场景/关键字过滤。"""
    size = max(1, min(size, PAGE_MAX))
    query = select(Case).where(Case.deleted_at.is_(None))
    if scenario_id:
        scenario = db.scalar(select(Scenario).where(Scenario.code == scenario_id,
                                                    Scenario.deleted_at.is_(None)))
        if scenario is None:
            raise not_found("场景", scenario_id)
        query = query.where(Case.scenario_id == scenario.id)
    if node_id is not None:
        query = query.where(Case.scenario_id.in_(
            select(Scenario.id).where(Scenario.node_id.in_(_descendant_node_ids(db, node_id)),
                                      Scenario.deleted_at.is_(None))))
    if q:
        query = query.where(or_(Case.name.contains(q), Case.code.contains(q)))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(db.scalars(query.order_by(Case.id.desc())
                           .offset((max(1, page) - 1) * size).limit(size)).all())
    runs = last_run_by_case(db, [case.id for case in rows])
    return {"items": case_list_items(db, rows, runs), "total": total, "page": page, "size": size}


@router.get("/{case_id}")
def get_case_detail(case_id: str, user: CurrentUser, db: Db) -> dict:
    case = get_case(db, case_id)
    scenario = db.get(Scenario, case.scenario_id)
    runs = last_run_by_case(db, [case.id])
    data = case_brief(case, runs.get(case.id), yaml_text=case.yaml_text)
    data["scenario_code"] = scenario.code if scenario else ""
    data["scenario_name"] = scenario.name if scenario else ""
    return data


@router.put("/{case_id}")
def save_case(case_id: str, body: CaseSave, user: CurrentUser, db: Db) -> dict:
    case = get_case(db, case_id)
    result = case_service.save(db, case, body.yaml_text, body.version, user, body.comment)
    return {"id": case.code, "name": case.name, **result}


MUTEX_LEVELS = ("none", "env", "account")


@router.patch("/{case_id}")
def patch_case(case_id: str, body: CasePatch, user: CurrentUser, db: Db) -> dict:
    """用例元信息修改：目前只有互斥挡（none | env | account）。"""
    case = get_case(db, case_id)
    if body.mutex is None:
        raise ApiError(422, "nothing_to_patch", "没有要修改的字段", {})
    if body.mutex not in MUTEX_LEVELS:
        raise ApiError(422, "bad_mutex", f"互斥挡只能是 {'/'.join(MUTEX_LEVELS)}", {"mutex": body.mutex})
    before = case.mutex or "none"
    if body.mutex != before:
        case.mutex = body.mutex
        case.updated_by = case_service.actor(user) or case.owner
        audit.log(db, user, "case.mutex", "case", case.code, {"from": before, "to": body.mutex})
        db.commit()
    return case_brief(case, yaml_text=case.yaml_text)


@router.post("/{case_id}/validate")
def validate_case(case_id: str, user: CurrentUser, db: Db, body: CaseValidate | None = None) -> dict:
    """校验 + 返回编译后的 TestPlan（始终 200，用 ok/errors 表达结果，便于编辑器逐行标红）。"""
    case = get_case(db, case_id)
    yaml_text = body.yaml_text if body and body.yaml_text else case.yaml_text
    env_name = case_service.brief(yaml_text).get("env") or ""
    result = case_service.compile_yaml(yaml_text, case_service.env_secret_keys(db, env_name))
    result["env"] = env_name
    return result


@router.post("/{case_id}/drafts")
def draft_case(case_id: str, body: CaseDraft, user: CurrentUser, db: Db) -> dict:
    """自然语言 → YAML 草稿（不落库、不自动运行）。"""
    case = get_case(db, case_id)
    result = case_service.draft(body.desc, body.url)
    return {"id": case.code, **result}


@router.post("/{case_id}/imports")
def import_case(case_id: str, body: CaseImport, user: CurrentUser, db: Db) -> dict:
    """老脚本（midscene.js）→ YAML 草稿（离线转换：不调模型、不起浏览器、不落库）。"""
    case = get_case(db, case_id)
    scenario = db.get(Scenario, case.scenario_id)
    result = case_service.import_script(
        body.script_text, code=case.code, title=case.name, priority=case.priority,
        owner=case.owner, env_name=_env_name(db, scenario), tags=list(case.tags or []),
        keep_url=body.keep_url)
    return {"id": case.code, **result}


@router.get("/{case_id}/versions")
def list_versions(case_id: str, user: CurrentUser, db: Db) -> dict:
    case = get_case(db, case_id)
    rows = db.scalars(select(CaseVersion).where(CaseVersion.case_id == case.id)
                      .order_by(CaseVersion.version.desc())).all()
    return {"current_version": case.current_version,
            "items": [{"version": row.version, "comment": row.comment, "author": row.author,
                       "created_at": iso(row.created_at), "size": len(row.yaml_text or "")}
                      for row in rows],
            "total": len(rows)}


@router.get("/{case_id}/versions/{version}")
def get_version(case_id: str, version: int, user: CurrentUser, db: Db) -> dict:
    case = get_case(db, case_id)
    row = db.scalar(select(CaseVersion).where(CaseVersion.case_id == case.id,
                                              CaseVersion.version == version))
    if row is None:
        raise not_found("用例版本", version)
    return {"version": row.version, "yaml_text": row.yaml_text, "comment": row.comment,
            "author": row.author, "created_at": iso(row.created_at)}


@router.post("/{case_id}/rollback")
def rollback_case(case_id: str, body: CaseRollback, user: CurrentUser, db: Db) -> dict:
    case = get_case(db, case_id)
    result = case_service.rollback(db, case, body.version, user)
    return {"id": case.code, "name": case.name, **result}


@router.post("/{case_id}/copy", status_code=201)
def copy_case(case_id: str, user: CurrentUser, db: Db, body: CaseCopy | None = None) -> dict:
    case = get_case(db, case_id)
    scenario = db.get(Scenario, case.scenario_id)
    if body and body.scenario_id:
        scenario = db.scalar(select(Scenario).where(Scenario.code == body.scenario_id,
                                                    Scenario.deleted_at.is_(None)))
        if scenario is None:
            raise not_found("目标场景", body.scenario_id)
    name = (body.name if body and body.name else "") or f"{case.name} 副本"
    new_case = case_service.new_case(db, scenario, name=name, priority=case.priority,
                                     owner=case.owner, template="copy", copy_from=case.code,
                                     user=user)
    audit.log(db, user, "case.copy", "case", new_case.code, {"from": case.code})
    db.commit()
    return case_brief(new_case, yaml_text=new_case.yaml_text)


@router.delete("/{case_id}", status_code=204)
def delete_case(case_id: str, user: CurrentUser, db: Db) -> Response:
    case = get_case(db, case_id)
    trash = get_settings().artifacts_dir / "trash"
    trash.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (trash / f"case-{case.code}-{stamp}.yaml").write_text(case.yaml_text or "", encoding="utf-8")
    case.deleted_at = utcnow()
    audit.log(db, user, "case.delete", "case", case.code, {"name": case.name})
    db.commit()
    return Response(status_code=204)
