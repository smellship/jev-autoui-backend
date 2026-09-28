"""监督缝的离线单测：裁决校验、预算、以及四类裁决接进主循环后的行为——不联网。"""

from __future__ import annotations

import json

import pytest
from fakes import FakeDriver, FakeEngine, ScriptedSupervisor, blocked, click, done, read_trace, settings_for, snapshot

from ui_agent.config import Settings
from ui_agent.decide.prompts import NEXT_ACTION
from ui_agent.llm import supervisor as supervisor_mod
from ui_agent.llm.supervisor import Supervisor, supervision_context
from ui_agent.observe.snapshot import build_state
from ui_agent.run.runner import Runner
from ui_agent.run.supervise import escalate, recover_decision
from ui_agent.schema.supervisor import SupervisorBudget, parse_ruling
from ui_agent.verify.asserts import parse_check


def state_of(raw: dict):
    return build_state(raw, [], {}, 250, 6000)


def state_with_select():
    raw = snapshot("a", labels=["查询", "行政区域"])
    raw["elements"][1].update({
        "role": "combobox", "operations": ["CLICK", "SELECT"],
        "options": [
            {"key": "2:1", "dom_index": 0, "label": "2102 大连", "value": "2102"},
            {"key": "2:2", "dom_index": 1, "label": "1101 北京", "value": "1101", "disabled": True},
        ],
    })
    return state_of(raw)


class TestParseRuling:
    def test_unknown_ruling_is_rejected(self):
        with pytest.raises(RuntimeError, match="裁决不在"):
            parse_ruling({"ruling": "RETRY"}, state_of(snapshot("a")))

    def test_hint_needs_text(self):
        with pytest.raises(RuntimeError, match="没给 hint"):
            parse_ruling({"ruling": "HINT", "hint": "  "}, state_of(snapshot("a")))
        ruling = parse_ruling({"ruling": "hint", "hint": "先点搜索框再选建议项"}, state_of(snapshot("a")))
        assert ruling.ruling == "HINT" and ruling.label.startswith("HINT：先点搜索框")

    def test_abort_needs_reason(self):
        with pytest.raises(RuntimeError, match="没说原因"):
            parse_ruling({"ruling": "ABORT"}, state_of(snapshot("a")))
        assert parse_ruling({"ruling": "ABORT", "reason": "入口页面 404"}, state_of(snapshot("a"))).reason

    @pytest.mark.parametrize("op", ["CLICK_FORCE", "TYPE_TEXT", "UPLOAD", "DONE", ""])
    def test_recover_operation_whitelist(self, op):
        data = {"ruling": "RECOVER", "action": {"operation": op, "element": 1}}
        with pytest.raises(RuntimeError, match="恢复动作只能在"):
            parse_ruling(data, state_of(snapshot("a")))

    def test_recover_element_must_be_in_the_snapshot(self):
        data = {"ruling": "RECOVER", "action": {"operation": "CLICK", "element": 99}}
        with pytest.raises(RuntimeError, match="不在当次快照里"):
            parse_ruling(data, state_of(snapshot("a")))

    def test_recover_respects_disabled_elements(self):
        raw = snapshot("a", labels=["查询"])
        raw["elements"][0].update({"disabled": True, "operations": []})
        data = {"ruling": "RECOVER", "action": {"operation": "CLICK", "element": 1}}
        with pytest.raises(RuntimeError, match="不支持 CLICK"):
            parse_ruling(data, state_of(raw))

    def test_recover_select_needs_an_enabled_option_of_that_element(self):
        state = state_with_select()
        with pytest.raises(RuntimeError, match="选项不在"):
            parse_ruling({"ruling": "RECOVER", "action": {"operation": "SELECT", "element": 2,
                                                          "option": "2:9"}}, state)
        with pytest.raises(RuntimeError, match="选项不在"):
            parse_ruling({"ruling": "RECOVER", "action": {"operation": "SELECT", "element": 2,
                                                          "option": "2:2"}}, state)  # 已禁用的选项
        ok = parse_ruling({"ruling": "RECOVER", "action": {"operation": "SELECT", "element": 2,
                                                           "option": "2:1"}}, state)
        assert ok.element == 2 and ok.option == "2:1" and ok.option_dom_index == 0

    def test_recover_press_key_needs_a_short_key(self):
        with pytest.raises(RuntimeError, match="按键名"):
            parse_ruling({"ruling": "RECOVER", "action": {"operation": "PRESS_KEY", "key": ""}},
                         state_of(snapshot("a")))
        ok = parse_ruling({"ruling": "RECOVER", "action": {"operation": "PRESS_KEY", "key": "Enter"}},
                          state_of(snapshot("a")))
        assert ok.press_key == "Enter" and ok.element is None

    def test_page_level_operations_need_no_element(self):
        ok = parse_ruling({"ruling": "RECOVER", "action": {"operation": "scroll_down"}},
                          state_of(snapshot("a")))
        assert ok.operation == "SCROLL_DOWN"

    def test_replan_steps_go_through_the_same_checks_as_the_planner(self):
        state = state_of(snapshot("a"))
        with pytest.raises(RuntimeError, match="断言类型不支持"):
            parse_ruling({"ruling": "REPLAN", "steps": [{"goal": "g", "checks": [{"kind": "screen_shows"}]}]},
                         state)
        with pytest.raises(RuntimeError, match="步骤不合法"):
            parse_ruling({"ruling": "REPLAN", "steps": [{"goal": "  "}]}, state)
        steps = [{"goal": f"第 {i} 步", "checks": [{"kind": "text_contains", "value": "x"}]}
                 for i in range(13)]
        with pytest.raises(RuntimeError, match="超过上限"):
            parse_ruling({"ruling": "REPLAN", "steps": steps}, state)
        ok = parse_ruling({"ruling": "REPLAN", "reason": "目标写错了",
                           "steps": [{"goal": "重新进入高级查询", "checks": []},
                                     {"goal": "查看结果", "checks": [{"kind": "url_contains", "value": "adv"}]}]},
                          state)
        assert [s.goal for s in ok.steps] == ["重新进入高级查询", "查看结果"]
        assert ok.steps[1].checks[0].kind == "url_contains"


