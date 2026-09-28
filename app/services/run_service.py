"""运行服务：服务端串行队列 + 内核子进程 + 状态映射 + 页清单（§10/§7）。"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from queue import Queue

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ApiError
from app.db import session_scope
from app.models import Case, Env, PageInventory, Run, Scenario, utcnow
from app.services import audit, case_service, secret_store
from app.services.kernel_client import KernelError, KernelProcess

ACTIVE = ("queued", "running")
TERMINAL = ("passed", "failed", "aborted", "unverified", "error")
STATUS_MAP = {
    "ok": "passed",
    "done_unverified": "unverified",
    "check_failed": "failed",
    "blocked": "failed",
    "stuck": "failed",
    "budget": "failed",
    "replanned": "failed",
    "skipped": "failed",
    "aborted": "aborted",
    "error": "error",
}


def actor(user) -> str:
    return getattr(user, "name", "") or getattr(user, "account", "") or ""


def slug(text: str, limit: int = 24) -> str:
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in (text or "")).strip("_")
    return cleaned[:limit] or "case"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


class RunQueue:
    """服务端串行队列（默认并发 1，可配 2）。"""

    def __init__(self, concurrency: int = 1):
        self.concurrency = max(1, int(concurrency))
        self._q: Queue[int | None] = Queue()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for index in range(self.concurrency):
            thread = threading.Thread(target=self._worker, name=f"run-worker-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for _ in self._threads:
            self._q.put(None)
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()

    def submit(self, run_id: int) -> None:
        self._q.put(run_id)

    def pending(self) -> int:
        return self._q.qsize()

    def _worker(self) -> None:
        while not self._stop.is_set():
            item = self._q.get()
            if item is None:
                break
            try:
                execute(item)
            except Exception as exc:  # 队列不能因单次异常死掉
                _fail(item, f"{exc.__class__.__name__}: {exc}")


queue = RunQueue()
_procs: dict[int, KernelProcess] = {}
_lock = threading.Lock()


def _fail(run_id: int, detail: str) -> None:
    with session_scope() as db:
        db.execute(update(Run).where(Run.id == run_id, Run.status.in_(ACTIVE))
                   .values(status="error", detail=detail, finished_at=utcnow())
                   .execution_options(synchronize_session=False))


def _run_status(run_id: int) -> str:
    with session_scope() as db:
        return db.scalar(select(Run.status).where(Run.id == run_id)) or ""


def _unique_code(db: Session, case: Case) -> str:
    base = f"r-{datetime.now():%Y%m%d-%H%M%S}-{slug(case.name)}"
    code, n = base, 1
    while db.scalar(select(Run).where(Run.code == code)) is not None:
        n += 1
        code = f"{base}-{n}"
    return code


def scenario_name(db: Session, case: Case) -> str:
    scenario = db.get(Scenario, case.scenario_id)
    return scenario.name if scenario else ""


def create_run(db: Session, case: Case, env: Env, mode: str, user) -> Run:
    """校验 → 解析密钥 → 落 run 台账（queued）→ 入队。"""
    if mode not in ("debug", "ci"):
        raise ApiError(422, "bad_mode", "mode 只能是 debug 或 ci", {"mode": mode})
    base = secret_store.base_url(env)
    if not base:
        raise ApiError(409, "env_unconfigured", f"环境「{env.name}」还没有配置地址，请到环境管理补齐",
                       {"env": env.name})
    result = case_service.guard(case.yaml_text, list(env.secret_keys or []))
    plan = dict(result["plan"] or {})
    plan.setdefault("url", base)
    _, missing = secret_store.resolve_vars(plan.get("vars") or {}, env, env.name)
    if missing:
        raise ApiError(409, "var_unresolved", "用例引用的密钥/环境变量在服务器上不存在",
                       {"missing": missing, "env": env.name})
    code = _unique_code(db, case)
    run = Run(
        code=code, case_id=case.id, env_id=env.id, case_code=case.code, case_name=case.name,
        scenario_name=scenario_name(db, case), env_name=env.name, mode=mode, status="queued",
        steps=len(plan.get("steps") or []), plan_json=json.dumps(plan, ensure_ascii=False),
        artifact_dir=str(get_settings().runs_dir / code), triggered_by=actor(user),
    )
    db.add(run)
    db.flush()
    audit.log(db, user, "run.create", "run", run.code,
              {"case": case.code, "env": env.name, "mode": mode})
    db.commit()
    queue.submit(run.id)
    return run


def queue_position(db: Session, run: Run) -> int | None:
    if run.status != "queued":
        return None
    ahead = db.scalar(select(func.count()).select_from(Run).where(Run.status == "queued", Run.id < run.id)) or 0
    return int(ahead) + 1


def execute(run_id: int) -> None:
    settings = get_settings()
    with session_scope() as db:
        run = db.get(Run, run_id)
        if run is None or run.status != "queued":
            return
        case = db.get(Case, run.case_id)
        env = db.get(Env, run.env_id) if run.env_id else None
        if case is None or env is None:
            run.status = "error"
            run.detail = "用例或环境已被删除，运行未执行"
            run.finished_at = utcnow()
            return
        base = secret_store.base_url(env)
        plan = json.loads(run.plan_json or "{}")
        vars_map, _ = secret_store.resolve_vars(plan.get("vars") or {}, env, env.name)
        artifact_dir = Path(run.artifact_dir)
        # 认领只能从 queued 走到 running：排队期间被停止（aborted 已落库）时 rowcount 为 0
        claimed = db.execute(update(Run).where(Run.id == run_id, Run.status == "queued")
                             .values(status="running", started_at=utcnow())
                             .execution_options(synchronize_session=False))
        if claimed.rowcount == 0:
            return
        mode = run.mode

    artifact_dir.mkdir(parents=True, exist_ok=True)
    plan_path = artifact_dir / "plan.json"
    plan_path.write_text(json.dumps({
        "name": plan.get("name") or case.name, "url": base, "steps": plan.get("steps") or [],
        "vars": plan.get("vars") or {}, "notes": plan.get("notes") or "",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    args = ["run", "--plan-file", str(plan_path), "--run-dir", str(artifact_dir),
            "--mode", mode, "--url", base, "--json"]
    for key, value in sorted(vars_map.items()):
        args += ["--var", f"{key}={value}"]

    started = time.time()
    proc = KernelProcess(args, artifact_dir / "kernel.log")
    with _lock:
        _procs[run_id] = proc
    if _run_status(run_id) != "running":
        proc.kill()  # 停止请求早于进程登记：补一刀，别让它白跑
    try:
        proc.wait(settings.kernel_run_timeout_s)
        try:
            payload = proc.result()
        except KernelError as exc:
            payload = {"error": exc.message, **exc.detail}
    finally:
        proc.close()
        with _lock:
            _procs.pop(run_id, None)

    summary = _read_json(artifact_dir / "summary.json")
    with session_scope() as db:
        run = db.get(Run, run_id)
        if run is None:
            return
        steps = summary.get("steps") or []
        kernel_status = str(summary.get("status") or payload.get("status") or "")
        duration = int((time.time() - started) * 1000)
        status = STATUS_MAP.get(kernel_status, "failed")
        detail = str(summary.get("detail") or payload.get("detail") or payload.get("error") or "")
        if proc.killed:
            status = "aborted"
            detail = "运行被终止（子进程树已杀），产物可能不完整（partial）"
        # 终态只从 running 落：用户停止（aborted）已经写过状态，不能被这里盖掉
        db.execute(update(Run).where(Run.id == run_id, Run.status == "running")
                   .values(status=status, kernel_status=kernel_status, detail=detail,
                           finished_at=utcnow(), duration_ms=duration)
                   .execution_options(synchronize_session=False))
        db.execute(update(Run).where(Run.id == run_id)
                   .values(steps=len(steps) or run.steps, checks=flatten_checks(steps))
                   .execution_options(synchronize_session=False))
        record_pages(db, run, artifact_dir)


def flatten_checks(steps: list) -> list[dict]:
    out: list[dict] = []
    for step in steps or []:
        for check in step.get("checks") or []:
            out.append({"step": step.get("index"), "kind": check.get("kind"),
                        "expected": check.get("expected"), "ok": bool(check.get("ok")),
                        "evidence": check.get("evidence") or ""})
    return out


def record_pages(db: Session, run: Run, artifact_dir: Path) -> None:
    """运行后把访问过的 URL + 标题登记进页清单（§7 ②）。"""
    trace = artifact_dir / "trace.jsonl"
    if run.env_id is None or not trace.exists():
        return
    seen: dict[str, str] = {}
    for line in trace.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        url = str(row.get("url") or "").strip()
        if url:
            seen[url] = str(row.get("title") or "")
    for url, title in seen.items():
        page = db.scalar(select(PageInventory).where(PageInventory.env_id == run.env_id,
                                                    PageInventory.url == url))
        if page is None:
            db.add(PageInventory(env_id=run.env_id, url=url, title=title, needs_login=None,
                                 source_case_id=run.case_id))
        else:
            page.title = title or page.title
            page.last_seen = utcnow()


def read_summary(artifact_dir: str) -> dict:
    return _read_json(Path(artifact_dir) / "summary.json") if artifact_dir else {}


def stop(db: Session, run: Run, user=None) -> dict:
    with _lock:
        proc = _procs.get(run.id)
    duration = run.duration_ms
    if run.started_at:
        duration = int((utcnow() - run.started_at).total_seconds() * 1000)
    detail = "用户停止：子进程树已终止，产物可能不完整（partial）"
    # 终态只从 queued/running 落：与 worker 收尾撞上时，先落库的一方说了算
    changed = db.execute(update(Run).where(Run.id == run.id, Run.status.in_(ACTIVE))
                         .values(status="aborted", detail=detail, finished_at=utcnow(),
                                 duration_ms=duration)
                         .execution_options(synchronize_session=False))
    if changed.rowcount == 0:
        current = _run_status(run.id)
        raise ApiError(409, "run_not_active", f"运行已是终态：{current}", {"status": current})
    audit.log(db, user, "run.stop", "run", run.code, {})
    db.commit()  # 先把 aborted 落库再杀进程：worker 被唤醒后就不会把状态盖回 failed
    if proc is not None:
        proc.kill()
    return {"id": run.code, "status": "aborted", "detail": detail}


def regenerate_report(db: Session, run: Run, user=None) -> dict:
    from app.services import kernel_client

    if not run.artifact_dir or not Path(run.artifact_dir).exists():
        raise ApiError(409, "no_artifacts", "这次运行没有产物目录，无法重出报告", {"run": run.code})
    try:
        data = kernel_client.call_json(["report", "--run-dir", run.artifact_dir, "--json"])
    except KernelError as exc:
        raise ApiError(502, exc.code, "内核重出报告失败", exc.detail) from exc
    report = Path(run.artifact_dir) / "report.html"
    audit.log(db, user, "run.report", "run", run.code, {})
    db.commit()
    return {"report": str(data.get("report") or report), "exists": report.exists()}
