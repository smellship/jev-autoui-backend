"""场景套件：管理 + 有序用例 + 运行（场景管理）。"""

from __future__ import annotations

from fastapi import APIRouter, Response
from sqlalchemy import func, or_, select

from app.api.deps import CurrentUser, Db
from app.api.serializers import (
    batch_brief,
    case_count_by_suite,
    case_list_items,
    last_batch_by_suite,
    last_run_by_case,
    suite_brief,
)
from app.core.errors import ApiError, not_found
from app.models import Env, Suite, utcnow
from app.schemas import SuiteCasesIn, SuiteCopy, SuiteIn, SuitePatch, SuiteRunIn
from app.services import audit, run_service, suite_service

router = APIRouter(prefix="/suites", tags=["suites"])
PAGE_MAX = 200


@router.get("")
def list_suites(user: CurrentUser, db: Db, q: str = "", page: int = 1, size: int = 50) -> dict:
    size = max(1, min(size, PAGE_MAX))
    query = select(Suite).where(Suite.deleted_at.is_(None))
    if q:
        query = query.where(or_(Suite.name.contains(q), Suite.code.contains(q)))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(db.scalars(query.order_by(Suite.id).offset((max(1, page) - 1) * size)
                           .limit(size)).all())
    ids = [row.id for row in rows]
    counts = case_count_by_suite(db, ids)
    batches = last_batch_by_suite(db, ids)
    return {"items": [suite_brief(row, counts.get(row.id, 0), batches.get(row.id)) for row in rows],
            "total": total, "page": page, "size": size}


@router.post("", status_code=201)
def create_suite(body: SuiteIn, user: CurrentUser, db: Db) -> dict:
    suite = suite_service.create_suite(db, body.name, body.owner or user.name, body.tags,
                                       body.description, user)
    return suite_brief(suite)


@router.get("/{suite_id}")
def get_suite_detail(suite_id: str, user: CurrentUser, db: Db) -> dict:
    suite = suite_service.get_suite(db, suite_id)
    live, missing = suite_service.cases_of(db, suite.id)
    runs = last_run_by_case(db, [case.id for case in live])
    data = suite_brief(suite, len(live))
    data["items"] = case_list_items(db, live, runs)
    data["missing"] = [{"id": case.code, "name": case.name} for case in missing]
    return data


@router.patch("/{suite_id}")
def patch_suite(suite_id: str, body: SuitePatch, user: CurrentUser, db: Db) -> dict:
    suite = suite_service.get_suite(db, suite_id)
    for field in ("name", "owner", "description"):
        value = getattr(body, field)
        if value is not None:
            setattr(suite, field, value)
    if body.tags is not None:
        suite.tags = body.tags
    audit.log(db, user, "suite.update", "suite", suite.code, {"name": suite.name})
    db.commit()
    counts = case_count_by_suite(db, [suite.id])
    return suite_brief(suite, counts.get(suite.id, 0))


@router.delete("/{suite_id}", status_code=204)
def delete_suite(suite_id: str, user: CurrentUser, db: Db) -> Response:
    suite = suite_service.get_suite(db, suite_id)
    suite.deleted_at = utcnow()
    audit.log(db, user, "suite.delete", "suite", suite.code, {"name": suite.name})
    db.commit()
    return Response(status_code=204)


@router.post("/{suite_id}/copy", status_code=201)
def copy_suite(suite_id: str, user: CurrentUser, db: Db, body: SuiteCopy | None = None) -> dict:
    suite = suite_service.get_suite(db, suite_id)
    name = (body.name if body and body.name else "") or f"{suite.name} 副本"
    new_suite = suite_service.copy_suite(db, suite, name, user)
    counts = case_count_by_suite(db, [new_suite.id])
    return suite_brief(new_suite, counts.get(new_suite.id, 0))


@router.put("/{suite_id}/cases")
def set_suite_cases(suite_id: str, body: SuiteCasesIn, user: CurrentUser, db: Db) -> dict:
    suite = suite_service.get_suite(db, suite_id)
    count = suite_service.set_cases(db, suite, body.case_ids, user)
    return {"id": suite.code, "count": count}


@router.post("/{suite_id}/run", status_code=202)
def run_suite(suite_id: str, body: SuiteRunIn, user: CurrentUser, db: Db) -> dict:
    """运行套件 = 批量串跑：整批同一 batch_code 依次入队。"""
    suite = suite_service.get_suite(db, suite_id)
    env = db.get(Env, body.env_id)
    if env is None:
        raise not_found("环境", body.env_id)
    live, missing = suite_service.cases_of(db, suite.id)
    if missing:
        raise ApiError(409, "suite_has_missing", "套件里有已删除的用例，先修套件再运行",
                       {"cases": [case.code for case in missing]})
    if not live:
        raise ApiError(409, "suite_empty", "套件里还没有可用用例", {"suite": suite.code})
    batch_code, runs = run_service.create_batch(db, live, env, body.mode, user, suite=suite)
    return batch_brief(db, batch_code, runs)