class TestBudget:
    def test_per_step_window_resets_per_run_does_not(self):
        budget = SupervisorBudget(per_step=2, per_run=3)
        budget.take(), budget.take()
        assert budget.exhausted is True
        budget.new_step()
        assert budget.exhausted is False
        budget.take()
        assert budget.exhausted is True  # 每步 2 次、每次运行 3 次，谁先用完都算用完


class TestSupervisorSeam:
    def settings(self, **over) -> Settings:
        return Settings(**{"text_model_api_key": "k", **over})

    def test_requires_key(self):
        sup = Supervisor(Settings(text_model_api_key=""))
        assert sup.available is False
        with pytest.raises(RuntimeError, match="TEXT_MODEL_API_KEY"):
            sup.ask(state_of(snapshot("a")), "目标", "blocked", "详情")

    def test_ask_parses_and_records_meta(self, monkeypatch):
        seen: list[dict] = []

        def fake_chat(settings, system, user, max_tokens=1024):
            seen.append(user)
            return {"ruling": "HINT", "hint": "先关掉弹窗"}, {"model": "m", "latency_ms": 5}

        monkeypatch.setattr(supervisor_mod, "chat_json", fake_chat)
        sup = Supervisor(self.settings())
        ruling, meta = sup.ask(state_of(snapshot("a", text="首页")), "查询票据", "blocked", "无法推进",
                               remaining=["填表", "提交"])
        assert ruling.ruling == "HINT" and meta["model"] == "m"
        assert sup.budget.step_used == 1 and sup.budget.run_used == 1
        assert seen[0]["stuck"] == {"trigger": "blocked", "detail": "无法推进"}
        assert seen[0]["remaining_goals"] == ["填表", "提交"]
        assert seen[0]["state"]["page"]["text"] == "首页" and "rect" not in json.dumps(seen[0])

    def test_ask_stops_at_the_budget(self, monkeypatch):
        calls = []

        def fake_chat(*a, **k):
            calls.append(1)
            return {"ruling": "HINT", "hint": "再看一眼"}, {}

        monkeypatch.setattr(supervisor_mod, "chat_json", fake_chat)
        sup = Supervisor(self.settings(), SupervisorBudget(per_step=1, per_run=9))
        sup.ask(state_of(snapshot("a")), "目标", "blocked", "详情")
        with pytest.raises(RuntimeError, match="监督预算已用完"):
            sup.ask(state_of(snapshot("a")), "目标", "blocked", "详情")
        assert len(calls) == 1

    def test_invalid_reply_raises_after_consuming_one_call(self, monkeypatch):
        monkeypatch.setattr(supervisor_mod, "chat_json", lambda *a, **k: ({"ruling": "RECOVER"}, {}))
        sup = Supervisor(self.settings())
        with pytest.raises(RuntimeError, match="没有给出动作"):
            sup.ask(state_of(snapshot("a")), "目标", "blocked", "详情")
        assert sup.budget.run_used == 1

    def test_context_is_json_serialisable(self):
        text = json.dumps(supervision_context(state_of(snapshot("a")), "目标", "blocked", "详情"),
                          ensure_ascii=False)
        assert "stuck" in text and "rect" not in text


