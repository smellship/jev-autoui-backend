"""主循环的离线单测：用假驱动 + 假决策引擎验证控制流（不起浏览器、不调模型）。"""

from __future__ import annotations

import json

from fakes import FakeDriver, FakeEngine, blocked, click, done, read_trace, settings_for, snapshot

from ui_agent.run.runner import Runner
from ui_agent.schema.decision import Decision
from ui_agent.verify.asserts import parse_check


def make_runner(tmp_path, snapshots, decisions, checks=()):
    settings = settings_for(tmp_path)
    driver = FakeDriver(snapshots)
    driver.s = settings
    engine = FakeEngine(decisions)
    runner = Runner(settings, "目标", "http://x/", checks=[parse_check(c) for c in checks],
                    task="t", driver=driver, engine=engine, verbose=False)
    return runner, driver, engine


def test_happy_path_marks_page_changed_and_writes_artifacts(tmp_path):
    runner, driver, engine = make_runner(
        tmp_path,
        [snapshot("a"), snapshot("b", text="查询完成"), snapshot("b", text="查询完成")],
        [click(), done()],
        checks=["text_contains=查询完成"],
    )
    result = runner.run()

    assert result.status == "ok" and result.steps == 1
    assert result.success and "全部断言通过" in result.label
    assert driver.closed is True
    trace = read_trace(result.run_dir)
    assert [line["operation"] for line in trace] == ["CLICK", "DONE"]
    assert trace[0]["action"]["ok"] is True and trace[0]["action"]["level"] == "L0"
    assert trace[1]["action"] is None and trace[1]["confidence"] > 0
    saved = json.loads((result.run_dir / "result.json").read_text(encoding="utf-8"))
    assert saved["status"] == "ok" and saved["steps"] == 1
    assert (result.run_dir / "snapshots" / "000.json").exists()
    assert (result.run_dir / "snapshots" / "001.json").exists()
    # 第二次决策时，第一次点击的"页面已变化"必须已经写进历史
    assert engine.seen_states[1].recent_actions[-1].page_changed is True


def test_done_without_checks_is_not_success(tmp_path):
    runner, _, _ = make_runner(tmp_path, [snapshot("a")], [done()])
    result = runner.run()
    assert result.status == "done_unverified"
    assert not result.success and "没有配置断言" in result.detail


def test_failed_checks_are_told_to_the_model_then_give_up(tmp_path):
    runner, _, engine = make_runner(
        tmp_path,
        [snapshot("a"), snapshot("a"), snapshot("a")],
        [done(), done()],
        checks=["text_contains=票据类型"],
    )
    result = runner.run()
    assert result.status == "check_failed" and result.steps == 1
    assert [c.ok for c in result.checks] == [False]
    # 第一次 DONE 未过断言 → 以事实形式回填历史，模型第二次仍说 DONE 才放弃
    assert any(a.action == "CHECK_FAILED" for a in engine.seen_states[1].recent_actions)


def test_blocked_stops_immediately(tmp_path):
    runner, _, _ = make_runner(tmp_path, [snapshot("a")], [Decision(operation="BLOCKED", confidence=0.9)])
    result = runner.run()
    assert result.status == "blocked" and result.steps == 0


def test_repeat_without_progress_is_reported_as_stuck(tmp_path):
    runner, _, _ = make_runner(tmp_path, [snapshot("a"), snapshot("a"), snapshot("a")], [click(), click(), click()])
    result = runner.run()
    assert result.status == "stuck" and "repeat_no_progress" in result.detail
    assert read_trace(result.run_dir)[-1]["signal"]["kind"] == "repeat_no_progress"


def test_action_budget_stops_the_loop(tmp_path):
    runner, _, _ = make_runner(
        tmp_path,
        [snapshot("a"), snapshot("b"), snapshot("c"), snapshot("d")],
        [click(), click(), click(), click()],
    )
    result = runner.run()
    assert result.status == "budget" and "动作预算" in result.detail
    assert len(read_trace(result.run_dir)) == 4


