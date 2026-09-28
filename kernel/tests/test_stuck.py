"""卡住判定的离线单测：只有"能确定"的信号才允许停机。"""

from __future__ import annotations

from ui_agent.run.stuck import StuckDetector
from ui_agent.schema.decision import Decision
from ui_agent.schema.state import RecentAction


def click(index: int = 1, confidence: float = 0.9) -> Decision:
    return Decision(operation="CLICK", target=str(index), target_element=index, confidence=confidence)


def history(*page_changed: bool | None) -> list[RecentAction]:
    return [RecentAction(action="CLICK", kind="1", text="查询", page_changed=flag) for flag in page_changed]


class TestRepeatNoProgress:
    def test_fires_on_third_identical_decision_without_page_change(self):
        detector = StuckDetector(conf_min=0.5)
        assert detector.observe(click(), history()) is None
        assert detector.observe(click(), history(False)) is None
        signal = detector.observe(click(), history(False, False))
        assert signal is not None and signal.kind == "repeat_no_progress"
        assert "3" in signal.detail

    def test_page_change_resets_progress_requirement(self):
        detector = StuckDetector(conf_min=0.5)
        detector.observe(click(), history())
        detector.observe(click(), history(False))
        assert detector.observe(click(), history(False, True)) is None

    def test_switching_target_resets_streak(self):
        detector = StuckDetector(conf_min=0.5)
        detector.observe(click(1), history())
        detector.observe(click(1), history(False))
        assert detector.observe(click(2), history(False, False)) is None

    def test_same_target_with_progress_never_fires(self):
        detector = StuckDetector(conf_min=0.5)
        for _ in range(5):
            assert detector.observe(click(), history(True)) is None


class TestLowConfidence:
    def test_fires_after_three_low_confidence_decisions(self):
        detector = StuckDetector(conf_min=0.55)
        assert detector.observe(click(1, 0.4), history()) is None
        assert detector.observe(click(1, 0.4), history(True)) is None
        signal = detector.observe(click(1, 0.4), history(True, True))
        assert signal is not None and signal.kind == "low_confidence"

    def test_confident_decision_resets_counter(self):
        detector = StuckDetector(conf_min=0.55)
        detector.observe(click(1, 0.4), history())
        detector.observe(click(1, 0.9), history(True))
        assert detector.observe(click(1, 0.4), history(True, True)) is None
        assert detector.observe(click(1, 0.4), history(True, True, True)) is None
        assert detector.observe(click(1, 0.4), history(True, True, True, True)).kind == "low_confidence"


def test_press_key_decision_has_key_in_target():
    decision = Decision(operation="PRESS_KEY", press_key="Enter", confidence=0.9)
    assert StuckDetector.key(decision) == ("PRESS_KEY", "Enter")


def test_page_fact_rows_do_not_mask_no_progress():
    """页面事实行（新标签/对话框）不是动作，不能让"重复且没进展"判不出来。"""
    detector = StuckDetector(conf_min=0.5)
    fact = RecentAction(action="PAGE", kind="dialog", text="原生 alert 对话框", step=1)
    assert detector.observe(click(), [*history(False), fact]) is None
    assert detector.observe(click(), [*history(False, False), fact]) is None
    signal = detector.observe(click(), [*history(False, False, False), fact])
    assert signal is not None and signal.kind == "repeat_no_progress"
