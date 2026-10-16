"""开关路由的离线单测：决策层三档（jev/llm/off）、大模型总闸、代班出参校验——不联网。"""

from __future__ import annotations

import pytest
from fakes import FakeDriver, FakeEngine, done, read_rows, read_trace, settings_for, snapshot

from ui_agent.config import Settings
from ui_agent.decide import delegate as delegate_mod
from ui_agent.decide.delegate import LlmEngine
from ui_agent.decide.engine import OffEngine, make_engine
from ui_agent.decide.jev import JevEngine
from ui_agent.decide.prompts import DELEGATE
from ui_agent.llm import textfill as textfill_mod
from ui_agent.llm.textfill import resolve_text
from ui_agent.observe.snapshot import build_state
from ui_agent.run.runner import Runner
from ui_agent.schema.state import Element, PageInfo, State
from ui_agent.verify.asserts import parse_check


def state_of(raw: dict) -> State:
    return build_state(raw, [], {}, 250, 6000)


def sealed(criteria: list[str], choice: str) -> dict:
    """按 validate_choice 的口径造一个合法答复：全键覆盖、和=1、choice 最大。"""
    rest = [key for key in criteria if key != choice]
    share = round(0.5 / len(rest), 4) if rest else 0.0
    probabilities = {key: share for key in rest}
    probabilities[choice] = round(1 - share * len(rest), 4)
    return {"choice": choice, "probabilities": probabilities, "confidence": probabilities[choice]}


def fake_delegate_reply(script: list[tuple[str, str | None]]):
    """按 (operation, target) 剧本生成合法的 answers；顺带记录每次收到的上下文。"""
    seen: list[dict] = []

    def fake_chat(settings, system, user, max_tokens=1024, reasoning=None):
        seen.append({"system": system, "user": user, "max_tokens": max_tokens, "reasoning": reasoning})
        operation, target = script.pop(0)
        questions = user["questions"]
        answers = {"operation": sealed(list(questions["operation"]["criteria"]), operation)}
        head = operation.lower() + "_target"
        if target is not None and head in questions:
            answers[head] = sealed(list(questions[head]["criteria"]), target)
        return {"answers": answers}, {"model": "m", "latency_ms": 3, "usage": {"total_tokens": 9}}

    return fake_chat, seen


class TestEngineRouting:
    def test_make_engine_follows_the_switch(self):
        assert isinstance(make_engine(Settings()), JevEngine)
        assert isinstance(make_engine(Settings(ui_agent_decider="jev")), JevEngine)
        assert isinstance(make_engine(Settings(ui_agent_decider="llm")), LlmEngine)
        assert isinstance(make_engine(Settings(ui_agent_decider="off")), OffEngine)

    def test_off_engine_decides_nothing(self):
        with pytest.raises(RuntimeError, match="已停止"):
            OffEngine(Settings()).decide(state_of(snapshot("a")), "目标")


