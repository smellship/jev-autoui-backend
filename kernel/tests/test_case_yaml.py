"""用例 YAML 的离线单测：校验规则、行号、引用检查、编译产物与反向生成。"""

from __future__ import annotations

import json

from ui_agent.case.yaml_case import compile_case, plan_to_case_yaml
from ui_agent.llm.planner import plan_from_json
from ui_agent.schema.check import CheckSpec
from ui_agent.schema.plan import PlanStep
from ui_agent.schema.plan import TestPlan as Plan

GOOD = """\
id: sc-0001-01
title: 83票查询
priority: P1
vars:
  账号: ${{secret.账号}}
  密码: ${{secret.密码}}
  样例: ${{env.样例目录}}/83票.pdf
steps:
  - goal: 打开登录页并登录
    checks:
      - text_contains: 已登录
  - goal: 把行政区域选成 2102 大连
    checks:
      - element_value: 行政区域|2102 大连
      - element_exists: 查询
"""


def codes(result: dict, key: str = "errors") -> set[str]:
    return {item["code"] for item in result[key]}


def test_good_case_compiles_to_plan_with_lines_and_no_errors():
    result = compile_case(GOOD, ["账号", "密码"])
    assert result["ok"] is True and result["errors"] == [] and result["warnings"] == []
    plan = result["plan"]
    assert plan["name"] == "83票查询" and len(plan["steps"]) == 2
    assert plan["vars"]["账号"] == "${{secret.账号}}"  # 引用原样带出，值不在用例里
    assert plan["steps"][1]["checks"][0] == {"kind": "element_value", "value": "行政区域",
                                             "expected": "2102 大连", "role": ""}
    # 编译结果必须能被计划执行器原样吃进去
    assert [step.goal for step in plan_from_json(json.dumps(plan)).steps] == \
           [step["goal"] for step in plan["steps"]]


def test_env_reference_in_vars_is_allowed_and_secret_must_be_registered():
    ok = compile_case("vars:\n  文件: ${{env.目录}}/a.pdf\nsteps:\n  - goal: 上传\n    checks:\n"
                      "      - text_contains: 已上传\n", None)
    assert ok["ok"] is True

    bad = compile_case(GOOD, ["账号"])
    assert bad["ok"] is False and "unknown_secret" in codes(bad)
    assert bad["errors"][0]["line"] == 6  # 指到「密码」那一行
    assert "密码" in bad["errors"][0]["message"]


def test_secret_keys_not_given_skips_the_registry_check():
    result = compile_case(GOOD, None)
    assert result["ok"] is True


def test_empty_or_broken_yaml_reports_a_line():
    empty = compile_case("   ")
    assert empty["ok"] is False and codes(empty) == {"empty_file"} and empty["plan"] is None
    broken = compile_case("steps:\n  - goal: 登录\n   checks: []\n")
    assert broken["ok"] is False and codes(broken) == {"yaml_error"}
    assert broken["errors"][0]["line"] >= 1


def test_steps_must_exist_be_well_formed_and_have_goals():
    no_steps = compile_case("title: 空\n")
    assert codes(no_steps) == {"steps_empty"} and no_steps["errors"][0]["line"] == 1

    result = compile_case("steps:\n  - checks:\n      - text_contains: a\n  - goal: 第二步\n"
                          "    checks:\n      - text_contains: b\n")
    assert codes(result) == {"goal_empty"} and result["errors"][0]["line"] == 2


def test_checks_are_required_and_kinds_are_whitelisted():
    empty = compile_case("steps:\n  - goal: 登录\n")
    assert codes(empty) == {"checks_empty"} and empty["errors"][0]["line"] == 2
    bad_kind = compile_case("steps:\n  - goal: 登录\n    checks:\n      - tap: 确定\n")
    assert codes(bad_kind) == {"bad_kind"}
    no_value = compile_case("steps:\n  - goal: 登录\n    checks:\n      - text_contains: ''\n")
    assert codes(no_value) == {"empty_check"}
    shape = compile_case("steps:\n  - goal: 登录\n    checks:\n      - 就点一下\n")
    assert codes(shape) == {"bad_check"}


