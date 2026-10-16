"""M0 单目标循环：快照 → Jev 决策 → 守卫/执行 → 断言验收。

一次迭代只做一件事；预算、卡住、DONE 的"不是证据"都在这里收口。
M2：卡住/阻塞/预算/断言未过时先问一次监督模型，拿到 HINT/RECOVER/REPLAN/ABORT 才继续；
监督模型不可用（没 key、预算用尽、回答不合规）时行为与 M0 完全一致——直接停机。
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ui_agent.act.executor import ActionResult, Executor
from ui_agent.act.upload import resolve_files
from ui_agent.config import Settings
from ui_agent.decide.delegate import AbortDecision, LlmEngine
from ui_agent.decide.engine import Decider, make_engine
from ui_agent.decide.jev import to_jev_state
from ui_agent.decide.prompts import NEXT_ACTION
from ui_agent.driver.playwright_ import Driver, FileRef
from ui_agent.llm.supervisor import Supervisor
from ui_agent.llm.textfill import resolve_text
from ui_agent.observe.snapshot import build_state
from ui_agent.replay.cache import CACHE_SOURCE
from ui_agent.replay.match import match_next
from ui_agent.replay.session import ReplaySession, record_from
from ui_agent.report.html import build_report
from ui_agent.run.stuck import StuckDetector, StuckSignal
from ui_agent.run.supervise import Verdict, escalate, recover_decision
from ui_agent.run.trace import AGGREGATED_KINDS, TraceWriter, now_at
from ui_agent.schema.decision import Decision
from ui_agent.schema.plan import PlanStep
from ui_agent.schema.state import Element, RecentAction, State
from ui_agent.schema.status import STATUS_LABELS
from ui_agent.verify.asserts import CheckResult, CheckSpec, VerifyOutcome, run_checks_polling

HINT_LIMIT = 3          # 注入 Jev 的监督提示最多留最近三条
BUDGET_EXTEND = 10      # 监督模型说"还能继续"时，给预算触发点放宽的量
DIALOG_DEFECT_KINDS = ("confirm", "prompt", "beforeunload")  # 由配置代替用户应答的对话框


@dataclass
class RunResult:
    status: str
    steps: int
    run_dir: Path
    detail: str = ""
    checks: list[CheckResult] = field(default_factory=list)
    defect_candidates: list[str] = field(default_factory=list)
    zoom_note: str = ""
    report: Path | None = None
    replan: list[PlanStep] | None = None

    @property
    def label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def success(self) -> bool:
        return self.status == "ok"


@dataclass
class Escalation:
    verdict: Verdict
    stop: RunResult | None = None
    acted: bool = False   # 恢复动作已执行（算一次动作）
    cycles: int = 0       # 本次升级额外消耗的决策轮数


class Runner:
    def __init__(self, settings: Settings, goal: str, url: str, checks: list[CheckSpec] | None = None,
                 task: str = "task", driver: Driver | None = None, engine: Decider | None = None,
                 verbose: bool = True, run_dir: Path | None = None, manage_driver: bool = True,
                 recent: list[RecentAction] | None = None, vars: dict[str, str] | None = None,
                 max_actions: int | None = None, max_decisions: int | None = None,
                 supervisor: Supervisor | None = None, remaining_goals: list[str] | None = None,
                 json_mode: bool = False, trace_sink: Callable[[dict], None] | None = None,
                 cache: ReplaySession | None = None):
        self.s = settings
        self.goal = goal
        self.url = url
        self.checks = checks or []
        self.task = task
        self.verbose = verbose
        self.json_mode = json_mode      # 追 JSON 时人类话术走 stderr，stdout 只留最后那份 JSON
        self.trace_sink = trace_sink    # 计划模式用它把每行 trace 汇总到运行目录根部
        self.driver = driver or Driver(settings)
        self.engine = engine or make_engine(settings)
        self.executor = Executor(self.driver)
        self.stuck = StuckDetector(conf_min=settings.ui_agent_conf_min)
        if supervisor is not None:
            self.supervisor: Supervisor | None = supervisor
        else:
            self.supervisor = Supervisor(settings) if settings.ui_agent_supervisor else None
        self.remaining_goals = list(remaining_goals or [])
        self.hints: list[str] = []
        self.recent: list[RecentAction] = list(recent or [])
        self._seeded = len(self.recent)  # 上一步带过来的历史不算本次动作
        self.defects: list[str] = []
        self.text_meta: dict = {}
        self.upload_meta: dict = {}
        self.vars = vars or {}
        self.run_dir = run_dir
        self.manage_driver = manage_driver
        self.cache = cache              # 回放缓存会话：由外层（PlanRunner/CLI）开、本层只暂存与结算
        self.max_actions = max_actions if max_actions is not None else settings.max_actions
        self.max_decisions = max_decisions if max_decisions is not None else settings.max_decisions

    # --- 主流程 ---

    def run(self) -> RunResult:
        run_dir = self.run_dir or self._make_run_dir()
        (run_dir / "snapshots").mkdir(parents=True, exist_ok=True)
        self.run_dir = run_dir
        started = datetime.now()
        self._say(f"运行目录：{run_dir}")
        self._say(f"模式：{'无头（回归）' if self.s.headless else '可视化（调试）'} · 视口 {self.s.ui_agent_viewport}")
        if self.cache is not None:
            for line in self.cache.boot_lines(self.s.ui_agent_decider):
                self._say(line)
        if not self.s.text_model_api_key and not self.vars:
            self._say("注意：无 TEXT_MODEL_API_KEY 与 --var，需要填写的字段会拒填并记为失败（不猜值）。")
        result = RunResult("error", 0, run_dir, "未开始")
        try:
            if self.manage_driver:
                self.driver.start()
                self.driver.goto(self.url)
                self.driver.wait_first_paint()
                if getattr(self.driver, "paint_note", ""):
                    self._say(f"⚠ {self.driver.paint_note}")
            with (run_dir / "trace.jsonl").open("w", encoding="utf-8") as handle:
                self._writer = TraceWriter(handle)
                self._writer.write(self._start_row(run_dir))
                try:
                    result = self._loop(run_dir)
                except Exception as exc:
                    result = RunResult("error", 0, run_dir, f"{exc.__class__.__name__}: {exc}")
                self._writer.write(self._end_row(result, started))
        except Exception as exc:
            result = RunResult("error", 0, run_dir, f"{exc.__class__.__name__}: {exc}")
        finally:
            if self.cache is not None:
                self.cache.settle_step(result.status)
            if result.status != "ok":
                self._shot(run_dir, "failure")
            result.defect_candidates = list(self.defects)
            result.zoom_note = self.driver.zoom_note
            self._write_result(run_dir, result, started)
            if self.manage_driver:
                self.driver.close()
            result.report = build_report(run_dir)
        return result

    def _loop(self, run_dir: Path) -> RunResult:
        prev_fp = ""
        step = actions = done_attempts = 0
        blank_tried = False

        def after(esc: Escalation) -> None:
            nonlocal step, actions, prev_fp
            step += esc.cycles
            if esc.acted:
                actions += 1
                prev_fp = fp

        while True:
            raw = self.driver.snapshot()
            if raw.get("elements"):
                blank_tried = False
            elif not blank_tried:
                # 空白页不是任何模型能据以行动的事实（SPA 登录后偶发）：刷新一次再看
                blank_tried = True
                self._say("⚠ 快照里没有任何元素：刷新一次再看")
                self.driver.reload()
                raw = self.driver.wait_rendered()
            fp = raw.get("fingerprint", "")
            if prev_fp:
                self._mark_page_changed(fp != prev_fp)
            state = build_state(raw, self.recent, self.executor.click_failures,
                                self.s.max_elements, self.s.max_text)
            self._dump(run_dir, "snapshots", step, to_jev_state(state))

            if step >= self.max_decisions:
                esc = self._escalate(run_dir, step, state, None, "budget_decisions",
                                     f"决策预算耗尽（{self.max_decisions}）")
                if esc.stop is not None:
                    return esc.stop
                if esc.verdict.is_unavailable:
                    return RunResult("budget", step, run_dir, f"决策预算耗尽（{self.max_decisions}）", [])
                self.max_decisions += BUDGET_EXTEND
                self._say(f"⇒ 监督后放宽决策预算到 {self.max_decisions}")
                after(esc)
                continue

            decision: Decision | None = None
            if self.cache is not None:
                decision, miss_reason = match_next(self.cache, state)
                if decision is None and self.s.ui_agent_decider == "off":
                    # handover：本步先前已记过未命中原因，讲它，不讲"交还"本身
                    reason = miss_reason if miss_reason != "handover" else (self.cache.last_miss or "retry")
                    if reason == "no-entry":
                        detail = ("没有可用录像（no-entry）：该用例×环境尚未录制，"
                                  "或缓存格式升级后旧录像已作废；决策模型已关闭，无法接管")
                    elif reason == "no-variant":
                        detail = ("没有匹配当前页面起点的录像变体（no-variant）："
                                  "录像的起点与当前页面不符；决策模型已关闭，无法接管")
                    else:
                        detail = f"回放未命中（{reason}）；决策模型已关闭"
                    self._say(f"✗ {detail}，停止")
                    return RunResult("cache_missed", step, run_dir, detail, [])
            if decision is None:
                # 决策关闭时不进两阶段循环——OffEngine 直接报错
                if self.s.ui_agent_decider == "off":
                    self.engine.decide(state, self.goal, self._rules())  # 会抛 RuntimeError
                # 两阶段决策：Phase 1（2 次简单决策）→ Phase 2（5 次思考决策）→ 升级监督
                phase1_error: str | None = None
                phase1_exception: Exception | None = None
                for p1 in range(2):
                    try:
                        decision = self.engine.decide(state, self.goal, self._rules(),
                                                      error_context=phase1_error)
                        break
                    except Exception as exc:
                        phase1_error = f"{exc.__class__.__name__}: {exc}"
                        phase1_exception = exc
                        self._emit({"kind": "model", "at": now_at(), "stage": "decide",
                                    "model": self._decider_model(), "phase": 1,
                                    "attempt": p1 + 1, "error": phase1_error})
                else:
                    # Phase 1 两次都失败：只有校验失败（ValueError）才升级 Phase 2
                    # 配置缺失（RuntimeError）等硬错直接抛，不浪费 Phase 2 调用
                    if not isinstance(phase1_exception, ValueError):
                        raise phase1_exception
                    # Phase 2（思考模式，一律走 LLM）：只有当引擎本身是 LlmEngine 或可以创建时才尝试
                    if isinstance(self.engine, LlmEngine):
                        thinking = self.engine
                    elif self.s.text_model_api_key:
                        thinking = LlmEngine(self.s)
                    else:
                        # 无法创建 LlmEngine，直接抛原异常
                        raise phase1_exception
                    phase2_error = phase1_error
                    for p2 in range(5):
                        try:
                            decision = thinking.decide_thinking(state, self.goal, self._rules(),
                                                                error_context=phase2_error)
                            break
                        except AbortDecision as exc:
                            self._emit({"kind": "model", "at": now_at(), "stage": "decide",
                                        "model": self._decider_model(), "phase": 2,
                                        "attempt": p2 + 1, "abort": str(exc)})
                            return RunResult("aborted", step, run_dir, f"思考模型中止：{exc}", [])
                        except Exception as exc:
                            phase2_error = f"{exc.__class__.__name__}: {exc}"
                            self._emit({"kind": "model", "at": now_at(), "stage": "decide",
                                        "model": self._decider_model(), "phase": 2,
                                        "attempt": p2 + 1, "error": phase2_error})
                    else:
                        # Phase 2 也失败 5 次，交监督裁决
                        if self.supervisor is None:
                            raise RuntimeError(f"两阶段决策均失败（无监督模型）：{phase2_error}")
                        esc = self._escalate(run_dir, step, state, None, "decide_invalid",
                                             f"两阶段决策均失败（Phase1×2 + Phase2×5）；末次：{phase2_error}")
                        if esc.stop is not None:
                            return esc.stop
                        if esc.verdict.is_unavailable:
                            raise RuntimeError(
                                f"两阶段决策均失败：{phase2_error}；监督模型不可用：{esc.verdict.detail}")
                        after(esc)
                        continue
            if self.s.debug_artifacts:
                self._shot(run_dir, f"{step:02d}-{decision.operation.lower()}")

            if decision.operation == "DONE":
                if not self.checks:
                    self._trace(step, state, decision, None)
                    return RunResult("done_unverified", step, run_dir,
                                     "模型报告 DONE，但没有配置断言；不计为成功", [])
                outcome = self._verify(state)
                self._trace(step, state, decision, None)
                self._assert_trace(step, outcome)
                if outcome.ok:
                    self._stage(decision, state)
                    return RunResult("ok", step, run_dir, "全部断言通过", outcome.checks)
                if self.cache is not None and decision.model == CACHE_SOURCE:
                    # 回放的收尾没过断言：录像结论不成立，交还重试
                    self.cache.replay_failed()
                failed = "；".join(c.expected for c in outcome.checks if not c.ok)
                done_attempts += 1
                if done_attempts >= 2:
                    return RunResult("check_failed", step, run_dir, f"断言未通过：{failed}",
                                     outcome.checks)
                esc = self._escalate(run_dir, step, state, decision, "check_failed",
                                     f"断言未通过：{failed}")
                if esc.stop is not None:
                    return esc.stop
                if esc.verdict.is_unavailable and self.supervisor is None:
                    # 大模型总闸被关：升级点直接停，不给第二次 DONE 机会
                    return RunResult("check_failed", step, run_dir, f"断言未通过：{failed}",
                                     outcome.checks)
                self.recent.append(RecentAction(action="CHECK_FAILED", kind="verify",
                                                text=f"断言未通过：{failed}", page_changed=None, step=step))
                if esc.verdict.is_unavailable:
                    self._say(f"≠ 升级不可用（{esc.verdict.detail}）；按 M0 语义再给一次机会")
                    step += 1
                    continue
                after(esc)
                continue

            if decision.operation == "BLOCKED":
                esc = self._escalate(run_dir, step, state, decision, "blocked",
                                     "决策模型判定无法推进")
                if esc.stop is not None:
                    return esc.stop
                if esc.verdict.is_unavailable:
                    return RunResult("blocked", step, run_dir, "决策模型判定无法推进", [])
                after(esc)
                continue

            signal = self.stuck.observe(decision, self.recent)
            if signal is not None:
                esc = self._escalate(run_dir, step, state, decision, signal.kind, signal.detail,
                                     signal=signal)
                if esc.stop is not None:
                    return esc.stop
                if esc.verdict.is_unavailable:
                    return RunResult("stuck", step, run_dir, self._signal_detail(signal), [])
                after(esc)
                continue

            if actions >= self.max_actions:
                esc = self._escalate(run_dir, step, state, decision, "budget_actions",
                                     f"动作预算耗尽（{self.max_actions}）")
                if esc.stop is not None:
                    return esc.stop
                if esc.verdict.is_unavailable:
                    return RunResult("budget", step, run_dir, f"动作预算耗尽（{self.max_actions}）", [])
                self.max_actions += BUDGET_EXTEND
                self._say(f"⇒ 监督后放宽动作预算到 {self.max_actions}")
                after(esc)
                continue

            self._execute(step, state, decision)
            actions += 1
            step += 1
            prev_fp = fp

    def _rules(self) -> list[str] | None:
        return [NEXT_ACTION, *self.hints] if self.hints else None

    def _decider_model(self) -> str:
        """错误行里报告的"进行决策的模型"：跟开关走，别报没发生的事。"""
        if self.s.ui_agent_decider == "llm":
            return self.s.text_model
        if self.s.ui_agent_decider == "off":
            return ""
        return self.s.typesafe_model

    def _escalate(self, run_dir: Path, step: int, state: State, decision: Decision | None,
                  trigger: str, detail: str, signal: StuckSignal | None = None) -> Escalation:
        """问一次监督模型并把裁决落到 trace；返回的 Escalation 说明下一步怎么走。"""
        verdict = escalate(self.supervisor, state, self.goal, trigger, detail, self.remaining_goals)
        esc = Escalation(verdict)
        if verdict.is_unavailable:
            self._trace(step, state, decision, None, signal, supervisor=verdict.trace())
            return esc

        self._say(f"⇄ 升级（{trigger}）：{verdict.label}")
        self._trace(step, state, decision, None, signal, supervisor=verdict.trace())

        if verdict.kind == "hint":
            self.hints = [*self.hints, verdict.ruling.hint][-HINT_LIMIT:]
            self.stuck.reset()
            esc.cycles = 1
            return esc

        if verdict.kind == "recover":
            self.stuck.reset()
            esc.cycles, esc.acted = 2, True
            recovery = recover_decision(verdict.ruling)
            step += 1
            self._dump(run_dir, "snapshots", step, to_jev_state(state))
            self._execute(step, state, recovery, supervisor=verdict.trace())
            return esc

        if verdict.kind == "abort":
            esc.stop = RunResult("aborted", step, run_dir, f"监督模型中止：{verdict.ruling.reason}", [])
            return esc
        steps = list(verdict.ruling.steps)
        esc.stop = RunResult("replanned", step, run_dir,
                             f"监督模型重排：给出新序列 {len(steps)} 步，本次运行停在此步",
                             [], replan=steps)
        return esc

    def _execute(self, step: int, state: State, decision: Decision,
                 supervisor: dict | None = None) -> ActionResult:
        element, text, files, note = self._prepare(decision, state)
        action = self.executor.apply(decision, text, element, files) if not note else ActionResult(
            False, decision.operation, element=decision.target_element, detail=note)
        if action.ok:
            recorded = text if (decision.operation == "TYPE_TEXT"
                                and self.text_meta.get("source") == "text_model") else ""
            self._stage(decision, state, element, recorded)
        elif self.cache is not None and decision.model == CACHE_SOURCE:
            # 回放的记录没执行成：本步剩余录像作废，交给模型或其他处置
            self.cache.replay_failed()
        if action.warning:
            self.defects.append(f"决策#{step} {decision.operation}: {action.warning}")
            self._say(f"⚠ {action.warning}")
        self.recent.append(RecentAction(
            action=decision.operation,
            kind=str(decision.target or decision.press_key or ""),
            text=self._action_text(decision, element, text, action),
            level=action.level,
            page_changed=None,
            step=step,
        ))
        facts = self._collect_facts(step)
        self._trace(step, state, decision, action, supervisor=supervisor, facts=facts)
        self._say(self._step_line(step, decision, element, action))

    def _stage(self, decision: Decision, state: State, element: Element | None = None,
               text: str = "") -> None:
        """断言确认前只暂存；回放命中的决策不重录（消费已计数），模型决策才进暂存。"""
        if self.cache is not None and decision.model != CACHE_SOURCE:
            self.cache.stage(record_from(decision, state, element, text))

    def _collect_facts(self, step: int) -> list[dict]:
        """把驱动登记的页面级事实（新标签、原生对话框）变成可对账的行与缺陷候选。

        对话框是"配置代替用户按了确定/取消"——模型没见过它，所以 confirm/prompt/beforeunload
        一律记成缺陷候选，避免静默假成功。
        """
        facts: list[dict] = []
        for note in self.driver.take_notes():
            facts.append({"fact": "tab", "note": note})
            self.recent.append(RecentAction(action="PAGE", kind="tab", text=note, step=step))
            self._say(f"⇢ {note}")
        for dialog in self.driver.take_dialogs():
            handled = {"accept": "已点确定", "dismiss": "已关闭", "failed": "应答失败"}.get(
                dialog["handled"], dialog["handled"])
            note = f"原生 {dialog['kind']} 对话框：「{dialog['message']}」→ {handled}"
            if dialog.get("default_value"):
                note += f"（默认值 {dialog['default_value']}）"
            facts.append({"fact": "dialog", **dialog})
            self.recent.append(RecentAction(action="PAGE", kind="dialog", text=note, step=step))
            self._say(f"⇢ {note}")
            if dialog["kind"] in DIALOG_DEFECT_KINDS:
                self.defects.append(f"决策#{step} {note}")
        return facts

    def _mark_page_changed(self, changed: bool) -> None:
        """page_changed 只挂在真正的动作行上：页面事实行不是动作，不该被当成一次尝试。"""
        row = next((a for a in reversed(self.recent) if a.action != "PAGE"), None)
        if row is not None:
            row.page_changed = changed

    @staticmethod
    def _signal_detail(signal: StuckSignal) -> str:
        return f"{signal.kind}：{signal.detail}"

    # --- 决策落地前的准备：元素与文本 ---

    @staticmethod
    def _element(state: State, index: int | None) -> Element | None:
        if index is None:
            return None
        return next((el for el in state.elements if el.index == index), None)

    def _prepare(self, decision: Decision, state: State) -> tuple[Element | None, str, list[FileRef] | None, str]:
        element = self._element(state, decision.target_element)
        if decision.operation == "TYPE_TEXT":
            if element is None:
                return None, "", None, "目标索引不在当次快照中；未执行输入。"
            replay_text = ""
            if decision.model == CACHE_SOURCE:
                replay_text = str(decision.raw_answers.get("text") or "")
            if replay_text:
                # 录像是文本模型产出的值：直接复用，零模型回归不再问一次
                self.text_meta = {}
                return element, replay_text, None, ""
            try:
                text, meta = resolve_text(self.s, self.goal, element, state, self.vars)
            except RuntimeError as exc:
                return element, "", None, str(exc)
            self.text_meta = meta
            return element, text, None, ""
        if decision.operation == "UPLOAD":
            if element is None:
                return None, "", None, "目标索引不在当次快照中；未执行上传。"
            try:
                files, meta = resolve_files(element, self.vars)
            except RuntimeError as exc:
                return element, "", None, str(exc)
            self.upload_meta = meta
            return element, "", files, ""
        return element, "", None, ""

    def _action_text(self, decision: Decision, element: Element | None, text: str, action: ActionResult) -> str:
        if not action.ok or element is None:
            return action.detail
        if decision.operation == "TYPE_TEXT":
            return "•" * 6 if element.secret else text
        if decision.operation == "SELECT":
            option = next((o for o in element.options if o.key == decision.target), None)
            return f"{element.label} → {option.label}" if option else element.label
        if decision.operation in ("CLICK", "CLICK_FORCE", "HOVER", "SCROLL_TO"):
            return element.label
        return action.detail

    # --- 产物 ---

    def _make_run_dir(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", self.task).strip("_") or "task"
        return self.s.runs_dir / f"{name}-{stamp}"

    @staticmethod
    def _dump(run_dir: Path, folder: str, step: int, data: dict) -> None:
        path = run_dir / folder / f"{step:03d}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _shot(self, run_dir: Path, name: str) -> None:
        try:
            self.driver.screenshot(run_dir / "screenshots" / f"{name}.png")
        except Exception:
            pass

    def _trace(self, step: int, state: State, decision: Decision | None,
               action: ActionResult | None, signal: StuckSignal | None = None,
               supervisor: dict | None = None, facts: list[dict] | None = None) -> None:
        line = {
            "kind": "decision",
            "at": now_at(),
            "step": step,
            "time": datetime.now().isoformat(timespec="seconds"),
            "url": state.page.url,
            "title": state.page.title,
            "fingerprint": state.fingerprint,
            "operation": "" if decision is None else decision.operation,
            "target": None if decision is None else decision.target,
            "target_element": None if decision is None else decision.target_element,
            "confidence": None if decision is None else decision.confidence,
            "operation_probabilities": {} if decision is None else decision.operation_probabilities,
            "target_probabilities": {} if decision is None else decision.target_probabilities,
            "model": "" if decision is None else decision.model,
            "latency_ms": 0 if decision is None else decision.latency_ms,
            "usage": {} if decision is None else decision.usage,
            "action": None if action is None else action.trace,
            "signal": None if signal is None else {"kind": signal.kind, "detail": signal.detail},
            "supervisor": supervisor,
            "click_failures": dict(self.executor.click_failures),
        }
        if action is not None and decision is not None and decision.operation == "TYPE_TEXT" and self.text_meta:
            line["text_model"] = self.text_meta
        if action is not None and decision is not None and decision.operation == "UPLOAD" and self.upload_meta:
            line["upload"] = self.upload_meta  # 只记文件名/大小与变量键名，路径不落盘
        if facts:
            line["page_facts"] = facts
        self._emit(line)

    # --- 时间线写入：一份 trace.jsonl 承载运行/决策/断言/模型调用四层 ---

    def _start_row(self, run_dir: Path) -> dict:
        return {
            "kind": "run_start",
            "at": now_at(),
            "run_id": run_dir.name,
            "task": self.task,
            "goal": self.goal,
            "url": self.url,
            "mode": self.s.ui_agent_mode,
            "headless": self.s.headless,
            "viewport": self.s.ui_agent_viewport,
            "decider": self.s.ui_agent_decider,
            "decision_model": self.s.typesafe_model,
            "text_model": self.s.text_model,
            "supervisor": self.supervisor is not None,
            "cache": self.cache is not None,
            "cache_strict": self.cache.strict if self.cache is not None else False,
            "vars": sorted(self.vars),
        }

    def _end_row(self, result: RunResult, started: datetime) -> dict:
        return {
            "kind": "run_end",
            "at": now_at(),
            "status": result.status,
            "status_label": result.label,
            "detail": result.detail,
            "steps": result.steps,
            "duration_s": round((datetime.now() - started).total_seconds(), 1),
            "defects": len(self.defects),
        }

    def _emit(self, row: dict) -> None:
        written = self._writer.write(row)
        if self.trace_sink is not None and row.get("kind", "decision") in AGGREGATED_KINDS:
            self.trace_sink(written)

    def _verify(self, state: State) -> VerifyOutcome:
        def fresh() -> State:
            return build_state(self.driver.snapshot(), self.recent, self.executor.click_failures,
                               self.s.max_elements, self.s.max_text)
        return run_checks_polling(self.checks, state, fresh,
                                  self.s.ui_agent_assert_timeout_ms, dom=self.driver)

    def _assert_trace(self, step: int, outcome: VerifyOutcome) -> None:
        self._emit({
            "kind": "assert",
            "at": now_at(),
            "step": step,
            "ok": outcome.ok,
            "attempts": outcome.attempts,
            "elapsed_ms": outcome.elapsed_ms,
            "checks": [item.model_dump() for item in outcome.checks],
        })

    def _write_result(self, run_dir: Path, result: RunResult, started: datetime) -> None:
        data = {
            "task": self.task,
            "goal": self.goal,
            "url": self.url,
            "status": result.status,
            "status_label": result.label,
            "detail": result.detail,
            "steps": result.steps,
            "mode": self.s.ui_agent_mode,
            "headless": self.s.headless,
            "viewport": self.s.ui_agent_viewport,
            "decider": self.s.ui_agent_decider,
            "decision_model": self.s.typesafe_model,
            "text_model": self.s.text_model,
            "started": started.isoformat(timespec="seconds"),
            "ended": datetime.now().isoformat(timespec="seconds"),
            "duration_s": round((datetime.now() - started).total_seconds(), 1),
            "checks": [c.model_dump() for c in result.checks],
            "replan": [s.model_dump() for s in result.replan or []],
            "defect_candidates": result.defect_candidates,
            "zoom_note": result.zoom_note,
        }
        (run_dir / "result.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        (run_dir / "actions.json").write_text(
            json.dumps([a.model_dump() for a in self.recent[self._seeded:]], ensure_ascii=False, indent=2),
            encoding="utf-8")

    # --- 输出 ---

    def _say(self, text: str) -> None:
        if self.verbose:
            print(text, file=sys.stderr if self.json_mode else sys.stdout, flush=True)

    def _step_line(self, step: int, decision: Decision, element: Element | None, action: ActionResult) -> str:
        label = element.label if element else (decision.target or "—")
        mark = "✓" if action.ok else "✗"
        level = f" {action.level}" if action.level else ""
        return (f"[{step:02d}] {decision.operation}{level} 置信 {decision.confidence:.2f} → {label} "
                f"{mark} {action.detail}")