def test_type_text_resolution_failure_becomes_a_recorded_failure(tmp_path, monkeypatch):
    runner, _, engine = make_runner(
        tmp_path,
        [snapshot("a", labels=["账号"]), snapshot("a", labels=["账号"])],
        [Decision(operation="TYPE_TEXT", target="1", target_element=1, confidence=0.9),
         Decision(operation="BLOCKED", confidence=0.9)],
    )

    def boom(*args, **kwargs):
        raise RuntimeError("缺少 TEXT_MODEL_API_KEY；无法为 TYPE_TEXT 取值，未执行任何输入。")

    monkeypatch.setattr("ui_agent.run.runner.resolve_text", boom)
    result = runner.run()
    assert result.status == "blocked"
    line = read_trace(result.run_dir)[0]
    assert line["action"]["ok"] is False and "TEXT_MODEL_API_KEY" in line["action"]["detail"]
    assert any(a.action == "TYPE_TEXT" and "TEXT_MODEL_API_KEY" in a.text
               for a in engine.seen_states[1].recent_actions)


def test_blank_state_mid_run_is_recovered_with_one_reload(tmp_path):
    """SPA 偶发全空白：不是任何模型能据以行动的事实，先刷新一次再看。"""
    runner, driver, engine = make_runner(
        tmp_path,
        [snapshot("blank", labels=[]), snapshot("form", labels=["查询"])],
        [done()],
        checks=["element_exists=查询"],
    )
    result = runner.run()

    assert driver.reloads == 1
    assert result.status == "ok"
    assert [len(state.elements) for state in engine.seen_states] == [1], "模型不该看到空白快照"


def test_still_blank_after_reload_is_left_to_the_decision_model(tmp_path):
    runner, driver, engine = make_runner(tmp_path, [snapshot("blank", labels=[])],
                                         [Decision(operation="BLOCKED", confidence=0.9)])
    result = runner.run()

    assert driver.reloads == 1, "只刷新一次，不能刷成循环"
    assert result.status == "blocked"
    assert engine.seen_states[0].elements == []


def test_artifact_policy_follows_mode_not_headless(tmp_path):
    """debug 即便无头也留逐步截图：产物策略与有无头已经解绑。"""
    settings = settings_for(tmp_path, ui_agent_mode="debug", ui_agent_headless=True)
    driver = FakeDriver([snapshot("a"), snapshot("b", text="查询完成"), snapshot("b", text="查询完成")])
    driver.s = settings
    runner = Runner(settings, "目标", "http://x/", checks=[parse_check("text_contains=查询完成")],
                    task="t", driver=driver, engine=FakeEngine([click(), done()]), verbose=False)
    result = runner.run()

    assert settings.headless is True and result.status == "ok"
    shots = sorted(p.name for p in (result.run_dir / "screenshots").glob("*.png"))
    assert shots == ["00-click.png", "01-done.png"]
    saved = json.loads((result.run_dir / "result.json").read_text(encoding="utf-8"))
    assert saved["mode"] == "debug" and saved["headless"] is True


def test_ci_mode_keeps_only_the_failure_screenshot(tmp_path):
    settings = settings_for(tmp_path, ui_agent_mode="ci", ui_agent_headless=False)
    driver = FakeDriver([snapshot("a")])
    driver.s = settings
    runner = Runner(settings, "目标", "http://x/", checks=[],
                    task="t", driver=driver, engine=FakeEngine([blocked()]), verbose=False)
    result = runner.run()

    assert result.status == "blocked"
    assert sorted(p.name for p in (result.run_dir / "screenshots").glob("*.png")) == ["failure.png"]
    saved = json.loads((result.run_dir / "result.json").read_text(encoding="utf-8"))
    assert saved["mode"] == "ci" and saved["headless"] is False


def test_trace_sink_gets_the_same_rows_as_the_trace_file(tmp_path):
    rows: list[dict] = []
    settings = settings_for(tmp_path)
    driver = FakeDriver([snapshot("a"), snapshot("b", text="查询完成"), snapshot("b", text="查询完成")])
    driver.s = settings
    runner = Runner(settings, "目标", "http://x/", checks=[parse_check("text_contains=查询完成")],
                    task="t", driver=driver, engine=FakeEngine([click(), done()]), verbose=False,
                    trace_sink=rows.append)
    result = runner.run()

    assert [row["operation"] for row in rows] == ["CLICK", "DONE"]
    assert rows == read_trace(result.run_dir)
