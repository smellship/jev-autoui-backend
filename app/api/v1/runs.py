"""运行：发起 / 台账 / SSE / 停止 / 产物 / 报告（§10/§11）。"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, Db
from app.api.serializers import run_brief, run_detail
from app.core.errors import ApiError, not_found
from app.core.paths import safe_join
from app.db import SessionLocal
from app.models import Case, Env, Run
from app.schemas import RunIn
from app.services import run_service

router = APIRouter(prefix="/runs", tags=["runs"])
PAGE_MAX = 200


def get_run(db: Session, code: str) -> Run:
    run = db.scalar(select(Run).where(Run.code == code))
    if run is None:
        raise not_found("运行", code)
    return run


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _parse_time(text: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ApiError(422, "bad_time", f"时间格式应为 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM:SS：{text}")


@router.post("", status_code=202)
def create_run(body: RunIn, user: CurrentUser, db: Db) -> dict:
    case = db.scalar(select(Case).where(Case.code == body.case_id, Case.deleted_at.is_(None)))
    if case is None:
        raise not_found("用例", body.case_id)
    env = db.get(Env, body.env_id)
    if env is None:
        raise not_found("环境", body.env_id)
    run = run_service.create_run(db, case, env, body.mode, user)
    return run_brief(run, run_service.queue_position(db, run))


@router.get("")
def list_runs(user: CurrentUser, db: Db, case_id: str = "", env_id: int | None = None,
              status: str = "", mode: str = "", date_from: str = "", date_to: str = "",
              page: int = 1, size: int = 50) -> dict:
    size = max(1, min(size, PAGE_MAX))
    query = select(Run)
    if case_id:
        query = query.where(Run.case_code == case_id)
    if env_id is not None:
        query = query.where(Run.env_id == env_id)
    if status:
        query = query.where(Run.status.in_([item for item in status.split(",") if item]))
    if mode:
        query = query.where(Run.mode == mode)
    if date_from:
        query = query.where(Run.created_at >= _parse_time(date_from))
    if date_to:
        query = query.where(Run.created_at <= _parse_time(date_to))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(query.order_by(Run.id.desc())
                      .offset((max(1, page) - 1) * size).limit(size)).all()
    return {"items": [run_brief(run) for run in rows], "total": total, "page": page, "size": size}


@router.get("/{run_id}")
def get_run_detail(run_id: str, user: CurrentUser, db: Db) -> dict:
    return run_detail(db, get_run(db, run_id))


@router.post("/{run_id}/stop")
def stop_run(run_id: str, user: CurrentUser, db: Db) -> dict:
    return run_service.stop(db, get_run(db, run_id), user)


@router.get("/{run_id}/report")
def get_report(run_id: str, user: CurrentUser, db: Db):
    run = get_run(db, run_id)
    path = safe_join(Path(run.artifact_dir or "."), "report.html")
    if not path.is_file():
        raise ApiError(404, "report_missing", "报告还没生成，可先『重出报告』",
                       {"run": run.code})
    return FileResponse(path, media_type="text/html")


@router.post("/{run_id}/report")
def rebuild_report(run_id: str, user: CurrentUser, db: Db) -> dict:
    return run_service.regenerate_report(db, get_run(db, run_id), user)


@router.get("/{run_id}/artifacts/{path:path}")
def get_artifact(run_id: str, path: str, user: CurrentUser, db: Db):
    run = get_run(db, run_id)
    full = safe_join(Path(run.artifact_dir or "."), path)
    if not full.is_file():
        raise not_found("产物", path)
    return FileResponse(full)


@router.get("/{run_id}/events")
def run_events(run_id: str, user: CurrentUser, db: Db) -> StreamingResponse:
    """SSE：status / trace / done 三类事件（§11）；trace 直接 tail 运行目录的 trace.jsonl。"""
    run = get_run(db, run_id)
    run_db_id, code, artifact_dir, total_hint = run.id, run.code, run.artifact_dir, run.steps

    def gen():
        trace_path = (Path(artifact_dir) / "trace.jsonl") if artifact_dir else None
        offset = 0
        last_key = None
        yield ": connected\n\n"
        while True:
            with SessionLocal() as session:
                current = session.get(Run, run_db_id)
                if current is None:
                    break
                status, total = current.status, current.steps
                detail, duration = current.detail, current.duration_ms
            step_no = None
            if trace_path is not None and trace_path.exists():
                with trace_path.open("rb") as handle:
                    handle.seek(offset)
                    for raw in handle:
                        if not raw.endswith(b"\n"):  # 半行：等下一次
                            break
                        offset += len(raw)
                        try:
                            row = json.loads(raw.decode("utf-8", errors="replace"))
                        except json.JSONDecodeError:
                            continue
                        if isinstance(row, dict):
                            step_no = row.get("step") or step_no
                            yield sse("trace", row)
            key = (status, step_no)
            if key != last_key:
                yield sse("status", {"run_id": code, "status": status, "step": step_no,
                                     "total": total or total_hint, "detail": detail})
                last_key = key
            if status in run_service.TERMINAL:
                yield sse("done", {"run_id": code, "status": status, "duration_ms": duration,
                                   "detail": detail})
                break
            time.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
