"""验收断言的离线单测：只看 State，不看模型。"""

from __future__ import annotations

import pytest

from ui_agent.schema.state import Element, PageInfo, State
from ui_agent.verify.asserts import check, find_elements, parse_check, run_checks, run_checks_polling


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


def with_text(text: str) -> State:
    st = state()
    st.page.text = text
    return st


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def advance(clock: FakeClock, seconds: float) -> None:
    clock.now += seconds


class TestCountAndAttribute:
    def test_count_pass_and_fail(self):
        spec = parse_check("element_count=查询|1")
        assert (spec.value, spec.expected) == ("查询", "1")
        assert check(state(), spec).ok
        failed = check(state(), parse_check("element_count=查询|2"))
        assert not failed.ok and "实际匹配 1 个" in failed.evidence

    def test_count_without_label_means_no_label_filter(self):
        spec = parse_check("element_count=4")
        assert (spec.value, spec.expected) == ("", "4")
        assert check(state(), spec).ok
        assert not check(state(), parse_check("element_count=3")).ok

    def test_count_rejects_non_integer(self):
        with pytest.raises(ValueError, match="整数"):
            parse_check("element_count=查询|很多")

    def test_count_evidence_marks_truncated_snapshot(self):
        st = state()
        st.omitted = 2  # 快照有元素被截掉，计数断言要标出这一点
        ok = check(st, parse_check("element_count=4"))
        assert ok.ok and "截断" in ok.evidence
        assert "截断" in check(st, parse_check("element_count=9")).evidence

    def test_attribute_requires_attr_and_works_with_dom(self):
        with pytest.raises(ValueError, match="attr"):
            parse_check('{"kind":"element_attribute","value":"下载","expected":"/x"}')
        st = state()
        st.elements.append(Element(index=5, role="link", label="下载使用手册"))

        class Dom:
            def __init__(self, values: dict):
                self.values = values

            def get_attribute(self, index: int, name: str) -> str:
                return self.values.get((index, name), "")

        spec = parse_check('{"kind":"element_attribute","value":"下载使用手册",'
                           ' "attr":"href", "expected":"/help/manual.pdf"}')
        assert check(st, spec, Dom({(5, "href"): "/help/manual.pdf"})).ok
        assert not check(st, spec, Dom({(5, "href"): "/other.pdf"})).ok
        assert not check(st, spec, Dom({})).ok
        missing, evidence = check(st, spec, None), check(st, spec, None).evidence
        assert not missing.ok and "不支持" in evidence

    def test_attribute_reports_missing_element(self):
        class Dom:
            def get_attribute(self, index: int, name: str) -> str:
                return ""

        result = check(state(), parse_check('{"kind":"element_attribute","value":"不存在",'
                                            ' "attr":"href", "expected":"/x"}'), Dom())
        assert not result.ok and "没有匹配元素" in result.evidence


class TestPolling:
    def test_succeeds_after_retries_and_reports_attempt(self):
        clock = FakeClock()
        outcome = run_checks_polling(
            [parse_check("text_contains=查询完成")], with_text("加载中"),
            lambda: with_text("查询完成"), timeout_ms=5000,
            interval_ms=250, clock=clock, sleep=lambda s: advance(clock, s))
        assert outcome.ok and outcome.attempts == 2
        assert outcome.checks[0].attempts == 2 and outcome.elapsed_ms == 250

    def test_timeout_reports_total_rounds(self):
        clock = FakeClock()
        outcome = run_checks_polling(
            [parse_check("text_contains=永远不会出现")], with_text("加载中"),
            lambda: with_text("加载中"), timeout_ms=1000,
            interval_ms=250, clock=clock, sleep=lambda s: advance(clock, s))
        assert not outcome.ok and outcome.attempts == 5 and outcome.elapsed_ms == 1000
        assert outcome.checks[0].attempts == 5

    def test_per_check_timeout_widens_the_window(self):
        clock = FakeClock()
        spec = parse_check('{"kind":"text_contains","value":"X","timeout_ms":1000}')
        outcome = run_checks_polling([spec], with_text("加载中"), lambda: with_text("加载中"),
                                     timeout_ms=0, interval_ms=250, clock=clock,
                                     sleep=lambda s: advance(clock, s))
        assert not outcome.ok and outcome.elapsed_ms == 1000  # 运行级窗口为 0，单条自己加宽到 1s

    def test_zero_window_evaluates_once(self):
        calls = {"n": 0}

        def state_fn() -> State:
            calls["n"] += 1
            return with_text("加载中")

        outcome = run_checks_polling([parse_check("text_contains=不出现")], with_text("加载中"),
                                     state_fn, timeout_ms=0)
        assert outcome.attempts == 1 and calls["n"] == 0

    def test_passed_checks_stop_being_reevaluated(self):
        clock = FakeClock()
        outcome = run_checks_polling(
            [parse_check("text_contains=已经就绪"), parse_check("text_contains=后来就绪")],
            with_text("已经就绪"), lambda: with_text("已经就绪，后来就绪"),
            timeout_ms=1000, interval_ms=250, clock=clock, sleep=lambda s: advance(clock, s))
        assert outcome.ok and outcome.attempts == 2
        assert [item.attempts for item in outcome.checks] == [1, 2]