def test_full_check_form_is_accepted():
    result = compile_case("steps:\n  - goal: 登录\n    checks:\n"
                          "      - kind: element_value\n        value: 行政区域\n        expected: 2102 大连\n")
    assert result["ok"] is True
    assert result["plan"]["steps"][0]["checks"][0]["role"] == ""


def test_references_are_rejected_in_goals_only():
    result = compile_case("steps:\n  - goal: 用 ${{secret.账号}} 登录\n    checks:\n"
                          "      - text_contains: 已登录\n")
    assert codes(result) == {"ref_in_goal"}
    assert "vars 槽位" in result["errors"][0]["message"]


def test_names_cannot_contain_path_characters():
    result = compile_case("title: 83票/回归\nsteps:\n  - goal: 登录\n    checks:\n"
                          "      - text_contains: 已登录\n")
    assert codes(result) == {"bad_name"} and result["errors"][0]["line"] == 1


def test_duplicate_keys_are_reported_with_the_later_line():
    result = compile_case("title: 甲\ntitle: 乙\nsteps:\n  - goal: 登录\n    checks:\n"
                          "      - text_contains: 已登录\n")
    assert codes(result) == {"duplicate_key"} and result["errors"][0]["line"] == 2


def test_warnings_do_not_block_compiling():
    text = ("steps:\n"
            "  - goal: 输入密码 abc123 后点确定\n"
            "    checks:\n      - text_contains: 已登录\n"
            + "".join(f"      - element_exists: 控件{i}\n" for i in range(3))
            + "  - goal: 查询\n    checks:\n      - element_exists: 查询\n")
    result = compile_case(text)
    assert result["ok"] is True and result["plan"] is not None
    assert {"literal_secret", "too_many_checks", "weak_final_check"} <= codes(result, "warnings")


def test_warning_when_too_many_steps_or_a_hardcoded_url():
    steps = "".join(f"  - goal: 第{i}步\n    checks:\n      - text_contains: 结果{i}\n"
                    for i in range(13))
    result = compile_case(f"url: http://x/\nsteps:\n{steps}")
    assert result["ok"] is True
    assert {"too_many_steps", "hardcoded_url"} <= codes(result, "warnings")
    assert not any(item["code"] == "hardcoded_url" for item in result["errors"])


def test_warning_when_a_secret_looks_like_a_plain_literal():
    result = compile_case("vars:\n  密码: abc123\nsteps:\n  - goal: 登录\n    checks:\n"
                          "      - text_contains: 已登录\n")
    assert result["ok"] is True
    assert codes(result, "warnings") == {"literal_secret"}
    assert "密码" in result["warnings"][0]["message"]


def test_errors_and_warnings_are_sorted_by_line():
    text = ("steps:\n  - goal: ''\n    checks: []\n  - goal: 第二步\n    checks: []\n")
    result = compile_case(text)
    assert [item["line"] for item in result["errors"]] == sorted(item["line"] for item in result["errors"])


def test_plan_can_be_emitted_back_to_yaml_and_recompiled():
    plan = Plan(name="登录并查询", notes="先登录", vars={"账号": "${{secret.账号}}"}, steps=[
        PlanStep(goal="登录", checks=[CheckSpec(kind="text_contains", expected="已登录")]),
        PlanStep(goal="查询", checks=[CheckSpec(kind="element_value", value="行政区域", expected="2102 大连")]),
    ])
    text = plan_to_case_yaml(plan)
    assert "title: 登录并查询" in text and "账号: ${{secret.账号}}" in text
    assert "element_value: 行政区域|2102 大连" in text
    assert "url:" not in text  # 入口由环境决定，不写进用例

    again = compile_case(text, ["账号"])
    assert again["ok"] is True
    assert again["plan"]["steps"][1]["checks"][0]["expected"] == "2102 大连"
