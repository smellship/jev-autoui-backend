"""响应形状（直接给 dict）。"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Case, Node, Run, Scenario, Suite, SuiteCase
from app.services import run_service


def iso(value) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def user_brief(user) -> dict:
    return {"id": user.id, "account": user.account, "name": user.name,
            "active": user.active, "is_admin": user.is_admin,
            "created_at": iso(user.created_at)}


def node_brief(node: Node) -> dict:
    return {"id": node.id, "parent_id": node.parent_id, "name": node.name,
            "sort": node.sort, "description": node.description}


def node_tree(nodes: list[Node]) -> list[dict]:
    items = {node.id: node_brief(node) | {"children": []} for node in nodes}
    roots: list[dict] = []
    for node in nodes:
        item = items[node.id]
        parent = items.get(node.parent_id) if node.parent_id else None
        (parent["children"] if parent else roots).append(item)
    return roots


def last_run_by_case(db: Session, case_ids: list[int]) -> dict[int, Run]:
    if not case_ids:
        return {}
    rows = db.scalars(select(Run).where(Run.case_id.in_(case_ids)).order_by(Run.id.desc())).all()
    out: dict[int, Run] = {}
    for run in rows:
        out.setdefault(run.case_id, run)
    return out


def last_run_by_scenario(db: Session, scenario_ids: list[int]) -> dict[int, Run]:
    if not scenario_ids:
        return {}
    rows = db.execute(
        select(Case.scenario_id, Run)
        .join(Run, Run.case_id == Case.id)
        .where(Case.scenario_id.in_(scenario_ids))
        .order_by(Run.id.desc())
    ).all()
    out: dict[int, Run] = {}
    for scenario_id, run in rows:
        out.setdefault(scenario_id, run)
    return out


def run_brief(run: Run, position: int | None = None) -> dict:
    return {
        "id": run.code, "case_id": run.case_code, "case_name": run.case_name,
        "scenario_name": run.scenario_name, "env": run.env_name, "mode": run.mode,
        "batch_code": run.batch_code or "",
        "status": run.status, "kernel_status": run.kernel_status, "detail": run.detail,
        "steps": run.steps, "created_at": iso(run.created_at), "started_at": iso(run.started_at),
        "finished_at": iso(run.finished_at), "duration_ms": run.duration_ms,
        "triggered_by": run.triggered_by, "queue_position": position,
        "report_url": f"/api/v1/runs/{run.code}/report",
        "events_url": f"/api/v1/runs/{run.code}/events",
    }


def scenario_brief(scenario: Scenario, case_count: int = 0, last_run: Run | None = None) -> dict:
    return {
        "id": scenario.code, "node_id": scenario.node_id, "name": scenario.name,
        "priority": scenario.priority, "owner": scenario.owner, "tags": scenario.tags or [],
        "description": scenario.description, "env_id": scenario.env_id,
        "case_count": case_count, "last_run": run_brief(last_run) if last_run else None,
        "created_by": scenario.created_by, "updated_by": scenario.updated_by,
        "updated_at": iso(scenario.updated_at),
    }


def case_brief(case: Case, last_run: Run | None = None, yaml_text: str | None = None) -> dict:
    data = {
        "id": case.code, "scenario_id": case.scenario_id, "name": case.name,
        "priority": case.priority, "tags": case.tags or [], "owner": case.owner,
        "version": case.current_version,
        "created_by": case.created_by, "updated_by": case.updated_by,
        "updated_at": iso(case.updated_at),
        "last_run": run_brief(last_run) if last_run else None,
    }
    if yaml_text is not None:
        data["yaml_text"] = yaml_text
    return data


ARTIFACT_KINDS = {
    "report.html": "report", "trace.jsonl": "trace", "summary.json": "summary",
    "actions.json": "actions", "plan.json": "plan", "kernel.log": "log",
}


def artifact_kind(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    if rel.startswith("screenshots/"):
        return "screenshot"
    if "/snapshots/" in rel or rel.startswith("snapshots/"):
        return "snapshot"
    return ARTIFACT_KINDS.get(name, "other")


def artifacts_of(run: Run, limit: int = 500) -> list[dict]:
    root = Path(run.artifact_dir) if run.artifact_dir else None
    if not root or not root.exists():
        return []
    items: list[dict] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            continue
        items.append({"path": rel, "size": size, "kind": artifact_kind(rel),
                      "url": f"/api/v1/runs/{run.code}/artifacts/{rel}"})
        if len(items) >= limit:
            break
    return items


def run_detail(db: Session, run: Run) -> dict:
    summary = run_service.read_summary(run.artifact_dir)
    data = run_brief(run, run_service.queue_position(db, run))
    data["checks"] = run.checks or []
    data["steps_detail"] = summary.get("steps") or []
    data["defect_candidates"] = summary.get("defect_candidates") or []
    data["plan"] = json.loads(run.plan_json) if run.plan_json else None
    data["artifacts"] = artifacts_of(run)
    data["report_exists"] = bool(run.artifact_dir) and (Path(run.artifact_dir) / "report.html").exists()
    return data


def scenario_cases(db: Session, scenario_id: int) -> list[dict]:
    cases = db.scalars(select(Case).where(Case.scenario_id == scenario_id,
                                          Case.deleted_at.is_(None)).order_by(Case.id)).all()
    runs = last_run_by_case(db, [case.id for case in cases])
    return [case_brief(case, runs.get(case.id)) for case in cases]


def case_count_by_scenario(db: Session, scenario_ids: list[int]) -> dict[int, int]:
    if not scenario_ids:
        return {}
    rows = db.execute(select(Case.scenario_id, func.count()).where(
        Case.deleted_at.is_(None), Case.scenario_id.in_(scenario_ids)).group_by(Case.scenario_id)).all()
    return {scenario_id: count for scenario_id, count in rows}


def case_count_by_suite(db: Session, suite_ids: list[int]) -> dict[int, int]:
    if not suite_ids:
        return {}
    rows = db.execute(select(SuiteCase.suite_id, func.count())
                      .join(Case, Case.id == SuiteCase.case_id)
                      .where(SuiteCase.suite_id.in_(suite_ids), Case.deleted_at.is_(None))
                      .group_by(SuiteCase.suite_id)).all()
    return {suite_id: count for suite_id, count in rows}


def case_list_items(db: Session, cases: list[Case], runs: dict[int, Run]) -> list[dict]:
    """用例列表项：case_brief + 场景名（选择器 / 套件面板共用）。"""
    scenario_ids = {case.scenario_id for case in cases}
    scenarios = (db.scalars(select(Scenario).where(Scenario.id.in_(scenario_ids))).all()
                 if scenario_ids else [])
    by_id = {row.id: row for row in scenarios}
    items = []
    for case in cases:
        scenario = by_id.get(case.scenario_id)
        items.append(case_brief(case, runs.get(case.id)) | {
            "scenario_code": scenario.code if scenario else "",
            "scenario_name": scenario.name if scenario else ""})
    return items


def suite_brief(suite: Suite, case_count: int = 0, last_batch: dict | None = None) -> dict:
    return {
        "id": suite.code, "name": suite.name, "owner": suite.owner, "tags": suite.tags or [],
        "description": suite.description, "case_count": case_count, "last_batch": last_batch,
        "created_at": iso(suite.created_at), "updated_at": iso(suite.updated_at),
    }


def batch_counts(runs: list[Run]) -> dict:
    counts = {name: 0 for name in ("queued", "running", "passed", "failed", "aborted",
                                   "unverified", "error")}
    for run in runs:
        counts[run.status] = counts.get(run.status, 0) + 1
    return counts


def batch_summary(db: Session, code: str, runs: list[Run]) -> dict:
    suite = db.get(Suite, runs[0].suite_id) if runs[0].suite_id else None
    first = runs[0]
    return {
        "code": code, "label": suite.name if suite else "临时串跑",
        "suite_id": suite.code if suite else None,
        "env": first.env_name, "mode": first.mode, "triggered_by": first.triggered_by,
        "created_at": iso(first.created_at), "total": len(runs),
        "done": all(run.status in run_service.TERMINAL for run in runs),
        "counts": batch_counts(runs),
    }


def batch_brief(db: Session, code: str, runs: list[Run]) -> dict:
    data = batch_summary(db, code, runs)
    current = next((run for run in runs if run.status == "running"), None)
    data["current"] = run_brief(current) if current else None
    data["items"] = [run_brief(run) for run in runs]
    return data


def last_batch_by_suite(db: Session, suite_ids: list[int]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for suite_id in suite_ids:
        last = db.scalar(select(Run).where(Run.suite_id == suite_id, Run.batch_code.is_not(None))
                         .order_by(Run.id.desc()))
        if last is None or not last.batch_code:
            continue
        runs = list(db.scalars(select(Run).where(Run.batch_code == last.batch_code)
                               .order_by(Run.id)).all())
        out[suite_id] = batch_summary(db, last.batch_code, runs)
    return out


def node_codes(nodes: list[Node]) -> dict[int, Node]:
    return {node.id: node for node in nodes}
