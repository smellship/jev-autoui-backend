"""运行服务：并发调度器 + 内核子进程 + 状态映射 + 页清单（§10/§7）。"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ApiError, not_found
from app.db import session_scope
from app.models import Case, Env, PageInventory, Run, Scenario, Suite, utcnow
from app.services import audit, case_service, run_policy, run_settings, secret_store
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


def mutex_conflict(cand: dict, running: dict) -> bool:
    """双向互斥命中：候选与运行中任一挡位命中就得排队。

    挡位用生效挡（账号挡无键已降级为环境挡）：
    ①/③ 任一方是环境挡且同环境；②/④ 任一方是账号挡且键相等（键里含 env，等键即同环境同账号）。
    挡=none 的 run 也存账号键：互斥用例入队时，前面同键在跑的非互斥用例也让它等。
    """
    c_eff = effective_mutex(cand["mutex"], cand["mutex_key"])
    r_eff = effective_mutex(running["mutex"], running["mutex_key"])
    if cand["env_id"] == running["env_id"] and (c_eff == "env" or r_eff == "env"):
        return True
    same_key = cand["mutex_key"] is not None and cand["mutex_key"] == running["mutex_key"]
    return same_key and (c_eff == "account" or r_eff == "account")


def eligible(cand: dict, running: list[dict], conf: dict) -> bool:
    """派发条件：全局在跑 < max 且 该用户在跑 < k 且 与任一在跑者互斥不命中。"""
    if len(running) >= conf["max"]:
        return False
    if sum(1 for row in running if row["user_id"] == cand["user_id"]) >= conf["k"]:
        return False
    return not any(mutex_conflict(cand, row) for row in running)


def profile_dir_for(user_id: int | None, lane: int) -> Path:
    """登录态 profile 目录：按 (人, 路) 两级分，并发的两路各用独立目录绕开 Chromium 单实例锁。"""
    return get_settings().artifacts_dir / "chrome-profiles" / f"u{user_id or 0}" / f"lane-{lane}"


class RunQueue:
    """并发调度器：固定线程小池 + 动态派发条件（全局 max / 每人 k / 用例互斥双向）。

    默认设置 max=1、k=1，行为与老的串行队列一致。派发条件每次现读设置文件；
    submit / 完工 / discard（停止排队）/ wake（管理员改设置）都会唤醒重派。
    调小 max/k 不杀在跑 run，只影响新派发。
    """

    def __init__(self, pool_size: int = run_settings.MAX_HARD, executor=None):
        self.pool_size = max(1, int(pool_size))
        self._executor = executor  # 可注入（单测用假执行器）；None = 用模块级 execute（调用时才解析）
        self._condition = threading.Condition()
        self._waiting: dict[int, dict] = {}  # run_id -> meta，插入序 = FIFO
        self._running: dict[int, dict] = {}  # run_id -> meta + lane
        self._threads: list[threading.Thread] = []
        self._stopping = False

    def start(self) -> None:
        if self._threads:
            return
        self._stopping = False
        for index in range(self.pool_size):
            thread = threading.Thread(target=self._worker, name=f"run-worker-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 5.0) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()

    def submit(self, run: Run) -> None:
        with self._condition:
            self._waiting[run.id] = {"run_id": run.id, "user_id": run.user_id,
                                     "env_id": run.env_id, "mutex": run.mutex or "none",
                                     "mutex_key": run.mutex_key}
            self._condition.notify_all()

    def discard(self, run_id: int) -> None:
        """把还没派发的 run 移出等待表（停止排队中的运行用）。"""
        with self._condition:
            if self._waiting.pop(run_id, None) is not None:
                self._condition.notify_all()

    def wake(self) -> None:
        """管理员改完并发设置后唤醒重派（动态生效）。"""
        with self._condition:
            self._condition.notify_all()

    def pending(self) -> int:
        with self._condition:
            return len(self._waiting)

    def active(self) -> int:
        with self._condition:
            return len(self._running)

    def snapshot(self) -> dict:
        conf = run_settings.load()
        return {"pending": self.pending(), "running": self.active(), "pool": self.pool_size,
                "max": conf["max"], "k": conf["k"], "concurrency": conf["max"]}

    def _worker(self) -> None:
        while True:
            with self._condition:
                claim = None
                while claim is None:
                    if self._stopping:
                        return
                    claim = self._try_claim()
                    if claim is None:
                        self._condition.wait(timeout=1.0)  # 兜底轮询：绕过 wake 直改文件也能兜住
            run_id, user_id, lane = claim
            run_once = self._executor or execute
            try:
                run_once(run_id, profile_dir_for(user_id, lane))
            except Exception as exc:  # 单个 run 的异常不能打死 worker
                _fail(run_id, f"{exc.__class__.__name__}: {exc}")
            finally:
                with self._condition:
                    self._running.pop(run_id, None)
                    self._condition.notify_all()

    def _try_claim(self) -> tuple[int, int | None, int] | None:
        """锁内挑第一个满足派发条件的等待 run，登记进运行表；返回 (run_id, user_id, lane)。"""
        if not self._waiting:
            return None
        conf = run_settings.load()
        if len(self._running) >= conf["max"]:
            return None
        for run_id, meta in self._waiting.items():
            if not eligible(meta, list(self._running.values()), conf):
                continue
            del self._waiting[run_id]  # 删除后立即返回，无迭代中断
            lane = self._next_lane(meta["user_id"])
            self._running[run_id] = {**meta, "lane": lane}
            return run_id, meta["user_id"], lane
        return None

    def _next_lane(self, user_id: int | None) -> int:
        """该用户最小空闲路：同人并发的两路拿不同 lane（对应不同 profile 目录）。"""
        used = {row["lane"] for row in self._running.values() if row["user_id"] == user_id}
        lane = 0
        while lane in used:
            lane += 1
        return lane


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


CODE_RETRY = 5
# 进程内串行化「查重 → 提交」：同一秒里的并发建 run 不会互撞候选 code；
# 撞了还有提交时的唯一约束重试兜底（多进程部署场景）。
_code_lock = threading.Lock()


def _is_code_conflict(exc: IntegrityError) -> bool:
    """是否是 run.code 撞唯一约束（SQLite 报 "UNIQUE constraint failed: run.code"；
    MySQL 报 "Duplicate entry ... for key 'run.ix_run_code'"——unique+index 的唯一索引名）。"""
    text = str(getattr(exc, "orig", None) or exc)
    return "run.code" in text or "ix_run_code" in text


def scenario_name(db: Session, case: Case) -> str:
    scenario = db.get(Scenario, case.scenario_id)
    return scenario.name if scenario else ""


def _prepare_plan(case: Case, env: Env, base: str) -> tuple[dict, list[str], dict]:
    """编译用例（guard）→ 补默认 url → 解析 vars；返回 (plan, 缺失引用, 解析后的变量表)。"""
    result = case_service.guard(case.yaml_text, list(env.secret_keys or []))
    plan = dict(result["plan"] or {})
    plan.setdefault("url", base)
    resolved, missing = secret_store.resolve_vars(plan.get("vars") or {}, env, env.name)
    return plan, missing, resolved


ACCOUNT_VAR, PASSWORD_VAR = "账号", "密码"


def account_mutex_key(env_id: int, resolved: dict) -> str | None:
    """账号域互斥键：sha256(env_id|账号值|密码值)，只存哈希不存明文。

    解析不到账号就没有键——挡位随之降级按环境挡（详情里会标注）。
    ${secret.账号} 与字面明文解析后是同一个值，能互相命中。
    """
    account = str(resolved.get(ACCOUNT_VAR) or "").strip()
    if not account:
        return None
    password = str(resolved.get(PASSWORD_VAR) or "")
    return hashlib.sha256(f"{env_id}|{account}|{password}".encode("utf-8")).hexdigest()


def effective_mutex(mutex: str, mutex_key: str | None) -> str:
    """生效挡：账号挡但解析不到账号（没有键）→ 按环境挡参与互斥。"""
    if mutex == "account" and not mutex_key:
        return "env"
    return mutex or "none"


def create_run(db: Session, case: Case, env: Env, mode: str, user) -> Run:
    """校验 → 解析密钥 → 落 run 台账（queued）→ 入队。

    code 是「查重后写」：并发下可能算出同一个候选值，提交撞唯一约束就换后缀重试。
    """
    if mode not in ("debug", "ci"):
        raise ApiError(422, "bad_mode", "mode 只能是 debug 或 ci", {"mode": mode})
    base = secret_store.base_url(env)
    if not base:
        raise ApiError(409, "env_unconfigured", f"环境「{env.name}」还没有配置地址，请到环境管理补齐",
                       {"env": env.name})
    plan, missing, resolved = _prepare_plan(case, env, base)
    if missing:
        raise ApiError(409, "var_unresolved", "用例引用的密钥/环境变量在服务器上不存在",
                       {"missing": missing, "env": env.name})
    case_id, env_id, env_name = case.id, env.id, env.name
    plan_json = json.dumps(plan, ensure_ascii=False)
    mutex, mutex_key = case.mutex or "none", account_mutex_key(env_id, resolved)
    policy = run_policy.load()  # 提交时的策略快照：在跑的 run 不受之后改开关影响
    with _code_lock:
        for _ in range(CODE_RETRY):
            case = db.get(Case, case_id) or case
            code = _unique_code(db, case)
            run = Run(
                code=code, case_id=case_id, env_id=env_id, case_code=case.code, case_name=case.name,
                scenario_name=scenario_name(db, case), env_name=env_name, mode=mode, status="queued",
                steps=len(plan.get("steps") or []), plan_json=plan_json,
                artifact_dir=str(get_settings().runs_dir / code), triggered_by=actor(user),
                user_id=getattr(user, "id", None), mutex=mutex, mutex_key=mutex_key,
                policy=dict(policy),
            )
            db.add(run)
            # 运行也算一次改动：用例卡片上的「最后修改或运行」要跟着运行走
            case.updated_by = actor(user)
            case.updated_at = utcnow()
            audit.log(db, user, "run.create", "run", run.code,
                      {"case": case.code, "env": env_name, "mode": mode})
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                if not _is_code_conflict(exc):
                    raise
                continue
            queue.submit(run)
            return run
    raise ApiError(409, "run_conflict", "运行编号连续撞车，请稍后重试", {"case": case_id})


def _unique_batch_code(db: Session) -> str:
    base = f"b-{datetime.now():%Y%m%d-%H%M%S}"
    code, n = base, 1
    while db.scalar(select(Run).where(Run.batch_code == code)) is not None:
        n += 1
        code = f"{base}-{n}"
    return code


def create_batch(db: Session, cases: list[Case], env: Env, mode: str, user,
                 suite: Suite | None = None) -> tuple[str, list[Run]]:
    """批量建 run（同一 batch_code 按序入队）。先整体校验：任一不过则整批不建。"""
    if mode not in ("debug", "ci"):
        raise ApiError(422, "bad_mode", "mode 只能是 debug 或 ci", {"mode": mode})
    unique: list[Case] = []
    seen: set[int] = set()
    for case in cases:
        if case.id not in seen:
            seen.add(case.id)
            unique.append(case)
    if not unique:
        raise ApiError(422, "batch_empty", "批次里至少要有 1 个用例")
    base = secret_store.base_url(env)
    if not base:
        raise ApiError(409, "env_unconfigured", f"环境「{env.name}」还没有配置地址，请到环境管理补齐",
                       {"env": env.name})
    problems: list[dict] = []
    prepared: list[tuple[Case, dict, dict]] = []
    for case in unique:
        try:
            plan, missing, resolved = _prepare_plan(case, env, base)
        except ApiError as exc:
            problems.append({"case": case.code, "name": case.name, "code": exc.code,
                             "message": exc.message, "detail": exc.detail})
            continue
        if missing:
            problems.append({"case": case.code, "name": case.name, "code": "var_unresolved",
                             "message": "用例引用的密钥/环境变量在服务器上不存在",
                             "detail": {"missing": missing}})
            continue
        prepared.append((case, plan, resolved))
    if problems:
        raise ApiError(422, "batch_invalid", "有用例没通过校验，批次未创建", {"problems": problems})
    meta_by_case = {case.id: (plan, resolved) for case, plan, resolved in prepared}
    case_codes = [case.code for case, _, _ in prepared]
    env_id, env_name = env.id, env.name
    suite_id, suite_code = (suite.id, suite.code) if suite else (None, "")
    policy = run_policy.load()  # 同批共用一份提交时的策略快照
    with _code_lock:
        batch_code = _unique_batch_code(db)
        for _ in range(CODE_RETRY):
            try:
                runs: list[Run] = []
                for case_id, (plan, resolved) in meta_by_case.items():
                    case = db.get(Case, case_id)
                    code = _unique_code(db, case)
                    run = Run(
                        code=code, case_id=case_id, env_id=env_id, case_code=case.code,
                        case_name=case.name, scenario_name=scenario_name(db, case),
                        env_name=env_name, mode=mode, status="queued",
                        steps=len(plan.get("steps") or []),
                        plan_json=json.dumps(plan, ensure_ascii=False),
                        artifact_dir=str(get_settings().runs_dir / code),
                        triggered_by=actor(user), user_id=getattr(user, "id", None),
                        mutex=case.mutex or "none",
                        mutex_key=account_mutex_key(env_id, resolved),
                        batch_code=batch_code, suite_id=suite_id,
                        policy=dict(policy),
                    )
                    db.add(run)
                    db.flush()  # 同批同用例的第二条要在查重时看到前一条
                    runs.append(run)
                    case.updated_by = actor(user)  # 同 create_run：运行刷新「最后操作人」
                    case.updated_at = utcnow()
                audit.log(db, user, "batch.create", "batch", batch_code,
                          {"total": len(runs), "env": env_name, "mode": mode,
                           "suite": suite_code, "cases": case_codes})
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                if not _is_code_conflict(exc):
                    raise
                batch_code = _unique_batch_code(db)
                continue
            for run in runs:
                queue.submit(run)
            return batch_code, runs
    raise ApiError(409, "batch_conflict", "运行编号连续撞车，批次未创建", {"total": len(case_codes)})


def queue_position(db: Session, run: Run) -> int | None:
    if run.status != "queued":
        return None
    ahead = db.scalar(select(func.count()).select_from(Run).where(Run.status == "queued", Run.id < run.id)) or 0
    return int(ahead) + 1


def execute(run_id: int, profile_dir: Path | None = None) -> None:
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
        policy = run.policy  # 提交时的策略快照；老台账没有 → 按默认全开显式透传

    artifact_dir.mkdir(parents=True, exist_ok=True)
    plan_path = artifact_dir / "plan.json"
    plan_path.write_text(json.dumps({
        "name": plan.get("name") or case.name, "url": base, "steps": plan.get("steps") or [],
        "vars": plan.get("vars") or {}, "notes": plan.get("notes") or "",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    args = ["run", "--plan-file", str(plan_path), "--run-dir", str(artifact_dir),
            "--mode", mode, "--url", base, "--json"]
    args += run_policy.kernel_args(policy)
    for key, value in sorted(vars_map.items()):
        args += ["--var", f"{key}={value}"]

    started = time.time()
    proc = KernelProcess(args, artifact_dir / "kernel.log", profile_dir=profile_dir)
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


def can_stop(run: Run, user) -> bool:
    """停止权限：管理员不限；否则仅发起人本人（老数据没记发起人 → 只许管理员停）。"""
    if getattr(user, "is_admin", False):
        return True
    return run.user_id is not None and run.user_id == getattr(user, "id", None)


def stop(db: Session, run: Run, user=None) -> dict:
    if not can_stop(run, user):
        raise ApiError(403, "forbidden", "只能停止自己发起的运行", {"run": run.code})
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
    queue.discard(run.id)  # 排队中的立刻移出等待表；运行中的等 worker 收尾自然出表
    if proc is not None:
        proc.kill()
    return {"id": run.code, "status": "aborted", "detail": detail}


def stop_batch(db: Session, batch_code: str, user=None) -> dict:
    """批次停止：运行中的杀掉，未跑的置 aborted；已是终态则 409。"""
    runs = list(db.scalars(select(Run).where(Run.batch_code == batch_code).order_by(Run.id)).all())
    if not runs:
        raise not_found("批次", batch_code)
    blocked = [run.code for run in runs if run.status in ACTIVE and not can_stop(run, user)]
    if blocked:
        raise ApiError(403, "forbidden", "批次里有别人发起的运行，只有发起人或管理员能停",
                       {"runs": blocked})
    now = utcnow()
    aborted = 0
    for run in runs:
        values: dict = {"status": "aborted", "finished_at": now, "detail": "批次停止：未执行"}
        if run.status == "running":
            values["detail"] = "批次停止：子进程树已终止，产物可能不完整（partial）"
            if run.started_at:
                values["duration_ms"] = int((now - run.started_at).total_seconds() * 1000)
        changed = db.execute(update(Run).where(Run.id == run.id, Run.status.in_(ACTIVE))
                             .values(**values).execution_options(synchronize_session=False))
        aborted += changed.rowcount
    if aborted == 0:
        db.rollback()
        raise ApiError(409, "batch_not_active", "批次已是终态", {"code": batch_code})
    audit.log(db, user, "batch.stop", "batch", batch_code, {"aborted": aborted})
    db.commit()  # 先把 aborted 落库再杀进程：worker 收尾时不会把状态盖回去
    for run in runs:  # 全量查进程表：覆盖"刚被 worker 认领、又刚好被跳过"的竞态
        queue.discard(run.id)
        with _lock:
            proc = _procs.get(run.id)
        if proc is not None:
            proc.kill()
    return {"id": batch_code, "status": "aborted", "aborted": aborted}


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
