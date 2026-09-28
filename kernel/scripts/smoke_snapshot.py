"""离线冒烟：用本地夹具把 M0 的真实链路跑一遍（快照 → 守卫 → 阶梯 → 断言）。

零模型调用：不碰 TypeSafe，也不碰百炼。用法（默认无头）：
    uv run python scripts/smoke_snapshot.py [--headed]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ui_agent.act.executor import Executor  # noqa: E402
from ui_agent.config import Settings  # noqa: E402
from ui_agent.decide.jev import available_operations, build_action_space, build_questions  # noqa: E402
from ui_agent.driver.playwright_ import Driver  # noqa: E402
from ui_agent.observe.snapshot import build_state  # noqa: E402
from ui_agent.schema.decision import Decision  # noqa: E402
from ui_agent.schema.state import Element, State  # noqa: E402
from ui_agent.verify.asserts import parse_check, run_checks  # noqa: E402

FIXTURE = PROJECT_ROOT / "examples" / "local_fixture.html"
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}{'' if ok else '  ← ' + detail}")
    if not ok:
        FAILURES.append(f"{name} {detail}".strip())


def find(state: State, label: str, role: str = "") -> Element:
    hits = [el for el in state.elements if label in el.label and (not role or el.role == role)]
    if not hits:
        raise AssertionError(f"夹具里没有『{label}』：{[el.label for el in state.elements]}")
    return hits[0]


def print_table(state: State) -> None:
    print(f"  页面：{state.page.title} · {state.page.url}")
    print(f"  元素 {len(state.elements)} 个，文本 {len(state.page.text)} 字，"
          f"滚动 y={state.page.scroll.get('y')}/{state.page.scroll.get('max')}")
    for el in state.elements:
        flags = []
        if not el.in_viewport:
            flags.append("视口外")
        if el.occluded_by:
            flags.append(f"被遮挡({el.occluded_by})")
        if el.secret:
            flags.append("敏感")
        ops = "|".join(el.operations)
        line = (f"    [{el.index:>2}] {el.role:<11} {el.label[:22]:<24} "
                f"value={el.value[:18]:<20} {ops:<22} {' '.join(flags)}")
        print(line)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true", help="可视化跑一遍（默认无头）")
    args = parser.parse_args()

    settings = Settings().model_copy(update={"ui_agent_mode": "debug" if args.headed else "ci",
                                             "ui_agent_supervisor": False,
                                             "slow_mo": 120 if args.headed else 0})
    driver = Driver(settings)
    executor = Executor(driver)

    def refresh() -> State:
        raw = driver.snapshot()
        return build_state(raw, [], executor.click_failures, driver.s.max_elements, driver.s.max_text)

    def act(decision: Decision, text: str = ""):
        element = next((el for el in state.elements if el.index == decision.target_element), None)
        return executor.apply(decision, text, element)

    print(f"夹具：{FIXTURE}")
    print(f"模式：{'可视化' if not settings.headless else '无头'} · 视口 {settings.ui_agent_viewport}")

    try:
        driver.start()
        driver.goto(FIXTURE.as_uri())

        print("\n[1] 登录前快照")
        state = refresh()
        print_table(state)
        check("密码框带敏感标记", any(el.secret for el in state.elements),
              str([(el.label, el.secret) for el in state.elements]))
        check("视口外的『下方按钮』被标记", any(not el.in_viewport for el in state.elements), "应有一个视口外元素")
        check("隐藏区域里的元素不进表", not any("查询" in el.label for el in state.elements),
              str([el.label for el in state.elements]))
        disabled = [el for el in state.elements if el.label == "重 置"]
        check("禁用按钮作为事实可见，但没有任何可执行的 operation",
              len(disabled) == 1 and disabled[0].operations == [], str([el.operations for el in disabled]))

        print("\n[2] 输入与下拉选择（T0 / S0）")
        user = find(state, "账号")
        action = act(Decision(operation="TYPE_TEXT", target_element=user.index, confidence=1.0), "demo-user")
        check("账号已输入", action.ok and "demo-user" in action.detail, action.detail)
        pwd = find(state, "密码")
        action = act(Decision(operation="TYPE_TEXT", target_element=pwd.index, confidence=1.0), "demo-pass-123")
        check("密码输入成功且日志脱敏",
              action.ok and "demo-pass-123" not in action.detail and "•" in action.detail, action.detail)
        region = find(state, "行政区域", role="combobox")
        option = next(o for o in region.options if o.label.endswith("大连"))
        action = act(Decision(operation="SELECT", target=f"{region.index}:{option.key}",
                                 target_element=region.index, target_option=option.dom_index, confidence=1.0))
        check("行政区域已选 2102 大连", action.ok, action.detail)
        state = refresh()
        pwd_now = find(state, "密码")
        check("回到模型前密码只剩掩码",
              pwd_now.secret and pwd_now.value == "••••••" and "demo-pass-123" not in state.page.text,
              f"value={pwd_now.value!r}")

        print("\n[3] 点击『确 定』（L0–L3 内成功）")
        submit = find(state, "确 定")
        action = act(Decision(operation="CLICK", target_element=submit.index, confidence=1.0))
        check("确定按钮点击成功", action.ok, action.detail)
        check("没有走强制降级", action.level in ("L0", "L1", "L2", "L3") and not action.warning,
              f"{action.level} {action.warning}")
        state = refresh()
        for item in run_checks(state, [parse_check("element_value=行政区域|2102 大连"),
                                       parse_check("element_exists=查询"),
                                       parse_check("element_absent=票据类型")]):
            check(item.label, item.ok, item.evidence)

        print("\n[4] 标签区分：『查询』不是『高级查询』")
        hits = [el for el in state.elements if "查询" in el.label]
        check("两个按钮都在候选里且标签不同",
              sorted(el.label for el in hits) == ["查询", "高级查询"], str([el.label for el in hits]))

        print("\n[5] 遮挡目标：CLICK 四级失败 → CLICK_FORCE 兜底并记缺陷候选")
        covered = find(state, "被遮挡的按钮")
        check("遮挡来自真实命中测试", bool(covered.occluded_by), f"occluded_by={covered.occluded_by!r}")
        action = act(Decision(operation="CLICK", target_element=covered.index, confidence=1.0))
        check("普通点击确实失败", not action.ok, action.detail)
        check("失败被记录（CLICK_FORCE 的依据）", str(covered.index) in executor.click_failures,
              str(executor.click_failures))
        targets = build_action_space(state.elements, executor.click_failures)
        check("CLICK_FORCE 只对被记录的索引出现",
              set(targets.get("CLICK_FORCE", {})) == {str(covered.index)}, str(list(targets.get("CLICK_FORCE", {}))))
        action = act(Decision(operation="CLICK_FORCE", target_element=covered.index, confidence=1.0))
        check("强制/合成点击成功", action.ok, action.detail)
        check("已标记为可用性缺陷候选", "可用性缺陷候选" in action.warning, action.warning or "(无警告)")
        print(f"    （降级级别 {action.level}：真实遮挡下只有合成事件能到达元素本身，所以必须通报缺陷）")
        state = refresh()
        check("按钮的动作真的生效了", "遮挡按钮被点到" in state.page.text, state.page.text[-100:])

        print("\n[6] 视口外目标：SCROLL_DOWN 改位置，SCROLL_TO 一步到位")
        below = find(state, "下方按钮")
        check("『下方按钮』当前在视口外", not below.in_viewport, f"in_viewport={below.in_viewport}")
        before_y = state.page.scroll.get("y", 0)
        action = act(Decision(operation="SCROLL_DOWN", confidence=1.0))
        state = refresh()
        check("向下滚动改变了 scroll.y", action.ok and state.page.scroll.get("y", 0) > before_y,
              f"{before_y} → {state.page.scroll.get('y')}")
        below = find(state, "下方按钮")
        action = act(Decision(operation="SCROLL_TO", target_element=below.index, confidence=1.0))
        state = refresh()
        check("SCROLL_TO 后『下方按钮』进入视口", action.ok and find(state, "下方按钮").in_viewport,
              f"ok={action.ok} in_viewport={find(state, '下方按钮').in_viewport}")

        print("\n[7] 决策输入预览（不发请求，只看组装结果）")
        targets = build_action_space(state.elements, executor.click_failures)
        print(f"  可用操作：{available_operations(state, targets)}")
        questions, _ = build_questions(state, "登录后选择行政区域 2102 大连并查询",
                                       ["（真实规则在 decide/prompts.py）"])
        print(f"  会发出的 head：{list(questions)}")
        check("组装出 operation head", "operation" in questions, "")
        check("click_target 有候选", bool(questions["click_target"]["criteria"]), "")
        check("模型输入里没有几何信息", "rect" not in str(questions["click_target"]["criteria"]), "")

        print("\n[8] 点击『查询』后结果表出现（验收断言）")
        action = act(Decision(operation="CLICK", target_element=find(state, "查询").index, confidence=1.0))
        check("查询按钮点击成功", action.ok, action.detail)
        state = refresh()
        for item in run_checks(state, [parse_check("text_contains=票据类型"),
                                       parse_check("element_exists=高级查询"),
                                       parse_check("url_contains=local_fixture")]):
            check(item.label, item.ok, item.evidence)
    finally:
        driver.close()

    print("")
    if FAILURES:
        print(f"冒烟失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("冒烟通过：快照 / 守卫 / 阶梯 / 断言 全部符合预期（未调用任何模型）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
