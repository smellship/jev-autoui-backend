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
        assert "[false, false]" in signal.detail

    def test_detail_shows_page_change_history(self):
        """前面在变、从某次起停了——历史序列是监督模型判断"没到页/到了没新数据"的证据。"""
        detector = StuckDetector(conf_min=0.5)
        detector.observe(click(), history())
        detector.observe(click(), history(True))
        signal = detector.observe(click(), history(True, False, False))
        assert signal is not None
        assert "[true, false, false]" in signal.detail and "旧→新" in signal.detail

    def test_history_skips_unsettled_rows(self):
        detector = StuckDetector(conf_min=0.5)
        fact = RecentAction(action="PAGE", kind="tab", text="新标签页", step=4)
        detector.observe(click(), history())
        detector.observe(click(), history(False))
        signal = detector.observe(click(), [*history(None, True, False, False), fact])
        assert signal is not None and "[true, false, false]" in signal.detail

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


def replayed(index: int = 1) -> Decision:
    return Decision(operation="CLICK", target=str(index), target_element=index, confidence=1.0,
                    model="cache")


def test_replayed_decisions_are_exempt_from_stuck():
    """回放只是照播录制时验证过的录像：重复点击不该在回放时重新触发卡住。"""
    detector = StuckDetector(conf_min=0.5)
    for _ in range(5):
        assert detector.observe(replayed(), history(False, False, False)) is None


def test_model_decisions_still_trigger_after_replay_exemption():
    """豁免只对回放：模型自己的重复决策照旧触发。"""
    detector = StuckDetector(conf_min=0.5)
    assert detector.observe(replayed(), history(False, False, False)) is None
    assert detector.observe(click(), history(False, False, False)) is None
    assert detector.observe(click(), history(False, False, False)) is None
    signal = detector.observe(click(), history(False, False, False))
    assert signal is not None and signal.kind == "repeat_no_progress"
