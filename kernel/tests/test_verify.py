"""验收断言的离线单测：只看 State，不看模型。"""

from __future__ import annotations

import pytest

from ui_agent.schema.state import Element, PageInfo, State
from ui_agent.verify.asserts import check, find_elements, parse_check, run_checks


def state() -> State:
    return State(
        page=PageInfo(url="http://x/main?tab=bill", title="电子票据平台", text="请输入帐户名\n登录成功"),
        elements=[
            Element(index=1, role="textbox", label="账号", value="sysadmin"),
            Element(index=2, role="combobox", label="行政区域", value="2102 大连"),
            Element(index=3, role="button", label="查询"),
            Element(index=4, role="button", label="Submit Order"),
        ],
    )


class TestParseCheck:
    def test_plain_form(self):
        spec = parse_check("text_contains=登录成功")
        assert (spec.kind, spec.value, spec.expected) == ("text_contains", "登录成功", "登录成功")

    def test_two_part_form(self):
        spec = parse_check("element_value=行政区域|2102 大连")
        assert (spec.kind, spec.value, spec.expected) == ("element_value", "行政区域", "2102 大连")

    def test_json_form_and_role(self):
        spec = parse_check('{"kind":"element_exists","value":"查询","role":"button"}')
        assert spec.role == "button"

    @pytest.mark.parametrize("bad", ["nope=1", "text_contains=", ""])
    def test_rejects_bad_input(self, bad):
        with pytest.raises(ValueError):
            parse_check(bad)


class TestChecks:
    def test_text_url_title(self):
        assert check(state(), parse_check("text_contains=登录成功")).ok
        assert not check(state(), parse_check("text_contains=查询结果")).ok
        assert check(state(), parse_check("url_contains=/main")).ok
        assert check(state(), parse_check("title_contains=电子票据")).ok

    def test_element_presence_and_value(self):
        assert check(state(), parse_check("element_exists=查询")).ok
        assert not check(state(), parse_check("element_absent=查询")).ok
        assert check(state(), parse_check("element_absent=高级查询")).ok
        assert check(state(), parse_check("element_value=行政区域|2102 大连")).ok
        assert not check(state(), parse_check("element_value=行政区域|2101 沈阳")).ok

    def test_role_filter_and_evidence(self):
        assert not check(state(), parse_check('{"kind":"element_exists","value":"查询","role":"textbox"}')).ok
        result = check(state(), parse_check("element_value=行政区域|2102 大连"))
        assert "2102 大连" in result.label
        assert "text_contains" in run_checks(state(), [parse_check("text_contains=登录成功")])[0].kind

    def test_find_elements_matches_substring_and_ignores_case(self):
        assert [el.index for el in find_elements(state(), "区域")] == [2]
        assert [el.index for el in find_elements(state(), "submit order")] == [4]
        assert find_elements(state(), "查询", role="textbox") == []
