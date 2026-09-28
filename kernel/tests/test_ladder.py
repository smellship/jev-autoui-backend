"""降级阶梯的离线单测：先成功即停、失败逐级记录、force 级别必须带警告。"""

from __future__ import annotations

from ui_agent.driver.ladder import run_ladder


def test_stops_at_first_success_and_records_earlier_failures():
    calls: list[str] = []

    def l0():
        calls.append("L0")
        raise RuntimeError("element is not visible")

    def l1():
        calls.append("L1")

    def l2():
        calls.append("L2")

    outcome = run_ladder([("L0", l0), ("L1", l1), ("L2", l2)])
    assert outcome.ok and outcome.level == "L1" and outcome.warning == ""
    assert calls == ["L0", "L1"]
    assert outcome.attempts == [("L0", "element is not visible")]


def test_all_failed_reports_every_level():
    outcome = run_ladder([("L0", lambda: (_ for _ in ()).throw(RuntimeError("a"))),
                          ("L1", lambda: (_ for _ in ()).throw(RuntimeError("b")))])
    assert not outcome.ok
    assert [level for level, _ in outcome.attempts] == ["L0", "L1"]


def test_force_and_synthetic_levels_carry_warning():
    for level in ("L4", "L5", "T2"):
        outcome = run_ladder([(level, lambda: None)])
        assert outcome.ok and outcome.warning.startswith(level)
        assert "可用性缺陷候选" in outcome.warning


def test_non_force_success_has_no_warning():
    assert run_ladder([("L3", lambda: None)]).warning == ""
