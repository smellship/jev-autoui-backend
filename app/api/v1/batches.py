"""批次：即席串跑创建 + 进度 + 停止（场景管理）。"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import CurrentUser, Db
from app.api.serializers import batch_brief
from app.core.errors import ApiError, not_found
from app.models import Case, Env, Run
from app.schemas import BatchIn
from app.services import run_service

router = APIRouter(prefix="/batches", tags=["batches"])


@router.post("", status_code=202)
def create_batch(body: BatchIn, user: CurrentUser, db: Db) -> dict:
    """即席串跑：任意有序用例 → 同一批次依次入队。"""
    env = db.get(Env, body.env_id)
    if env is None:
        raise not_found("环境", body.env_id)
    seen: set[str] = set()
    codes: list[str] = []
    for code in body.case_ids:
        if code not in seen:
            seen.add(code)
            codes.append(code)
    rows = db.scalars(select(Case).where(Case.code.in_(codes))).all()
    by_code = {case.code: case for case in rows}
    invalid = [code for code in codes if code not in by_code or by_code[code].deleted_at is not None]
    if invalid:
        raise ApiError(409, "case_invalid", "有用例不存在或已删除，批次未创建", {"cases": invalid})
    cases = [by_code[code] for code in codes]
    batch_code, runs = run_service.create_batch(db, cases, env, body.mode, user)
    return batch_brief(db, batch_code, runs)


@router.get("/{batch_code}")
def get_batch(batch_code: str, user: CurrentUser, db: Db) -> dict:
    runs = list(db.scalars(select(Run).where(Run.batch_code == batch_code).order_by(Run.id)).all())
    if not runs:
        raise not_found("批次", batch_code)
    return batch_brief(db, batch_code, runs)


@router.post("/{batch_code}/stop")
def stop_batch(batch_code: str, user: CurrentUser, db: Db) -> dict:
    return run_service.stop_batch(db, batch_code, user)
