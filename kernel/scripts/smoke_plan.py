"""离线冒烟：用脚本化决策把一条两步计划跑在本地夹具上（一个会话、零模型调用）。

覆盖：PlanRunner 多步流程、--var 供值（不调文本模型）、SELECT、每步断言、
历史交接、失败即停之外的正常路径、summary/plan/report 产物。
用法（默认无头）：
    uv run python scripts/smoke_plan.py [--headed]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ui_agent.config import Settings  # noqa: E402
from ui_agent.driver.playwright_ import Driver  # noqa: E402
from ui_agent.run.plan_runner import PlanRunner  # noqa: E402
from ui_agent.schema.check import CheckSpec  # noqa: E402
from ui_agent.schema.decision import Decision  # noqa: E402
from ui_agent.schema.plan import PlanStep  # noqa: E402
from ui_agent.schema.plan import TestPlan as Plan  # noqa: E402
from ui_agent.schema.state import Element, State  # noqa: E402

FIXTURE = PROJECT_ROOT / "examples" / "local_fixture.html"
VARS = {"账号": "smoke-user", "密码": "smoke-pass"}
FAILURES: list[str] = []

# 每步一份脚本：决策形状与 JevEngine 解析出来的完全一致
SCRIPTS: dict[str, list[tuple]] = {
    "填写登录信息并点确定": [("TYPE_TEXT", "账号", ""), ("TYPE_TEXT", "密码", ""),
                             ("SELECT", "行政区域", "2102"), ("CLICK", "确 定", ""), ("DONE", "", "")],
    "执行查询并看到结果表": [("CLICK", "查询", ""), ("DONE", "", "")],
}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}{'' if ok else '  ← ' + detail}")
    if not ok:
        FAILURES.append(f"{name} {detail}".strip())


def flat(text: str) -> str:
    return "".join(text.split())


def find(state: State, label: str, role: str = "") -> Element:
    hits = [el for el in state.elements
            if flat(label) in flat(el.label) and (not role or el.role == role)]
    if not hits:
        raise AssertionError(f"夹具里没有『{label}』：{[el.label for el in state.elements]}")
    return hits[0]


class ScriptedEngine:
    """按 goal 取脚本、按标签在当次快照里定位目标：确定性地顶替 Jev 的决策位置。"""

    def __init__(self, scripts: dict[str, list[tuple]]):
        self.scripts = scripts
        self.pos: dict[str, int] = {}
        self.calls: list[tuple[str, int]] = []

    def decide(self, state: State, goal: str, rules=None) -> Decision:
        script = self.scripts[goal]
        at = self.pos.get(goal, 0)
        if at >= len(script):
            raise AssertionError(f"『{goal}』的脚本已经用完（第 {at + 1} 次决策）")
        self.pos[goal] = at + 1
        operation, label, extra = script[at]
        self.calls.append((goal, len(state.elements)))
        if operation == "DONE":
            return Decision(operation="DONE", confidence=0.99)
        if operation == "SELECT":
            element = next((el for el in find_all(state, label) if el.options), None)
            if element is None:
                raise AssertionError(f"夹具里没有带选项的『{label}』")
            option = next((o for o in element.options if flat(extra) in flat(o.label)), None)
            if option is None:
                raise AssertionError(f"『{label}』没有选项 {extra}：{[o.label for o in element.options]}")
            return Decision(operation="SELECT", target=option.key, target_element=element.index,
                            target_option=option.dom_index, confidence=0.99)
        role = "textbox" if operation == "TYPE_TEXT" else ""
        element = find(state, label, role=role)
        return Decision(operation=operation, target=str(element.index), target_element=element.index,
                        confidence=0.99)


def find_all(state: State, label: str) -> list[Element]:
    return [el for el in state.elements if flat(label) in flat(el.label)]


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true", help="可视化跑一遍（会弹窗）")
    args = parser.parse_args()

    settings = Settings(ui_agent_mode="debug" if args.headed else "ci",
                        ui_agent_headless=not args.headed, ui_agent_supervisor=False)
    plan = Plan(name="本地夹具两步计划", url=FIXTURE.as_uri(), steps=[
        PlanStep(goal="填写登录信息并点确定",
                 checks=[CheckSpec(kind="text_contains", expected="已登录：smoke-user")]),
        PlanStep(goal="执行查询并看到结果表",
                 checks=[CheckSpec(kind="text_contains", expected="医疗电子票据"),
                         CheckSpec(kind="element_exists", expected="票据类型")]),
    ])
    driver = Driver(settings)
    engine = ScriptedEngine(SCRIPTS)
    runner = PlanRunner(settings, plan, driver=driver, engine=engine, verbose=True, vars=VARS)

    print(f"[1] 跑计划（{'可视化' if args.headed else '无头'}）：{plan.name}")
    result = runner.run()
    run_dir = result.run_dir

    print("\n[2] 结果与产物")
    check("计划整体成功", result.success, f"{result.status} {result.detail}")
    check("两步都通过", [s.status for s in result.steps] == ["ok", "ok"],
          str([s.status for s in result.steps]))
    check("决策按同一会话连续消费脚本（第1步5次 + 第2步2次）",
          [goal for goal, _ in engine.calls] == ["填写登录信息并点确定"] * 5 + ["执行查询并看到结果表"] * 2,
          str(engine.calls))
    check("plan.json 落盘", (run_dir / "plan.json").exists())
    summary = load_json(run_dir / "summary.json") if (run_dir / "summary.json").exists() else {}
    check("summary.json 是计划报告且有 2 步",
          summary.get("kind") == "plan" and len(summary.get("steps", [])) == 2, json.dumps(summary)[:120])
    check("变量只记键名，不落值",
          summary.get("vars") == sorted(VARS) and "smoke-pass" not in json.dumps(summary), str(summary.get("vars")))

    print("\n[3] 每步的独立产物")
    for step in result.steps:
        sub = run_dir / "steps" / step.dir
        trace = [json.loads(line) for line in
                 (sub / "trace.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        actions = load_json(sub / "actions.json")
        ok_ops = [row["operation"] for row in trace if row.get("action") and row["action"]["ok"]]
        check(f"第{step.index}步 trace/actions 齐备且有动作",
              bool(trace) and len(actions) == len([r for r in trace if r.get("action")]), str(ok_ops))
        check(f"第{step.index}步 至少一次动作改变了页面",
              any(a.get("page_changed") for a in actions), json.dumps(actions, ensure_ascii=False)[:160])
    first = load_json(run_dir / "steps" / result.steps[0].dir / "actions.json")
    check("TYPE_TEXT 的值来自 --var（没调文本模型）",
          any(a["action"] == "TYPE_TEXT" and "smoke-user" in a["text"] for a in first),
          json.dumps(first, ensure_ascii=False)[:160])
    check("密码在历史里被打码", any(a["action"] == "TYPE_TEXT" and a["text"] == "•" * 6 for a in first),
          json.dumps(first, ensure_ascii=False)[:160])

    print("\n[4] HTML 报告")
    report = run_dir / "report.html"
    text = report.read_text(encoding="utf-8") if report.exists() else ""
    shots_rel = (Path("steps") / result.steps[0].dir / "screenshots").as_posix() + "/"
    check("report.html 已生成", report.exists() and "计划「本地夹具两步计划」" in text)
    check("报告里有每步的断言", "已登录：smoke-user" in text and "医疗电子票据" in text)
    if args.headed:
        check("debug 模式每步都有截图链接", f'src="{shots_rel}' in text, text[-200:])
    else:
        check("ci 模式不落逐步截图（只有失败才截）",
              "screenshots/" not in text or (run_dir / "screenshots").exists() is False)

    print("\n[5] 平台契约产物（P0）")
    root_trace = [json.loads(line) for line in
                  (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    check("运行目录根部有聚合 trace.jsonl，每行都有 step/url/title",
          bool(root_trace) and all({"step", "url", "title"} <= set(row) for row in root_trace),
          f"{len(root_trace)} 行")
    check("聚合 trace 覆盖两步（plan_step 1/2）",
          {row.get("plan_step") for row in root_trace} == {1, 2},
          str(sorted({row.get("plan_step") for row in root_trace})))
    root_actions = load_json(run_dir / "actions.json") if (run_dir / "actions.json").exists() else []
    check("运行目录根部有聚合 actions.json（带 plan_step）",
          bool(root_actions) and {item.get("plan_step") for item in root_actions} == {1, 2},
          f"{len(root_actions)} 条")
    check("summary.json 记了 mode 与 headless（产物策略与有无头已解绑）",
          summary.get("mode") == ("debug" if args.headed else "ci")
          and summary.get("headless") == (not args.headed),
          f"mode={summary.get('mode')} headless={summary.get('headless')}")

    print("")
    if FAILURES:
        print(f"冒烟失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print(f"冒烟通过：多步计划在真实浏览器上跑通（未调用任何模型）\n报告：{report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
