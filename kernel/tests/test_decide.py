"""决策层的离线单测：动作空间组装、head 生成、响应校验——全部不联网。"""

from __future__ import annotations

import pytest

from ui_agent.config import Settings
from ui_agent.decide.jev import (
    JevEngine,
    available_operations,
    build_action_space,
    build_questions,
    to_jev_state,
    validate_choice,
)
from ui_agent.schema.state import Element, Option, PageInfo, State


def element(index: int, label: str, operations: list[str], **over) -> Element:
    fields = {"role": "button", "in_viewport": True, **over}
    return Element(index=index, label=label, operations=operations, **fields)


def state_with(elements: list[Element], y: int = 0, span: int = 0, text: str = "") -> State:
    return State(
        page=PageInfo(url="http://x/", title="T", text=text, viewport={"w": 1920, "h": 1080},
                      scroll={"y": y, "max": span}),
        elements=elements,
    )


class TestValidateChoice:
    def test_accepts_valid_answer(self):
        answer = {"choice": "CLICK", "probabilities": {"CLICK": 0.7, "WAIT": 0.3}, "confidence": 0.8}
        assert validate_choice(answer, {"CLICK", "WAIT"}) is answer

    @pytest.mark.parametrize(
        "answer",
        [
            {"choice": "PRESS", "probabilities": {"CLICK": 0.7, "WAIT": 0.3}, "confidence": 0.8},
            {"choice": "CLICK", "probabilities": {"CLICK": 0.7}, "confidence": 0.8},
            {"choice": "CLICK", "probabilities": {"CLICK": 0.7, "WAIT": 0.4}, "confidence": 0.8},
            {"choice": "WAIT", "probabilities": {"CLICK": 0.7, "WAIT": 0.3}, "confidence": 0.8},
            {"choice": "CLICK", "probabilities": {"CLICK": 1.7, "WAIT": -0.7}, "confidence": 0.8},
            {"choice": "CLICK", "probabilities": {"CLICK": "0.7", "WAIT": "0.3"}, "confidence": 0.8},
            {"probabilities": {"CLICK": 1.0}, "confidence": 0.9},
        ],
    )
    def test_rejects_broken_answers(self, answer):
        with pytest.raises(ValueError):
            validate_choice(answer, {"CLICK", "WAIT"})


class TestActionSpace:
    def test_click_force_only_for_failed_indexes(self):
        elements = [element(1, "查询", ["CLICK"]), element(2, "重置", ["CLICK"])]
        targets = build_action_space(elements, {"2": "L3 命中测试未通过"})
        assert set(targets["CLICK"]) == {"1", "2"}
        assert set(targets["CLICK_FORCE"]) == {"2"}
        assert targets["CLICK_FORCE"]["2"]["element"] == "[2] button 重置"
        assert "CLICK_FORCE" not in build_action_space(elements, {})

    def test_select_options_are_targets_without_dom_index(self):
        select = element(7, "行政区域", ["SELECT"], role="combobox", options=[
            Option(key="7:1", dom_index=0, label="请选择省份", value="", selected=True),
            Option(key="7:2", dom_index=1, label="2102 大连", value="2102"),
            Option(key="7:3", dom_index=2, label="2101 沈阳", value="2101", disabled=True),
        ])
        targets = build_action_space([select], {})
        assert set(targets["SELECT"]) == {"7:1", "7:2"}
        assert targets["SELECT"]["7:2"]["option"] == "2102 大连"

    def test_offscreen_and_occluded_flags_are_propagated(self):
        elements = [element(3, "下方按钮", ["CLICK"], in_viewport=False, occluded_by="遮罩")]
        targets = build_action_space(elements, {})
        assert targets["CLICK"]["3"]["in_viewport"] is False
        assert targets["CLICK"]["3"]["occluded_by"] == "遮罩"

    def test_scroll_to_targets_are_built_for_offscreen_elements(self):
        elements = [element(1, "查询", ["CLICK"]), element(2, "下方按钮", ["CLICK"], in_viewport=False)]
        targets = build_action_space(elements, {})
        assert set(targets["SCROLL_TO"]) == {"2"}
        assert "SCROLL_TO" not in build_action_space([elements[0]], {})