class TestEscalate:
    def test_no_supervisor_means_m0_behaviour(self):
        verdict = escalate(None, state_of(snapshot("a")), "目标", "blocked", "详情")
        assert verdict.is_unavailable and "--no-supervisor" in verdict.detail
        assert verdict.trace()["ruling"] is None and "error" in verdict.trace()

    def test_missing_key_is_unavailable(self):
        sup = Supervisor(Settings(text_model_api_key=""))
        assert escalate(sup, state_of(snapshot("a")), "目标", "blocked", "详情").is_unavailable

    def test_bad_reply_degrades_to_unavailable(self):
        class Bad(ScriptedSupervisor):
            def ask(self, *a, **k):
                raise RuntimeError("监督模型给的裁决不在 ('HINT',) 里：'RETRY'")

        verdict = escalate(Bad([]), state_of(snapshot("a")), "目标", "blocked", "详情")
        assert verdict.is_unavailable and "RETRY" in verdict.detail

    def test_recover_decision_keeps_operation_and_target(self):
        state = state_with_select()
        ruling = parse_ruling({"ruling": "RECOVER", "action": {"operation": "SELECT", "element": 2,
                                                               "option": "2:1"}}, state)
        decision = recover_decision(ruling)
        assert (decision.operation, decision.target_element, decision.target_option) == ("SELECT", 2, 0)
        assert decision.target == "2:1" and decision.model == "supervisor"

        key = recover_decision(parse_ruling({"ruling": "RECOVER", "action": {"operation": "PRESS_KEY",
                                                                            "key": "Escape"}}, state))
        assert key.operation == "PRESS_KEY" and key.press_key == "Escape" and key.target_element is None


def make_runner(tmp_path, snapshots, decisions, supervisor, checks=(), **settings_over):
    settings = settings_for(tmp_path, **settings_over)
    driver = FakeDriver(snapshots)
    driver.s = settings
    engine = FakeEngine(decisions)
    runner = Runner(settings, "目标", "http://x/", checks=[parse_check(c) for c in checks], task="t",
                    driver=driver, engine=engine, verbose=False, supervisor=supervisor)
    return runner, driver, engine


class TestHintInTheLoop:
    def test_hint_is_injected_as_a_rule_and_resets_the_detector(self, tmp_path):
        sup = ScriptedSupervisor([{"ruling": "HINT", "hint": "别再点查询，先选行政区域"}])
        runner, _, engine = make_runner(
            tmp_path,
            [snapshot("a"), snapshot("a"), snapshot("a"), snapshot("a"), snapshot("a", text="好了")],
            [click(), click(), click(), click(), done()],
            sup, checks=["text_contains=好了"],
        )
        result = runner.run()

        assert result.status == "ok"
        assert [line["operation"] for line in read_trace(result.run_dir)] == \
            ["CLICK", "CLICK", "CLICK", "CLICK", "DONE"]
        assert engine.rules[:3] == [None, None, None]        # 干预前的决策没有额外规则
        assert engine.rules[3] == [NEXT_ACTION, "别再点查询，先选行政区域"]
        assert engine.rules[4] == [NEXT_ACTION, "别再点查询，先选行政区域"]
        hint_row = read_trace(result.run_dir)[2]
        assert hint_row["signal"]["kind"] == "repeat_no_progress"
        assert hint_row["supervisor"]["ruling"] == "HINT"
        assert sup.asked[0]["trigger"] == "repeat_no_progress"

    def test_budget_caps_escalations_per_step(self, tmp_path):
        sup = ScriptedSupervisor([{"ruling": "HINT", "hint": "再看一眼"}], per_step=1)
        runner, _, _ = make_runner(tmp_path, [snapshot("a")] * 3, [blocked(), blocked()], sup)
        result = runner.run()

        assert result.status == "blocked" and len(sup.asked) == 1
        assert "监督预算已用完" in read_trace(result.run_dir)[-1]["supervisor"]["error"]


