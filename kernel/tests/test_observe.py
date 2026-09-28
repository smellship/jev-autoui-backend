"""快照→State 的离线单测：字段映射、截断、以及"不给模型几何信息"的边界。"""

from __future__ import annotations

from ui_agent.observe.snapshot import build_state
from ui_agent.schema.state import RecentAction

RAW = {
    "frame_url": "http://x/",
    "page": {
        "url": "http://x/login",
        "title": "登录",
        "text": "账号" * 50,
        "viewport": {"w": 1920, "h": 1080, "dpr": 1},
        "scroll": {"y": 120, "max": 900},
    },
    "elements": [
        {"index": 1, "role": "textbox", "label": "账号", "value": "sysadmin", "rect": [0, 0, 10, 10],
         "in_viewport": True, "occluded_by": "", "disabled": False, "operations": ["CLICK", "TYPE_TEXT"]},
        {"index": 2, "role": "textbox", "label": "密码", "secret": True, "value": "••••••",
         "rect": [0, 0, 10, 10], "in_viewport": True, "occluded_by": "", "disabled": False,
         "operations": ["CLICK", "TYPE_TEXT"]},
        {"index": 3, "role": "button", "label": "确 定", "rect": [0, 0, 10, 10], "in_viewport": True,
         "occluded_by": "", "disabled": False, "operations": ["CLICK", "HOVER"]},
    ],
    "fingerprint": "abc123",
}


def test_maps_raw_snapshot_to_state():
    state = build_state(RAW, [], None, max_elements=250, max_text=6000)
    assert state.page.url == "http://x/login"
    assert state.page.scroll == {"y": 120, "max": 900}
    assert state.fingerprint == "abc123"
    assert [el.index for el in state.elements] == [1, 2, 3]
    assert state.elements[1].secret is True and state.elements[1].value == "••••••"
    assert state.elements[0].rect == (0, 0, 10, 10)
    assert state.click_failures == {}


def test_truncates_elements_and_text():
    state = build_state(RAW, [], {}, max_elements=2, max_text=10)
    assert len(state.elements) == 2
    assert len(state.page.text) == 10


def test_recent_actions_and_click_failures_pass_through():
    recent = [RecentAction(action="CLICK", kind="3", text="确 定", level="L1", page_changed=False)]
    state = build_state(RAW, recent, {"9": "L3 命中测试未通过"}, 250, 6000)
    assert state.recent_actions[0].page_changed is False
    assert state.recent_actions[0].level == "L1"
    assert state.click_failures == {"9": "L3 命中测试未通过"}


def test_empty_snapshot_is_survivable():
    state = build_state({}, [], None, 250, 6000)
    assert state.elements == [] and state.page.url == ""


def test_shadow_flag_flows_from_the_snapshot_script():
    raw = {**RAW, "elements": [{**RAW["elements"][0], "shadow": True}]}
    state = build_state(raw, [], None, 250, 6000)
    assert state.elements[0].shadow is True
    assert build_state(RAW, [], None, 250, 6000).elements[0].shadow is False
