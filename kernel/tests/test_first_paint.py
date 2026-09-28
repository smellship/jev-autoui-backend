"""首帧就绪门：壳先渲染、内容后到的窗口不能当真（离线，用假时钟）。"""

from __future__ import annotations

from fakes import settings_for

from ui_agent.driver.playwright_ import Driver, wait_until_stable


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def raw_of(fingerprint: str, labels: list[str] | None = None) -> dict:
    elements = [{"index": i + 1, "role": "button", "label": label} for i, label in enumerate(labels or [])]
    return {"page": {"text": ""}, "elements": elements, "fingerprint": fingerprint}


def test_stable_page_returns_after_the_quiet_window() -> None:
    clock = Clock()
    raw = wait_until_stable(lambda: raw_of("a"), clock.now, clock.sleep, 2.5, 20)
    assert raw["fingerprint"] == "a"
    assert clock.t >= 2.5


def test_shell_that_changes_later_is_not_mistaken_for_ready() -> None:
    clock = Clock()
    seen: list[float] = []

    def sample() -> dict:
        seen.append(clock.t)
        return raw_of("shell" if clock.t < 1.7 else "content")

    raw = wait_until_stable(sample, clock.now, clock.sleep, 2.5, 20)
    assert raw["fingerprint"] == "content", "壳稳定了 1.7 秒，但内容还没到，不能就此放行"
    assert clock.t >= 1.7 + 2.5
    assert seen[0] < 1.7  # 先采到过壳


def test_never_settling_page_gives_up_at_the_timeout() -> None:
    clock = Clock()

    def sample() -> dict:
        return raw_of(f"t{clock.t}")

    raw = wait_until_stable(sample, clock.now, clock.sleep, 2.5, 3)
    assert raw["fingerprint"] == f"t{clock.t}"
    assert 3 <= clock.t <= 3.5


class StubPage:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.reloads = 0

    def reload(self, **kwargs) -> None:
        self.reloads += 1

    def wait_for_timeout(self, ms: int) -> None:
        self.clock.t += ms / 1000


def make_driver(tmp_path, before: dict, after: dict | None = None):
    """页面在 reload 之前一直返回 before，reload 之后返回 after（缺省与 before 相同）。"""
    clock = Clock()
    page = StubPage(clock)
    driver = Driver(settings_for(tmp_path, ready_stable_ms=100, ready_timeout_ms=2000),
                    clock=clock.now)
    driver.page = page
    driver.snapshot = lambda: after if (page.reloads and after is not None) else before  # type: ignore[method-assign]
    return driver, page


def test_blank_page_is_reloaded_once_then_used(tmp_path) -> None:
    driver, page = make_driver(tmp_path, raw_of("blank"), raw_of("content", ["查询"]))
    raw = driver.wait_first_paint()
    assert page.reloads == 1
    assert raw["fingerprint"] == "content"
    assert driver.paint_note


def test_rendered_page_is_not_reloaded(tmp_path) -> None:
    driver, page = make_driver(tmp_path, raw_of("shell", ["查询"]))
    raw = driver.wait_first_paint()
    assert page.reloads == 0
    assert raw["fingerprint"] == "shell"
    assert driver.paint_note == ""


def test_still_blank_after_reload_stops_at_one_reload(tmp_path) -> None:
    driver, page = make_driver(tmp_path, raw_of("blank"))
    raw = driver.wait_first_paint()
    assert page.reloads == 1, "只刷新一次，剩下的交给决策层按事实处理"
    assert not raw["elements"]