class TestRecoverInTheLoop:
    def test_recover_action_runs_through_the_normal_path(self, tmp_path):
        sup = ScriptedSupervisor([{"ruling": "RECOVER", "reason": "点高级查询",
                                   "action": {"operation": "CLICK", "element": 2}}])
        runner, _, engine = make_runner(
            tmp_path,
            [snapshot("a", labels=["查询", "高级查询"]),
             snapshot("b", text="已进入高级查询", labels=["查询", "高级查询"])],
            [blocked(), done()],
            sup, checks=["text_contains=已进入高级查询"],
        )
        result = runner.run()

        assert result.status == "ok"
        trace = read_trace(result.run_dir)
        assert [line["operation"] for line in trace] == ["BLOCKED", "CLICK", "DONE"]
        assert trace[0]["supervisor"]["trigger"] == "blocked" and trace[0]["action"] is None
        assert trace[1]["supervisor"]["ruling"] == "RECOVER"
        assert trace[1]["target_element"] == 2 and trace[1]["action"]["ok"] is True
        assert trace[1]["step"] == 1                          # 恢复动作单独占一个决策轮
        actions = json.loads((result.run_dir / "actions.json").read_text(encoding="utf-8"))
        assert [a["action"] for a in actions] == ["CLICK"]    # 和普通动作一样进 actions.json
        assert engine.seen_states[1].recent_actions[-1].page_changed is True

    def test_rejected_recover_keeps_the_m0_stop(self, tmp_path):
        sup = ScriptedSupervisor([{"ruling": "RECOVER", "action": {"operation": "CLICK", "element": 99}}])
        runner, _, _ = make_runner(tmp_path, [snapshot("a")], [blocked()], sup)

        result = runner.run()
        assert result.status == "blocked" and len(sup.asked) == 1
        block = read_trace(result.run_dir)[0]["supervisor"]
        assert block["ruling"] is None and "不在当次快照里" in block["error"]


class TestAbortAndReplanInTheLoop:
    def test_abort_is_its_own_status(self, tmp_path):
        sup = ScriptedSupervisor([{"ruling": "ABORT", "reason": "入口页面 404"}])
        runner, _, _ = make_runner(tmp_path, [snapshot("a")], [blocked()], sup)
        result = runner.run()

        assert result.status == "aborted" and not result.success
        assert "入口页面 404" in result.detail and "中止" in result.label
        assert read_trace(result.run_dir)[0]["supervisor"]["ruling"] == "ABORT"

    def test_single_goal_mode_reports_the_replan_without_running_it(self, tmp_path):
        steps = [{"goal": "先关弹窗", "checks": [{"kind": "text_contains", "value": "首页"}]},
                 {"goal": "再查询", "checks": []}]
        sup = ScriptedSupervisor([{"ruling": "REPLAN", "reason": "目标写错了", "steps": steps}])
        runner, _, engine = make_runner(tmp_path, [snapshot("a")], [blocked()], sup)

        result = runner.run()
        assert result.status == "replanned" and [s.goal for s in result.replan] == ["先关弹窗", "再查询"]
        assert "新序列 2 步" in result.detail and len(engine.seen_states) == 1
        assert "待重排" in result.label
        saved = json.loads((result.run_dir / "result.json").read_text(encoding="utf-8"))
        assert [s["goal"] for s in saved["replan"]] == ["先关弹窗", "再查询"]

    def test_supervisor_can_be_turned_off(self, tmp_path):
        runner, _, _ = make_runner(tmp_path, [snapshot("a")], [blocked()], None,
                                   ui_agent_supervisor=False)
        assert runner.supervisor is None
        result = runner.run()
        assert result.status == "blocked"
        assert "--no-supervisor" in read_trace(result.run_dir)[0]["supervisor"]["error"]


class TestFailedChecksEscalation:
    def test_first_failed_done_asks_the_supervisor(self, tmp_path):
        sup = ScriptedSupervisor([{"ruling": "HINT", "hint": "结果列表还没加载完"}])
        runner, _, engine = make_runner(
            tmp_path,
            [snapshot("a"), snapshot("b", text="票据类型：电子发票")],
            [done(), done()],
            sup, checks=["text_contains=票据类型"],
        )
        result = runner.run()

        assert result.status == "ok"
        assert sup.asked[0]["trigger"] == "check_failed"
        assert engine.rules[1] == [NEXT_ACTION, "结果列表还没加载完"]

    def test_second_failed_done_stops_even_with_a_supervisor(self, tmp_path):
        sup = ScriptedSupervisor([{"ruling": "HINT", "hint": "再看一眼"}])
        runner, _, _ = make_runner(tmp_path, [snapshot("a")] * 4, [done(), done(), done()],
                                   sup, checks=["text_contains=票据类型"])
        result = runner.run()
        assert result.status == "check_failed" and len(sup.asked) == 1
