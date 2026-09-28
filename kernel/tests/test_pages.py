"""M3 驱动侧的离线单测：新标签跟随、原生对话框事实化（不起浏览器、不用假驱动）。"""

from __future__ import annotations

import json

from fakes import FakeDriver, FakeEngine, click, done, read_trace, settings_for, snapshot
from playwright.sync_api import Error as PlaywrightError

from ui_agent.driver.playwright_ import Driver
from ui_agent.run.runner import Runner
from ui_agent.verify.asserts import parse_check


class FakePage:
    """只实现 Driver 用到的面：够测"跟随/切回"的判定，不需要真浏览器。"""

    def __init__(self, url: str = "", closed: bool = False):
        self.url = url
        self.closed = closed
        self.handlers: dict[str, list] = {}
        self.load_waits = 0

    def on(self, event, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def is_closed(self) -> bool:
        return self.closed

    def wait_for_load_state(self, state, timeout=None) -> None:
        self.load_waits += 1

    def wait_for_timeout(self, ms) -> None:
        pass


class FakeContext:
    def __init__(self, pages: list[FakePage]):
        self.pages = pages


class FakeDialog:
    def __init__(self, kind: str = "confirm", message: str = "确定要继续吗？", default_value: str = ""):
        self.type = kind
        self.message = message
        self.default_value = default_value
        self.answer: str | None = None

    def accept(self, value=None) -> None:
        self.answer = "accept" if value is None else f"accept:{value}"

    def dismiss(self) -> None:
        self.answer = "dismiss"


def wired(tmp_path, pages: list[FakePage], current: int = 0, **over) -> Driver:
    driver = Driver(settings_for(tmp_path, **over))
    driver.context = FakeContext(pages)
    driver.page = pages[current]
    return driver


class TestNativeDialogs:
    def test_dismissed_by_default_and_recorded_as_a_fact(self, tmp_path):
        driver = Driver(settings_for(tmp_path))
        dialog = FakeDialog("alert", "这是一条提示")
        driver._on_dialog(dialog)

        assert dialog.answer == "dismiss"
        assert driver.take_dialogs() == [{"kind": "alert", "message": "这是一条提示", "handled": "dismiss"}]
        assert driver.take_dialogs() == [], "取过的事实不能重复上报"

    def test_accept_policy_answers_the_page(self, tmp_path):
        driver = Driver(settings_for(tmp_path, ui_agent_dialog_policy="accept"))
        dialog = FakeDialog("confirm", "确定要继续吗？")
        driver._on_dialog(dialog)
        assert dialog.answer == "accept"
        assert driver.take_dialogs()[0]["handled"] == "accept"

    def test_prompt_keeps_the_page_default_value(self, tmp_path):
        driver = Driver(settings_for(tmp_path, ui_agent_dialog_policy="accept"))
        dialog = FakeDialog("prompt", "说点什么", "默认值")
        driver._on_dialog(dialog)
        assert dialog.answer == "accept:默认值"
        assert driver.take_dialogs()[0]["default_value"] == "默认值"

    def test_failed_answer_is_recorded_not_raised(self, tmp_path):
        class Broken(FakeDialog):
            def dismiss(self) -> None:
                raise PlaywrightError("target closed")

        driver = Driver(settings_for(tmp_path))
        driver._on_dialog(Broken("alert", "提示"))
        assert driver.take_dialogs()[0]["handled"] == "failed"


class TestPages:
    def test_new_tab_is_followed_after_the_action(self, tmp_path):
        first, second = FakePage("http://a/"), FakePage("http://b/")
        driver = wired(tmp_path, [first, second])

        driver._on_page(second)  # 事件处理器只登记，不做切换（处理器内不调用 Playwright）
        assert driver.page is first
        assert "dialog" in second.handlers and "close" in second.handlers

        driver._sync_pages()
        assert driver.page is second and second.load_waits == 1
        assert driver.take_notes() == ["跟随新标签页：http://b/"]
        assert driver.take_notes() == []

    def test_closing_the_current_tab_switches_back(self, tmp_path):
        first, second = FakePage("http://a/"), FakePage("http://b/")
        driver = wired(tmp_path, [first, second], current=1)

        second.closed = True
        driver._on_page_close(second)
        driver._sync_pages()
        assert driver.page is first
        assert driver.take_notes() == ["当前标签页已关闭，切回：http://a/"]

    def test_settle_follows_the_new_tab(self, tmp_path):
        """接线点：动作之后的 settle 里才会真的切页。"""
        first, second = FakePage("http://a/"), FakePage("http://b/")
        driver = wired(tmp_path, [first, second])
        driver._on_page(second)
        driver.settle(10)
        assert driver.page is second

    def test_nothing_to_do_when_no_page_events(self, tmp_path):
        first = FakePage("http://a/")
        driver = wired(tmp_path, [first])
        driver.settle(10)
        assert driver.page is first and driver.take_notes() == []


def run_with_facts(tmp_path, notes=(), dialogs=(), checks=("text_contains=查询完成",)):
    settings = settings_for(tmp_path)
    driver = FakeDriver([snapshot("a"), snapshot("b", text="查询完成")])
    driver.s = settings
    driver.notes = list(notes)
    driver.dialogs = list(dialogs)
    engine = FakeEngine([click(), done()])
    runner = Runner(settings, "目标", "http://x/", checks=[parse_check(c) for c in checks],
                    task="t", driver=driver, engine=engine, verbose=False)
    return runner.run(), engine


def test_flags_and_dialogs_become_rows_facts_and_defects(tmp_path):
    result, engine = run_with_facts(
        tmp_path,
        notes=["跟随新标签页：http://b/"],
        dialogs=[{"kind": "confirm", "message": "确定要继续吗？", "handled": "dismiss"}],
    )

    assert result.status == "ok"
    line = read_trace(result.run_dir)[0]
    assert [f["fact"] for f in line["page_facts"]] == ["tab", "dialog"]
    assert line["page_facts"][1]["kind"] == "confirm" and line["page_facts"][1]["handled"] == "dismiss"

    actions = json.loads((result.run_dir / "actions.json").read_text(encoding="utf-8"))
    assert [a["action"] for a in actions] == ["CLICK", "PAGE", "PAGE"]
    assert "confirm" in actions[2]["text"] and "已关闭" in actions[2]["text"]

    # confirm 是"配置替用户按了按钮"，必须记成缺陷候选
    assert len(result.defect_candidates) == 1 and "confirm" in result.defect_candidates[0]

    # 模型下一次决策时能看到这些事实，但事实行不会被当成"一次尝试"
    recent = engine.seen_states[1].recent_actions
    assert recent[-1].action == "PAGE" and recent[-1].page_changed is None
    assert recent[0].action == "CLICK" and recent[0].page_changed is True


def test_alert_is_a_fact_but_not_a_defect(tmp_path):
    result, _ = run_with_facts(tmp_path, dialogs=[{"kind": "alert", "message": "提示", "handled": "dismiss"}])
    assert result.defect_candidates == []


def test_no_facts_no_noise(tmp_path):
    result, engine = run_with_facts(tmp_path)
    assert "page_facts" not in read_trace(result.run_dir)[0]
    assert [a.action for a in engine.seen_states[1].recent_actions] == ["CLICK"]
