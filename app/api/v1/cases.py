"""用例详情页：校验 / 保存 / 自然语言草稿 / 版本 / 回滚 / 复制 / 删除（§8.4）。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, Db
from app.api.serializers import case_brief, iso, last_run_by_case
from app.core.config import get_settings
from app.core.errors import not_found
from app.models import Case, CaseVersion, Scenario, utcnow
from app.schemas import CaseCopy, CaseDraft, CaseImport, CaseRollback, CaseSave, CaseValidate
from app.services import audit, case_service

router = APIRouter(prefix="/cases", tags=["cases"])


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
                                     owner=case.owner, template="copy", copy_from=case.code)
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
