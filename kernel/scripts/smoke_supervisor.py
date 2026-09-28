"""离线冒烟：在真实浏览器上跑一遍监督升级的三条路径（零模型调用）。

监督模型是脚本化的替身，但裁决照样过 schema/supervisor.py 的校验，落点也从当次快照里现找：
HINT（重复无进展 → 注入规则后继续）、RECOVER（BLOCKED → 监督代理点一下）、ABORT（停机 + 剩余步骤 skipped）。
用法（默认无头）：
    uv run python scripts/smoke_supervisor.py [--headed]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ui_agent.config import Settings  # noqa: E402
from ui_agent.decide.prompts import NEXT_ACTION  # noqa: E402
from ui_agent.driver.playwright_ import Driver  # noqa: E402
from ui_agent.run.plan_runner import PlanRunner  # noqa: E402
from ui_agent.schema.check import CheckSpec  # noqa: E402
from ui_agent.schema.decision import Decision  # noqa: E402
from ui_agent.schema.plan import PlanStep  # noqa: E402
from ui_agent.schema.plan import TestPlan as Plan  # noqa: E402
from ui_agent.schema.state import Element, State  # noqa: E402
from ui_agent.schema.supervisor import SupervisorBudget, parse_ruling  # noqa: E402

FIXTURE = PROJECT_ROOT / "examples" / "local_fixture.html"
FAILURES: list[str] = []
HINT_TEXT = "确定按钮已经点过，主界面已出现，检查页面状态后直接报告 DONE"

SCRIPTS: dict[str, list[tuple]] = {
    "登录并在重复点击后停下": [("CLICK", "确 定", ""), ("CLICK", "确 定", ""), ("CLICK", "确 定", ""),
                               ("DONE", "", "")],
    "进入高级查询": [("BLOCKED", "", ""), ("DONE", "", "")],
    "点一个不存在的按钮": [("BLOCKED", "", "")],
}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}{'' if ok else '  ← ' + detail}")
    if not ok:
        FAILURES.append(f"{name} {detail}".strip())


def flat(text: str) -> str:
    return "".join(text.split())


def find(state: State, label: str) -> Element:
    hits = [el for el in state.elements if flat(label) in flat(el.label)]
    if not hits:
        raise AssertionError(f"夹具里没有『{label}』：{[el.label for el in state.elements]}")
    return hits[0]


class ScriptedEngine:
    """按 goal 取脚本、按标签在当次快照里定位：确定性地顶替 Jev，并记录收到的额外规则。"""

    def __init__(self, scripts: dict[str, list[tuple]]):
        self.scripts = scripts
        self.pos: dict[str, int] = {}
        self.rules: list[list[str] | None] = []

    def decide(self, state: State, goal: str, rules=None) -> Decision:
        script = self.scripts[goal]
        at = self.pos.get(goal, 0)
        if at >= len(script):
            raise AssertionError(f"『{goal}』的脚本已经用完（第 {at + 1} 次决策）")
        self.pos[goal] = at + 1
        self.rules.append(rules)
        operation, label, _ = script[at]
        if operation in ("DONE", "BLOCKED"):
            return Decision(operation=operation, confidence=0.99)
        element = find(state, label)
        return Decision(operation=operation, target=str(element.index), target_element=element.index,
                        confidence=0.99)


def hint_after_repeat(state: State, goal: str, trigger: str) -> dict:
    _ = state, goal
    return {"ruling": "HINT", "hint": HINT_TEXT, "reason": f"{trigger}：同一个按钮连点三次且页面没变"}


def recover_into_adv(state: State, goal: str, trigger: str) -> dict:
    _ = goal, trigger
    element = find(state, "高级查询")
    return {"ruling": "RECOVER", "reason": "普通查询点不动，先点『高级查询』",
            "action": {"operation": "CLICK", "element": element.index}}


ABORT = {"ruling": "ABORT", "reason": "夹具里没有这个按钮，没有任何支持的操作能继续"}


class ScriptedSupervisor:
    """监督模型替身：脚本可以是 dict，也可以是 (state, goal, trigger) → dict 的函数。"""

    def __init__(self, script: list):
        self.script = list(script)
        self.budget = SupervisorBudget(per_step=2, per_run=6)
        self.available = True
        self.asked: list[dict] = []

    def new_step(self) -> None:
        self.budget.new_step()

    def ask(self, state: State, goal: str, trigger: str, detail: str, remaining=None):
        self.asked.append({"goal": goal, "trigger": trigger, "detail": detail,
                           "remaining": list(remaining or [])})
        if self.budget.exhausted:
            raise RuntimeError(f"监督预算已用完（{self.budget.detail}）；不再咨询监督模型。")
        self.budget.take()
        item = self.script.pop(0)
        data = item(state, goal, trigger) if callable(item) else item
        return parse_ruling(data, state), {"model": "smoke-script", "latency_ms": 0}


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def trace_of(sub: Path) -> list[dict]:
    path = sub / "trace.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true", help="可视化跑一遍（会弹窗）")
    args = parser.parse_args()

    settings = Settings(ui_agent_mode="debug" if args.headed else "ci")
    plan = Plan(name="本地夹具监督冒烟", url=FIXTURE.as_uri(), steps=[
        PlanStep(goal="登录并在重复点击后停下",
                 checks=[CheckSpec(kind="text_contains", expected="已登录：smoke-user")]),
        PlanStep(goal="进入高级查询",
                 checks=[CheckSpec(kind="text_contains", expected="进入高级查询")]),
        PlanStep(goal="点一个不存在的按钮",
                 checks=[CheckSpec(kind="text_contains", expected="永远不会出现")]),
        PlanStep(goal="不会执行的步骤"),
    ])
    driver = Driver(settings)
    engine = ScriptedEngine(SCRIPTS)
    supervisor = ScriptedSupervisor([hint_after_repeat, recover_into_adv, ABORT])
    runner = PlanRunner(settings, plan, driver=driver, engine=engine, verbose=True, supervisor=supervisor)

    print(f"[1] 跑计划（{'可视化' if args.headed else '无头'}）：{plan.name}")
    result = runner.run()
    run_dir = result.run_dir

    print("\n[2] 三条升级路径都按裁决落地")
    check("第 3 步被监督模型中止，计划的整体状态是 aborted",
          result.status == "aborted", f"{result.status} {result.detail}")
    check("步骤状态依次是 ok / ok / aborted / skipped",
          [s.status for s in result.steps] == ["ok", "ok", "aborted", "skipped"],
          str([s.status for s in result.steps]))
    check("三次提问的触发点是 重复无进展 / 阻塞 / 阻塞",
          [item["trigger"] for item in supervisor.asked] == ["repeat_no_progress", "blocked", "blocked"],
          str([item["trigger"] for item in supervisor.asked]))
    check("HINT 以额外规则的形式注入下一次决策",
          engine.rules[3] == [NEXT_ACTION, HINT_TEXT], json.dumps(engine.rules[3:4], ensure_ascii=False))
    check("RECOVER 点的是快照里的『高级查询』，且真的点到了",
          supervisor.asked[1]["goal"] == "进入高级查询"
          and any(row.get("supervisor", {}).get("ruling") == "RECOVER"
                  and (row.get("action") or {}).get("ok") is True
                  for row in trace_of(run_dir / "steps" / result.steps[1].dir)),
          json.dumps(trace_of(run_dir / "steps" / result.steps[1].dir), ensure_ascii=False)[:200])
    check("ABORT 那次提问带着剩余步骤（供 REPLAN 参考）",
          supervisor.asked[2]["remaining"] == ["不会执行的步骤"], str(supervisor.asked[2]))
    check("每步重置窗口、整轮共享总预算：本步 1~2 次、整轮 3 次",
          supervisor.budget.run_used == 3 and not supervisor.budget.exhausted,
          supervisor.budget.detail)

    print("\n[3] 产物与报告")
    summary = load_json(run_dir / "summary.json")
    check("summary.json 记录了监督开关、重排为空、状态 aborted",
          summary.get("supervisor") is True and summary.get("replans") == []
          and summary.get("status") == "aborted", json.dumps(summary, ensure_ascii=False)[:160])
    report = run_dir / "report.html"
    text = report.read_text(encoding="utf-8") if report.exists() else ""
    check("报告里能看到 HINT / RECOVER / ABORT 三种裁决",
          all(token in text for token in ("监督 HINT", "监督 RECOVER", "监督 ABORT")), text[-200:])
    check("报告里 HINT 文本与触发点都在",
          HINT_TEXT in text and "repeat_no_progress" in text, text[-200:])
    if not args.headed:
        check("无头模式落下失败截图（第 3 步中止）", (run_dir / "screenshots" / "failure.png").exists())

    print("")
    if FAILURES:
        print(f"冒烟失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print(f"冒烟通过：监督升级在真实浏览器上跑通（未调用任何模型）\n报告：{report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