class TestLlmDelegate:
    def test_reuses_the_jev_request_shape_and_returns_a_decision(self, monkeypatch):
        fake_chat, seen = fake_delegate_reply([("CLICK", "1")])
        monkeypatch.setattr(delegate_mod, "chat_json", fake_chat)
        decision = LlmEngine(Settings(text_model_api_key="k")).decide(
            state_of(snapshot("a", labels=["查询"])), "点查询")

        assert decision.operation == "CLICK" and decision.target_element == 1
        assert decision.target == "1" and decision.model == "m"
        assert decision.usage == {"total_tokens": 9} and decision.latency_ms == 3
        # 入参与 Jev 请求同构（state + questions），快路径固定不开思考
        payload = seen[0]["user"]
        assert set(payload) == {"state", "questions"}
        assert payload["state"]["elements"][0]["label"] == "查询"
        assert "operation" in payload["questions"] and "click_target" in payload["questions"]
        assert seen[0]["system"] == DELEGATE
        assert seen[0]["reasoning"] == "none" and seen[0]["max_tokens"] == 4096

    def test_requires_a_text_model_key(self):
        with pytest.raises(RuntimeError, match="TEXT_MODEL_API_KEY"):
            LlmEngine(Settings(text_model_api_key="")).decide(state_of(snapshot("a")), "目标")

    def test_missing_answers_object_is_rejected(self, monkeypatch):
        calls = {"n": 0}

        def no_answers(*a, **k):
            calls["n"] += 1
            return {}, {}

        monkeypatch.setattr(delegate_mod, "chat_json", no_answers)
        with pytest.raises(ValueError, match="answers"):
            LlmEngine(Settings(text_model_api_key="k")).decide(state_of(snapshot("a")), "目标")
        assert calls["n"] == 1  # 重试由 runner 两阶段循环负责，engine 单次调用

    def test_answers_go_through_the_same_validation_as_jev(self, monkeypatch):
        bad = {"answers": {"operation": {"choice": "FLY", "probabilities": {"CLICK": 1.0},
                                         "confidence": 1.0}}}
        monkeypatch.setattr(delegate_mod, "chat_json", lambda *a, **k: (bad, {}))
        with pytest.raises(ValueError, match="未通过校验（choice='FLY' 不在候选内"):
            LlmEngine(Settings(text_model_api_key="k")).decide(state_of(snapshot("a")), "目标")

    def test_missing_target_head_is_rejected(self, monkeypatch):
        fake_chat, _ = fake_delegate_reply([("CLICK", None), ("CLICK", None)])
        monkeypatch.setattr(delegate_mod, "chat_json", fake_chat)
        with pytest.raises(ValueError, match="未通过校验"):
            LlmEngine(Settings(text_model_api_key="k")).decide(state_of(snapshot("a")), "目标")

    def test_one_bad_sealed_answer_raises_immediately(self, monkeypatch):
        """Phase 1 单次调用不重试——重试由 runner 两阶段循环负责。"""
        calls = {"n": 0}
        bad = {"answers": {"operation": {"choice": "CLICK", "probabilities": {"CLICK": 1.0},
                                         "confidence": 1.0}}}

        def always_bad(*a, **k):
            calls["n"] += 1
            return (bad, {})

        monkeypatch.setattr(delegate_mod, "chat_json", always_bad)
        with pytest.raises(ValueError, match="未通过校验"):
            LlmEngine(Settings(text_model_api_key="k")).decide(
                state_of(snapshot("a", labels=["查询"])), "点查询")
        assert calls["n"] == 1  # engine 单次调用，不内置重试

    def test_two_bad_sealed_answers_raise_validation_error(self, monkeypatch):
        """两次都败时，engine 只抛 ValueError（无「共尝试 N 次」尾缀）。"""
        calls = {"n": 0}
        bad = {"answers": {"operation": {"choice": "CLICK", "probabilities": {"CLICK": 1.0},
                                         "confidence": 1.0}}}

        def always_bad(*a, **k):
            calls["n"] += 1
            return bad, {}

        monkeypatch.setattr(delegate_mod, "chat_json", always_bad)
        with pytest.raises(ValueError, match="未通过校验"):
            LlmEngine(Settings(text_model_api_key="k")).decide(state_of(snapshot("a")), "目标")
        assert calls["n"] == 1


class TestSupervisorOffStopsAtEscalation:
    def test_failed_done_stops_without_a_second_chance(self, tmp_path):
        settings = settings_for(tmp_path, ui_agent_supervisor=False, ui_agent_assert_timeout_ms=0)
        driver = FakeDriver([snapshot("a")])
        engine = FakeEngine([done(), done()])
        runner = Runner(settings, "目标", "http://x/", checks=[parse_check("text_contains=票据类型")],
                        task="t", driver=driver, engine=engine, verbose=False, supervisor=None)
        result = runner.run()

        assert result.status == "check_failed"
        # 只发生了一次决策：直接停，不走"再给一次机会"（升级行会复制决策行走 trace，不拿来计数）
        assert len(engine.seen_states) == 1


