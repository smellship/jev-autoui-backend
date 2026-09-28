"""场景与场景下用例（§8.2/§8.3）。"""

from __future__ import annotations

from fastapi import APIRouter, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, Db
from app.api.serializers import (
    case_brief,
    case_count_by_scenario,
    last_run_by_case,
    last_run_by_scenario,
    scenario_brief,
)
from app.core.errors import ApiError, not_found
from app.db import next_seq
from app.models import Case, Env, Node, Scenario, utcnow
from app.schemas import CaseIn, ScenarioIn, ScenarioPatch
from app.services import audit, case_service

router = APIRouter(prefix="/scenarios", tags=["scenarios"])
PAGE_MAX = 200


def get_scenario(db: Session, code: str) -> Scenario:
    scenario = db.scalar(select(Scenario).where(Scenario.code == code, Scenario.deleted_at.is_(None)))
    if scenario is None:
        raise not_found("场景", code)
    return scenario


def _env_name(db: Session, env_id: int | None) -> str:
    if not env_id:
        return ""
    env = db.get(Env, env_id)
    return env.name if env else ""


@router.get("")
def list_scenarios(user: CurrentUser, db: Db, node_id: int | None = None, q: str = "",
                   page: int = 1, size: int = 50) -> dict:
    size = max(1, min(size, PAGE_MAX))
    query = select(Scenario).where(Scenario.deleted_at.is_(None))
    if node_id is not None:
        query = query.where(Scenario.node_id == node_id)
    if q:
        query = query.where(or_(Scenario.name.contains(q), Scenario.code.contains(q)))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(db.scalars(query.order_by(Scenario.id).offset((max(1, page) - 1) * size).limit(size)).all())
    ids = [row.id for row in rows]
    counts = case_count_by_scenario(db, ids)
    runs = last_run_by_scenario(db, ids)
    return {"items": [scenario_brief(row, counts.get(row.id, 0), runs.get(row.id)) for row in rows],
            "total": total, "page": page, "size": size}


@router.post("", status_code=201)
def create_scenario(body: ScenarioIn, user: CurrentUser, db: Db) -> dict:
    node = db.get(Node, body.node_id)
    if node is None or node.deleted_at is not None:
        raise not_found("节点", body.node_id)
    if body.env_id is not None and db.get(Env, body.env_id) is None:
        raise not_found("环境", body.env_id)
    scenario = Scenario(code=f"sc-{next_seq(db, 'scenario'):04d}", node_id=body.node_id,
                        name=body.name, priority=body.priority, owner=body.owner or user.name,
                        tags=body.tags, description=body.description, env_id=body.env_id)
    db.add(scenario)
    db.flush()
    audit.log(db, user, "scenario.create", "scenario", scenario.code, {"name": scenario.name})
    db.commit()
    return scenario_brief(scenario)


@router.get("/{scenario_id}")
def get_scenario_detail(scenario_id: str, user: CurrentUser, db: Db) -> dict:
    scenario = get_scenario(db, scenario_id)
    counts = case_count_by_scenario(db, [scenario.id])
    runs = last_run_by_scenario(db, [scenario.id])
    return scenario_brief(scenario, counts.get(scenario.id, 0), runs.get(scenario.id))


@router.patch("/{scenario_id}")
def patch_scenario(scenario_id: str, body: ScenarioPatch, user: CurrentUser, db: Db) -> dict:
    scenario = get_scenario(db, scenario_id)
    if body.node_id is not None:
        node = db.get(Node, body.node_id)
        if node is None or node.deleted_at is not None:
            raise not_found("节点", body.node_id)
        scenario.node_id = body.node_id
    if body.env_id is not None:
        if body.env_id and db.get(Env, body.env_id) is None:
            raise not_found("环境", body.env_id)
        scenario.env_id = body.env_id or None
    for field in ("name", "priority", "owner", "description"):
        value = getattr(body, field)
        if value is not None:
            setattr(scenario, field, value)
    if body.tags is not None:
        scenario.tags = body.tags
    audit.log(db, user, "scenario.update", "scenario", scenario.code, {"name": scenario.name})
    db.commit()
    return scenario_brief(scenario)


@router.delete("/{scenario_id}", status_code=204)
def delete_scenario(scenario_id: str, user: CurrentUser, db: Db, cascade: bool = False) -> Response:
    scenario = get_scenario(db, scenario_id)
    cases = list(db.scalars(select(Case).where(Case.scenario_id == scenario.id,
                                                Case.deleted_at.is_(None))).all())
    if cases and not cascade:
        raise ApiError(409, "scenario_not_empty", "该场景下还有用例，删用例或带 cascade=true 一起软删除",
                       {"cases": [case.code for case in cases]})
    now = utcnow()
    for case in cases:
        case.deleted_at = now
    scenario.deleted_at = now
    audit.log(db, user, "scenario.delete", "scenario", scenario.code,
              {"name": scenario.name, "cases": len(cases), "cascade": cascade})
    db.commit()
    return Response(status_code=204)


@router.get("/{scenario_id}/cases")
def list_cases(scenario_id: str, user: CurrentUser, db: Db) -> dict:
    scenario = get_scenario(db, scenario_id)
    cases = list(db.scalars(select(Case).where(Case.scenario_id == scenario.id,
                                               Case.deleted_at.is_(None)).order_by(Case.id)).all())
    runs = last_run_by_case(db, [case.id for case in cases])
    return {"items": [case_brief(case, runs.get(case.id)) for case in cases], "total": len(cases)}


@router.post("/{scenario_id}/cases", status_code=201)
def create_case(scenario_id: str, body: CaseIn, user: CurrentUser, db: Db) -> dict:
    scenario = get_scenario(db, scenario_id)
    case = case_service.new_case(db, scenario, name=body.name, priority=body.priority,
                                 owner=body.owner or user.name, template=body.template,
                                 copy_from=body.copy_from, env_name=_env_name(db, scenario.env_id))
    audit.log(db, user, "case.create", "case", case.code, {"name": case.name, "scenario": scenario.code})
    db.commit()
    return case_brief(case, yaml_text=case.yaml_text)