class TestOperations:
    def test_scroll_operations_follow_viewport_and_position(self):
        far = [element(2, "下方按钮", ["CLICK"], in_viewport=False)]
        targets = build_action_space(far, {})
        top = available_operations(state_with(far, y=0, span=2000), targets)
        middle = available_operations(state_with(far, y=400, span=2000), targets)
        bottom = available_operations(state_with(far, y=2000, span=2000), targets)
        assert "SCROLL_TO" in top and "SCROLL_DOWN" in top and "SCROLL_UP" not in top
        assert {"SCROLL_TO", "SCROLL_UP", "SCROLL_DOWN"} <= set(middle)
        assert "SCROLL_DOWN" not in bottom and "SCROLL_UP" in bottom
        assert {"WAIT", "DONE", "BLOCKED", "PRESS_KEY"} <= set(top)

    def test_no_scroll_heads_when_everything_is_visible(self):
        visible = [element(1, "查询", ["CLICK"])]
        ops = available_operations(state_with(visible, span=0), build_action_space(visible, {}))
        assert not {"SCROLL_TO", "SCROLL_UP", "SCROLL_DOWN"} & set(ops)

    def test_type_text_requires_editable_element(self):
        editable = element(4, "账号", ["CLICK", "TYPE_TEXT"], role="textbox")
        readonly = element(5, "只读", ["CLICK"], role="textbox")
        targets = build_action_space([editable, readonly], {})
        assert set(targets["TYPE_TEXT"]) == {"4"}
        assert "TYPE_TEXT" in available_operations(state_with([editable, readonly]), targets)


class TestQuestions:
    def test_heads_match_targets_and_goal_is_carried(self):
        elements = [element(1, "查询", ["CLICK"]),
                    element(2, "账号", ["CLICK", "TYPE_TEXT"], role="textbox")]
        state = state_with(elements)
        questions, targets = build_questions(state, "登录并查询", ["规则A"])
        assert set(questions["operation"]["criteria"]) == set(available_operations(state, targets))
        assert questions["operation"]["instructions"]["goal"] == "登录并查询"
        assert questions["operation"]["instructions"]["rules"] == ["规则A"]
        assert "click_target" in questions and "type_text_target" in questions
        assert "select_target" not in questions  # 没有观测到下拉框就不发这一问
        assert questions["press_key_target"]["instructions"]["operation"] == "PRESS_KEY"
        assert questions["click_target"]["criteria"]["1"]["element"] == "[1] button 查询"
        assert questions["click_target"]["instructions"]["rules"][-1].startswith("Choose the best observed target")

    def test_state_seen_by_model_has_no_geometry(self):
        state = state_with([element(1, "查询", ["CLICK"], rect=(1, 2, 3, 4))])
        payload = to_jev_state(state)
        assert payload["elements"][0] == {"index": 1, "role": "button", "label": "查询", "operations": ["CLICK"]}
        assert payload["page"]["url"] == "http://x/"
        assert payload["recent_actions"] == []

    def test_state_keeps_secret_flag_but_not_dom_index(self):
        select = element(7, "密码", ["CLICK", "TYPE_TEXT"], role="textbox", secret=True, value="••••••",
                         options=[Option(key="7:1", dom_index=3, label="甲", value="a")])
        payload = to_jev_state(state_with([select]))
        assert payload["elements"][0]["secret"] is True
        assert payload["elements"][0]["value"] == "••••••"
        assert payload["elements"][0]["options"] == [{"key": "7:1", "label": "甲", "value": "a",
                                                      "selected": False, "disabled": False}]

    def test_shadow_element_is_flagged_as_a_fact(self):
        payload = to_jev_state(state_with([element(1, "影子按钮", ["CLICK"], shadow=True),
                                           element(2, "普通按钮", ["CLICK"])]))
        assert payload["elements"][0]["shadow"] is True
        assert "shadow" not in payload["elements"][1]

    def test_every_offered_operation_that_needs_a_target_has_a_head(self):
        elements = [element(1, "查询", ["CLICK"]), element(2, "账号", ["CLICK", "TYPE_TEXT"], role="textbox"),
                    element(3, "下方按钮", ["CLICK"], in_viewport=False)]
        state = state_with(elements, span=2000)
        questions, _ = build_questions(state, "目标")
        for op in questions["operation"]["criteria"]:
            if op in ("CLICK", "CLICK_FORCE", "TYPE_TEXT", "SELECT", "HOVER", "SCROLL_TO"):
                assert op.lower() + "_target" in questions, f"{op} 被列为可选，却没有目标 head"


class TestEngineMapping:
    def test_scroll_to_answer_maps_to_the_offscreen_element(self, monkeypatch):
        """曾经的现场崩溃：SCROLL_TO 在可选操作里，但没有目标 head，模型一选就抛错。"""
        import ui_agent.decide.jev as jev_mod

        state = state_with([element(2, "下方按钮", ["CLICK"], in_viewport=False)], span=2000)
        questions, _ = build_questions(state, "目标")
        ops = {op: 0.0 for op in questions["operation"]["criteria"]}
        ops.update({"SCROLL_TO": 0.8, "WAIT": 0.2})
        answer = {
            "answers": {
                "operation": {"choice": "SCROLL_TO", "confidence": 0.8, "probabilities": ops},
                "scroll_to_target": {"choice": "2", "confidence": 0.9, "probabilities": {"2": 1.0}},
            },
            "model": "jev-test",
        }
        monkeypatch.setattr(jev_mod, "post_json", lambda *a, **k: answer)
        decision = JevEngine(Settings(typesafe_api_key="test")).decide(state, "目标")
        assert decision.operation == "SCROLL_TO" and decision.target_element == 2
