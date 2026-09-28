"""planner 缝的离线单测：payload 校验、步数上限、断言类型白名单与序列化。"""

from __future__ import annotations

import json

import pytest

from ui_agent.config import Settings
from ui_agent.llm.planner import MAX_DESCRIPTION, MAX_STEPS, make_plan, plan_context, plan_from_json, plan_to_json
from ui_agent.schema.plan import PlanStep
from ui_agent.schema.plan import TestPlan as Plan


def payload(**over) -> dict:
    data = {
        "name": "登录并查询",
        "url": "",
        "vars": {},
        "notes": "",
        "steps": [{"goal": "登录系统", "checks": [{"kind": "text_contains", "expected": "首页"}],
                   "url": "", "note": ""}],
    }
    data.update(over)
    return data


def stub(monkeypatch, data) -> list[dict]:
    seen: list[dict] = []

    def fake(settings, system, context, **kwargs):
        seen.append(context)
        return data, {"model": "stub", "latency_ms": 12}

    monkeypatch.setattr("ui_agent.llm.planner.chat_json", fake)
    return seen


def test_make_plan_returns_typed_plan_and_fills_url(monkeypatch):
    seen = stub(monkeypatch, payload())
    plan, meta = make_plan(Settings(typesafe_api_key=""), "  打开系统并查询  ", "http://x/")

    assert isinstance(plan, Plan)
    assert plan.url == "http://x/" and plan.steps[0].goal == "登录系统"
    assert meta["model"] == "stub"
    assert seen[0]["process_description"] == "打开系统并查询" and seen[0]["start_url"] == "http://x/"


def test_plan_url_kept_when_model_provides_one(monkeypatch):
    stub(monkeypatch, payload(url="http://from-model/"))
    plan, _ = make_plan(Settings(typesafe_api_key=""), "x", "http://from-arg/")
    assert plan.url == "http://from-model/"


def test_unknown_check_kind_is_rejected(monkeypatch):
    stub(monkeypatch, payload(steps=[{"goal": "登录", "checks": [{"kind": "screenshot_diff",
                                                                  "expected": "x"}]}]))
    with pytest.raises(RuntimeError, match="不支持的断言"):
        make_plan(Settings(typesafe_api_key=""), "x")


def test_too_many_steps_is_rejected(monkeypatch):
    steps = [{"goal": f"第{i}步"} for i in range(MAX_STEPS + 1)]
    stub(monkeypatch, payload(steps=steps))
    with pytest.raises(RuntimeError, match="超过上限"):
        make_plan(Settings(typesafe_api_key=""), "x")


def test_invalid_plan_payload_is_rejected(monkeypatch):
    stub(monkeypatch, {"name": "x", "steps": []})
    with pytest.raises(RuntimeError, match="计划不合法"):
        make_plan(Settings(typesafe_api_key=""), "x")


def test_plan_context_truncates_and_carries_hints():
    context = plan_context("字" * (MAX_DESCRIPTION + 500), "http://x/", {"mode": "ci"})
    assert len(context["process_description"]) == MAX_DESCRIPTION
    assert context["harness_hints"] == {"mode": "ci"}
    assert "start_url" not in plan_context("x")


def test_plan_json_round_trip(tmp_path):
    plan = Plan(name="登录", url="http://x/", vars={"账号": "sysadmin"},
                steps=[PlanStep(goal="登录", checks=[{"kind": "url_contains", "expected": "/home"}])])
    restored = plan_from_json(plan_to_json(plan))
    assert restored == plan


def test_broken_plan_file_is_reported_in_chinese():
    with pytest.raises(RuntimeError, match="计划文件不合法"):
        plan_from_json("{not json")


def test_plan_slug_and_step_dir_names():
    plan = Plan(name="登录 / 查询（电子票据）", steps=[PlanStep(goal="输入账号密码！")])
    assert plan.slug == "登录_查询_电子票据"
    assert plan.step_dir_name(1, plan.steps[0]) == "01-输入账号密码"
    assert json.loads(plan_to_json(plan))["steps"][0]["goal"] == "输入账号密码！"
