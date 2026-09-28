"""多步计划执行：一个浏览器会话跑完整条 plan，每步一个小 goal + 独立断言。

失败即停：前一步没成立就不把后续步骤当成可继续的状态，剩余步骤记为 skipped。
每步结束后把最近几条动作带给下一步，让决策模型知道"刚刚做过什么"。
M2：监督模型跨步共享一份预算；它给 REPLAN 时，剩下的步骤就地换成新序列（原计划留档在 plan.json）。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ui_agent.config import Settings
from ui_agent.decide.jev import JevEngine
from ui_agent.driver.playwright_ import Driver
from ui_agent.llm.planner import plan_to_json
from ui_agent.llm.supervisor import Supervisor
from ui_agent.report.html import build_report
from ui_agent.run.runner import Runner
from ui_agent.schema.plan import PlanStep, TestPlan
from ui_agent.schema.state import RecentAction
from ui_agent.schema.status import STATUS_LABELS

HISTORY_CARRY = 5


@dataclass
class StepOutcome:
    index: int
    goal: str
    dir: str
    status: str
    detail: str = ""
    decisions: int = 0
    checks: list = field(default_factory=list)
    defects: list[str] = field(default_factory=list)
    attempt: int = 1
    replan: list[PlanStep] | None = None

    @property
    def label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def success(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "attempt": self.attempt,
            "goal": self.goal,
            "dir": self.dir,
            "status": self.status,
            "status_label": self.label,
            "detail": self.detail,
            "decisions": self.decisions,
            "checks": [c.model_dump() for c in self.checks],
            "defects": self.defects,
            "replan": [s.model_dump() for s in self.replan or []],
        }


@dataclass
class PlanResult:
    status: str
    run_dir: Path
    detail: str = ""
    steps: list[StepOutcome] = field(default_factory=list)
    defect_candidates: list[str] = field(default_factory=list)
    zoom_note: str = ""
    report: Path | None = None

    @property
    def label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def success(self) -> bool:
        return self.status == "ok"

    @property
    def failed_step(self) -> StepOutcome | None:
        return next((s for s in self.steps if not s.success), None)


class PlanRunner:
    def __init__(self, settings: Settings, plan: TestPlan, driver: Driver | None = None,
                 engine: JevEngine | None = None, verbose: bool = True, run_dir: Path | None = None,
                 vars: dict[str, str] | None = None, history_carry: int = HISTORY_CARRY,
                 max_actions: int | None = None, max_decisions: int | None = None,
                 supervisor: Supervisor | None = None, json_mode: bool = False):
        self.s = settings
        self.plan = plan
        self.driver = driver or Driver(settings)
        self.engine = engine or JevEngine(settings)
        self.verbose = verbose
        self.json_mode = json_mode      # 追 JSON 时人类话术走 stderr，stdout 只留最后那份 JSON
        self.run_dir = run_dir
        self.history_carry = history_carry
        self.max_actions = max_actions if max_actions is not None else settings.max_actions
        self.max_decisions = max_decisions if max_decisions is not None else settings.max_decisions
        self.vars = {**plan.vars, **(vars or {})}  # CLI --var 覆盖计划里的同名变量
        if supervisor is not None:
            self.supervisor: Supervisor | None = supervisor
        else:
            self.supervisor = Supervisor(settings) if settings.ui_agent_supervisor else None
        self.defects: list[str] = []

    # --- 主流程 ---

    def run(self) -> PlanResult:
        run_dir = self.run_dir or self._make_run_dir()
        run_dir.mkdir(parents=True, exist_ok=True)
        self.run_dir = run_dir
        (run_dir / "plan.json").write_text(plan_to_json(self.plan), encoding="utf-8")
        started = datetime.now()
        self._say(f"计划「{self.plan.name}」共 {len(self.plan.steps)} 步 · 运行目录：{run_dir}")
        self._say(f"模式：{'无头（回归）' if self.s.headless else '可视化（调试）'} · 视口 {self.s.ui_agent_viewport}")
        if self.vars:
            self._say(f"变量：{'、'.join(sorted(self.vars))}（值不落盘、不进模型）")
        if self.supervisor is not None:
            budget = self.supervisor.budget
            self._say(f"监督：每步最多 {budget.per_step} 次、本次运行最多 {budget.per_run} 次")
        result = PlanResult("error", run_dir, "未开始")
        outcomes: list[StepOutcome] = []
        history: list[RecentAction] = []
        replans: list[dict] = []
        attempts: dict[int, int] = {}
        actions: list[dict] = []
        root_trace = None
        try:
            root_trace = (run_dir / "trace.jsonl").open("w", encoding="utf-8")
            self.driver.start()
            index = 1
            while index <= len(self.plan.steps):
                attempts[index] = attempts.get(index, 0) + 1
                step = self.plan.steps[index - 1]
                self._nav(index, step)
                if self.verbose:
                    self._say(f"\n=== 第 {index}/{len(self.plan.steps)} 步：{step.goal} ===")
                    if step.note:
                        self._say(f"备注：{step.note}")
                if self.supervisor is not None:
                    self.supervisor.new_step()
                try:
                    outcome, history = self._step(run_dir, index, step, history, attempts[index],
                                                  self._sink(root_trace, index))
                except Exception as exc:
                    outcome = StepOutcome(index, step.goal, self._dir_name(index, step, attempts[index]),
                                          "error", f"{exc.__class__.__name__}: {exc}", attempt=attempts[index])
                outcomes.append(outcome)
                actions.extend(self._step_actions(run_dir, outcome))
                self._say(self._line(outcome))
                if outcome.success:
                    index += 1
                    continue
                if outcome.replan:
                    replans.append({"index": index, "attempt": attempts[index],
                                    "steps": [s.model_dump() for s in outcome.replan]})
                    self.plan.steps[index - 1:] = list(outcome.replan)
                    self._say(f"⇄ 第 {index} 步被重排：剩余步骤换成 {len(outcome.replan)} 步新序列，继续执行")
                    continue
                result.status = outcome.status
                result.detail = f"第 {index} 步未通过（{outcome.label}）：{outcome.detail}"
                break
            else:
                result.status = "ok"
                result.detail = f"全部 {len(self.plan.steps)} 步断言通过"
        except Exception as exc:
            result.status = "error"
            result.detail = f"{exc.__class__.__name__}: {exc}"
        finally:
            done = {o.index for o in outcomes}
            for index in range(1, len(self.plan.steps) + 1):
                if index in done:
                    continue
                step = self.plan.steps[index - 1]
                outcomes.append(StepOutcome(index, step.goal, self._dir_name(index, step, 1), "skipped",
                                            "前序步骤未通过，未执行"))
            if result.status != "ok":
                self._shot(run_dir, "failure")
            if root_trace is not None:
                root_trace.close()
            (run_dir / "actions.json").write_text(
                json.dumps(actions, ensure_ascii=False, indent=2), encoding="utf-8")
            result.steps = outcomes
            result.defect_candidates = list(self.defects)
            result.zoom_note = self.driver.zoom_note
            if replans:
                (run_dir / "plan.final.json").write_text(
                    json.dumps({"name": self.plan.name, "url": self.plan.url,
                                "steps": [s.model_dump() for s in self.plan.steps]},
                               ensure_ascii=False, indent=2), encoding="utf-8")
            self._write_summary(run_dir, result, started, replans)
            self.driver.close()
            result.report = build_report(run_dir)
        return result

    def _nav(self, index: int, step: PlanStep) -> None:
        url = step.url or (self.plan.url if index == 1 else "")
        if not url:
            return
        self.driver.goto(url)
        self.driver.wait_first_paint()

    def _step(self, run_dir: Path, index: int, step: PlanStep, history: list[RecentAction],
              attempt: int = 1,
              trace_sink: Callable[[dict], None] | None = None) -> tuple[StepOutcome, list[RecentAction]]:
        remaining = [s.goal for s in self.plan.steps[index:]]  # 当前步之后的计划，供 REPLAN 参考
        runner = Runner(self.s, step.goal, step.url or self.plan.url, checks=step.checks,
                        task=self.plan.slug, driver=self.driver, engine=self.engine,
                        verbose=self.verbose, run_dir=run_dir / "steps" / self._dir_name(index, step, attempt),
                        manage_driver=False, recent=history[-self.history_carry:], vars=self.vars,
                        max_actions=self.max_actions, max_decisions=self.max_decisions,
                        supervisor=self.supervisor, remaining_goals=remaining,
                        json_mode=self.json_mode, trace_sink=trace_sink)
        result = runner.run()
        defects = [f"第{index}步 {item}" for item in result.defect_candidates]
        self.defects.extend(defects)
        outcome = StepOutcome(index, step.goal, self._dir_name(index, step, attempt), result.status,
                              result.detail, result.steps, list(result.checks), defects,
                              attempt=attempt, replan=result.replan)
        return outcome, runner.recent

    @staticmethod
    def _sink(handle, index: int) -> Callable[[dict], None]:
        """把某一步的 trace 行原样汇进运行目录根部的聚合 trace（补记它属于哪一步）。"""
        def emit(row: dict) -> None:
            handle.write(json.dumps({**row, "plan_step": index}, ensure_ascii=False) + "\n")
            handle.flush()
        return emit

    @staticmethod
    def _step_actions(run_dir: Path, outcome: StepOutcome) -> list[dict]:
        path = run_dir / "steps" / outcome.dir / "actions.json"
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [{**row, "plan_step": outcome.index} for row in rows if isinstance(row, dict)]

    def _dir_name(self, index: int, step: PlanStep, attempt: int = 1) -> str:
        return self.plan.step_dir_name(index, step, attempt)

    @staticmethod
    def _line(outcome: StepOutcome) -> str:
        mark = "✓" if outcome.success else "✗"
        nth = f"（第 {outcome.attempt} 次）" if outcome.attempt > 1 else ""
        return f"第 {outcome.index} 步{nth} {mark} {outcome.label} · {outcome.detail}"

    # --- 产物 ---

    def _make_run_dir(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return self.s.runs_dir / f"{self.plan.slug}-{stamp}"

    def _shot(self, run_dir: Path, name: str) -> None:
        try:
            self.driver.screenshot(run_dir / "screenshots" / f"{name}.png")
        except Exception:
            pass

    def _write_summary(self, run_dir: Path, result: PlanResult, started: datetime,
                       replans: list[dict] | None = None) -> None:
        data = {
            "kind": "plan",
            "name": self.plan.name,
            "url": self.plan.url,
            "status": result.status,
            "status_label": result.label,
            "detail": result.detail,
            "mode": self.s.ui_agent_mode,
            "headless": self.s.headless,
            "viewport": self.s.ui_agent_viewport,
            "decision_model": self.s.typesafe_model,
            "text_model": self.s.text_model,
            "supervisor": bool(self.supervisor),
            "started": started.isoformat(timespec="seconds"),
            "ended": datetime.now().isoformat(timespec="seconds"),
            "duration_s": round((datetime.now() - started).total_seconds(), 1),
            "vars": sorted(self.vars),  # 只记键名：值可能是凭据
            "steps": [s.as_dict() for s in result.steps],
            "replans": replans or [],
            "defect_candidates": result.defect_candidates,
            "zoom_note": result.zoom_note,
            "notes": self.plan.notes,
        }
        (run_dir / "summary.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- 输出 ---

    def _say(self, text: str) -> None:
        if self.verbose:
            print(text, file=sys.stderr if self.json_mode else sys.stdout, flush=True)