class TestTextFillFollowsMasterSwitch:
    def settings(self, **over) -> Settings:
        return Settings(**{"ui_agent_supervisor": False, **over})

    def test_switch_off_refuses_without_a_var(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("大模型总闸关着就不该调用文本模型")

        monkeypatch.setattr(textfill_mod, "chat_json", boom)
        state = State(page=PageInfo(url="http://x/", title="T", text="请输入帐户名"),
                      elements=[Element(index=1, role="textbox", label="账号",
                                        operations=["CLICK", "TYPE_TEXT"])])
        with pytest.raises(RuntimeError, match="总闸"):
            resolve_text(self.settings(), "登录", state.elements[0], state)

    def test_switch_off_points_at_unmatched_vars(self):
        state = State(page=PageInfo(url="http://x/", title="T", text=""),
                      elements=[Element(index=1, role="textbox", label="请输入帐户名",
                                        operations=["CLICK", "TYPE_TEXT"])])
        with pytest.raises(RuntimeError, match="总闸") as excinfo:
            resolve_text(self.settings(), "登录", state.elements[0], state, vars={"账号": "u"})
        message = str(excinfo.value)
        assert "账号" in message and "请输入帐户名" in message

    def test_switch_off_still_serves_vars(self):
        state = State(page=PageInfo(url="http://x/", title="T", text="请输入帐户名"),
                      elements=[Element(index=1, role="textbox", label="账号",
                                        operations=["CLICK", "TYPE_TEXT"])])
        value, meta = resolve_text(self.settings(), "登录", state.elements[0], state,
                                   vars={"账号": "sysadmin"})
        assert value == "sysadmin" and meta["source"] == "var"


class TestRunnerUsesTheFactory:
    def test_llm_decider_runs_end_to_end_offline(self, tmp_path, monkeypatch):
        fake_chat, _ = fake_delegate_reply([("CLICK", "1"), ("DONE", None)])
        monkeypatch.setattr(delegate_mod, "chat_json", fake_chat)
        settings = settings_for(tmp_path, ui_agent_decider="llm", text_model_api_key="k")
        driver = FakeDriver([snapshot("a"), snapshot("b", text="查询完成"), snapshot("b", text="查询完成")])
        runner = Runner(settings, "查询", "http://x/", checks=[parse_check("text_contains=查询完成")],
                        task="t", driver=driver, verbose=False)  # 不传 engine ⇒ 走工厂
        result = runner.run()

        assert result.status == "ok"
        trace = read_trace(result.run_dir)
        assert [row["operation"] for row in trace] == ["CLICK", "DONE"]
        assert trace[0]["model"] == "m"
        assert read_rows(result.run_dir)[0]["decider"] == "llm"

    def test_off_decider_stops_every_run(self, tmp_path):
        settings = settings_for(tmp_path, ui_agent_decider="off")
        driver = FakeDriver([snapshot("a")])
        runner = Runner(settings, "目标", "http://x/", checks=[], task="t", driver=driver, verbose=False)
        result = runner.run()

        assert result.status == "error" and "已关闭" in result.detail


class TestTwoPhaseDecision:
    """两阶段决策：Phase 1（2 次简单）→ Phase 2（5 次思考）→ 监督。"""

    def test_phase1_succeeds_on_first_try(self, tmp_path, monkeypatch):
        """Phase 1 第一次就成功，不进入 Phase 2。"""
        good_chat, seen = fake_delegate_reply([("CLICK", "1")])
        monkeypatch.setattr(delegate_mod, "chat_json", good_chat)
        engine = LlmEngine(Settings(text_model_api_key="k"))
        decision = engine.decide(state_of(snapshot("a", labels=["查询"])), "点查询")
        assert decision.operation == "CLICK"

    def test_phase1_retries_with_error_context(self, tmp_path, monkeypatch):
        """Phase 1 第二次调用带上一次的报错。"""
        calls = {"n": 0}
        good_chat, seen = fake_delegate_reply([("CLICK", "1")])

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("第一次失败")
            return good_chat(*a, **k)

        monkeypatch.setattr(delegate_mod, "chat_json", flaky)
        engine = LlmEngine(Settings(text_model_api_key="k"))
        # 模拟 runner 的重试逻辑
        last_error = None
        for attempt in range(2):
            try:
                decision = engine.decide(state_of(snapshot("a", labels=["查询"])), "点查询",
                                         error_context=last_error)
                break
            except ValueError as exc:
                last_error = str(exc)
        assert decision.operation == "CLICK"
        assert calls["n"] == 2
        # seen 只记录成功调用：第一次抛错没进 seen，第二次成功是 seen[0]
        assert seen[0]["user"].get("previous_error") == "第一次失败"

    def test_phase2_thinking_mode_enabled(self, tmp_path, monkeypatch):
        """Phase 2 固定以 enabled 口径调用（reasoning_options 不下发关思考字段）。"""
        calls = {"n": 0}
        good_chat, seen = fake_delegate_reply([("DONE", None)])

        def fake_chat(settings, system, user, max_tokens=1024, reasoning=None):
            calls["n"] += 1
            # Phase 2 应使用思考模式；None 会回落到配置值 "none"，不允许
            assert reasoning == "enabled", "Phase 2 应固定 enabled 口径"
            return good_chat(settings, system, user, max_tokens, reasoning)

        monkeypatch.setattr(delegate_mod, "chat_json", fake_chat)
        engine = LlmEngine(Settings(text_model_api_key="k"))
        decision = engine.decide_thinking(state_of(snapshot("a")), "目标")
        assert decision.operation == "DONE"

    def test_phase2_abort_decision(self, tmp_path, monkeypatch):
        """Phase 2 模型可以返回 error 字段中止运行。"""
        from ui_agent.decide.delegate import AbortDecision

        def abort_chat(*a, **k):
            return {"error": "页面上没有找到目标元素"}, {"model": "m", "latency_ms": 3, "usage": {}}

        monkeypatch.setattr(delegate_mod, "chat_json", abort_chat)
        engine = LlmEngine(Settings(text_model_api_key="k"))
        with pytest.raises(AbortDecision, match="页面上没有找到目标元素"):
            engine.decide_thinking(state_of(snapshot("a")), "目标")
