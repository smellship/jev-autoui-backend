"""Playwright 驱动：双模式（debug 可视化 / ci 无头）、同一 Chromium 二进制、点击降级阶梯。

模型输出永远不进入这里的选择器——索引只用来查内部 locator。
"""

from __future__ import annotations

import base64
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import BrowserContext, Frame, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from ui_agent.config import Settings
from ui_agent.driver.ladder import LadderOutcome, run_ladder

JS_DIR = Path(__file__).resolve().parents[1] / "observe" / "js"
SNAPSHOT_JS = (JS_DIR / "snapshot.js").read_text(encoding="utf-8")
GUARD_JS = (JS_DIR / "guard.js").read_text(encoding="utf-8")
ACT_JS_DIR = Path(__file__).resolve().parents[1] / "act" / "js"
DROP_FILE_JS = (ACT_JS_DIR / "drop_file.js").read_text(encoding="utf-8")

MIME_BY_SUFFIX = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
                  ".jpeg": "image/jpeg", ".txt": "text/plain"}


@dataclass(frozen=True)
class FileRef:
    """上传载荷：真实路径只在驱动内部用，模型与产物只看到文件名与大小。"""

    path: Path
    mime: str = ""

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def size(self) -> int:
        return self.path.stat().st_size


def file_ref(path) -> FileRef:
    path = Path(path)
    return FileRef(path=path, mime=MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream"))


def wait_until_stable(sample: Callable[[], dict], clock: Callable[[], float],
                      sleep: Callable[[float], None], stable_s: float, timeout_s: float,
                      interval_s: float = 0.25) -> dict:
    """反复采样直到指纹连续 stable_s 秒不变（或超时），返回最后一次采样。

    看的是"安静了多久"而不是"两次一样"：SPA 常见壳先渲染、内容后到，两次一样并不代表渲染完了。
    """
    started = clock()
    raw = sample()
    last_change = started
    while True:
        now = clock()
        if now - last_change >= stable_s or now - started >= timeout_s:
            return raw
        sleep(interval_s)
        nxt = sample()
        if nxt.get("fingerprint") != raw.get("fingerprint"):
            last_change = clock()
        raw = nxt


class StaleTarget(RuntimeError):
    """索引不在当次快照中（页面已变）。"""


class Driver:
    def __init__(self, settings: Settings, clock: Callable[[], float] = time.monotonic):
        self.s = settings
        self._pw = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._frames: dict[int, Frame] = {}
        self._clock = clock
        self.zoom_note = ""
        self.paint_note = ""
        self._opened: list[Page] = []      # 动作派生的新标签（事件处理器只登记，切换在 settle 里做）
        self._closed: list[Page] = []
        self._notes: list[str] = []        # 页面级事实（新标签/切回），由 runner 收进 trace
        self._dialogs: list[dict] = []     # 原生对话框事实

    # --- 生命周期 ---

    def start(self) -> None:
        self.s.profile_dir.mkdir(parents=True, exist_ok=True)
        w, h = self.s.viewport
        self._pw = sync_playwright().start()
        self.context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.s.profile_dir),
            channel="chromium",  # 完整 Chromium：无头也不落到 headless shell，两种模式同一个 exe
            headless=self.s.headless,
            slow_mo=0 if self.s.headless else self.s.slow_mo,
            viewport={"width": w, "height": h},
            device_scale_factor=1,
            args=[f"--window-size={w},{h}"],
        )
        pages = self.context.pages
        self.page = pages[0] if pages else self.context.new_page()
        for extra in pages[1:]:
            extra.close()  # 只留一个标签页，避免决策上下文漂移
        self.context.on("page", self._on_page)
        self.page.on("dialog", self._on_dialog)  # 不挂处理器时 Playwright 会静默自动关掉对话框
        self.page.on("close", self._on_page_close)
        self.normalize_zoom()

    def close(self) -> None:
        if self.context is not None:
            self.context.close()
        if self._pw is not None:
            self._pw.stop()
        self.context = None
        self._pw = None

    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        self.normalize_zoom()

    def reload(self) -> None:
        self.page.reload(wait_until="domcontentloaded", timeout=30000)

    # --- 新标签 / 原生对话框（事件处理器内不做往返调用，只登记事实） ---

    def _on_page(self, page: Page) -> None:
        page.on("dialog", self._on_dialog)
        page.on("close", self._on_page_close)
        self._opened.append(page)

    def _on_page_close(self, page: Page) -> None:
        self._closed.append(page)

    def _on_dialog(self, dialog) -> None:
        """原生对话框：按策略应答，并把"出现过什么"变成可对账的事实。

        默认 dismiss：不阻塞、不改页面状态；accept 会真的点确定，必须显式配置。
        """
        accept = self.s.ui_agent_dialog_policy == "accept"
        item = {"kind": dialog.type, "message": (dialog.message or "")[:200],
                "handled": "accept" if accept else "dismiss"}
        if dialog.type == "prompt":
            item["default_value"] = (dialog.default_value or "")[:100]
        try:
            if accept and dialog.type == "prompt":
                dialog.accept(dialog.default_value or "")
            elif accept:
                dialog.accept()
            else:
                dialog.dismiss()
        except PlaywrightError:
            item["handled"] = "failed"
        self._dialogs.append(item)

    def _sync_pages(self) -> None:
        """动作之后对齐"当前页"：新开的跟过去，当前页被关掉就切回剩下的一个。"""
        target: Page | None = None
        if self._closed:
            closed = set(self._closed)
            self._closed.clear()
            if self.page in closed:
                target = next((p for p in reversed(self.context.pages) if p not in closed), None)
                if target is not None:
                    self._notes.append(f"当前标签页已关闭，切回：{target.url or '（空）'}")
        if self._opened:
            fresh = next((p for p in reversed(self._opened) if not p.is_closed()), None)
            self._opened.clear()
            if fresh is not None and fresh is not self.page:
                target = fresh
                self._notes.append(f"跟随新标签页：{fresh.url or '（空）'}")
        if target is None or target is self.page:
            return
        self.page = target
        try:
            self.page.wait_for_load_state("domcontentloaded",
                                          timeout=max(1000, self.s.settle_ms * 3))
        except PlaywrightError:
            pass

    def take_notes(self) -> list[str]:
        notes, self._notes = self._notes, []
        return notes

    def take_dialogs(self) -> list[dict]:
        dialogs, self._dialogs = self._dialogs, []
        return dialogs

    # --- 首帧就绪门 / 空白页恢复 ---

    def wait_first_paint(self) -> dict:
        """首帧：等渲染稳定；稳定后仍然一个元素都没有（空白页）就刷新一次再等。

        只做"等一下、必要时刷新一次"，不猜内容；超时后原样交回，由决策层按事实处理。
        """
        raw = self.wait_rendered()
        if raw.get("elements"):
            return raw
        self.paint_note = "首屏稳定后仍为空白页，已刷新一次"
        self.reload()
        return self.wait_rendered()

    def wait_rendered(self) -> dict:
        """等指纹连续安静一段时间，返回最后一次采样（超时就用当前快照）。"""
        return wait_until_stable(self.snapshot, self._clock, self._sleep,
                                 self.s.ready_stable_ms / 1000, self.s.ready_timeout_ms / 1000)

    def _sleep(self, seconds: float) -> None:
        self.page.wait_for_timeout(int(seconds * 1000))

    # --- 缩放的归一化与记录（"比例问题导致点不到"的第一道防线） ---

    def normalize_zoom(self) -> dict:
        try:
            self.page.keyboard.press("Control+0")
        except PlaywrightError:
            pass
        info = self.page.evaluate("() => ({dpr: devicePixelRatio, w: innerWidth, h: innerHeight})")
        w, h = self.s.viewport
        if (info["w"], info["h"]) != (w, h):
            self.zoom_note = f"viewport 与配置不一致：实际 {info['w']}x{info['h']}，配置 {w}x{h}"
        return info

    # --- 快照 ---

    def snapshot(self) -> dict:
        self._frames.clear()
        frames_raw: list[dict] = []
        elements: list[dict] = []
        total = 0
        for frame in self.page.frames:
            budget = max(0, self.s.max_elements - total)
            if budget == 0:
                break
            try:
                data = frame.evaluate(
                    SNAPSHOT_JS,
                    {"start": total, "maxElements": budget, "maxText": self.s.max_text},
                )
            except PlaywrightError:
                continue  # 帧可能在导航或已销毁
            if not data:
                continue
            is_main = frame == self.page.main_frame
            for item in data["elements"]:
                item["frame"] = "" if is_main else frame.url
                self._frames[item["index"]] = frame
            elements.extend(data["elements"])
            total += len(data["elements"])
            frames_raw.append(data)

        empty_page = {"url": "", "title": "", "text": "", "viewport": {}, "scroll": {}}
        main = frames_raw[0]["page"] if frames_raw else empty_page
        text_parts = [f["page"]["text"] for f in frames_raw if f["page"]["text"]]
        if len(text_parts) > 1:
            text_parts = [text_parts[0]] + [f"[frame] {p}" for p in text_parts[1:]]
        text = "\n".join(text_parts)[: self.s.max_text]
        fingerprint = hashlib.sha1(
            "|".join(f["frame_url"] + "#" + f["fingerprint"] for f in frames_raw).encode("utf-8")
        ).hexdigest()[:12]
        return {
            "page": {**main, "text": text, "url": self.page.url},
            "elements": elements,
            "fingerprint": fingerprint,
            "omitted": max(0, total - self.s.max_elements),
        }

    # --- 目标解析与守卫 ---

    def frame_of(self, index: int) -> Frame:
        frame = self._frames.get(index)
        if frame is None:
            raise StaleTarget(f"索引 {index} 不在当次快照中")
        return frame

    def locator(self, index: int):
        return self.frame_of(index).locator(f'[data-uiagent-idx="{index}"]')

    def guard(self, index: int) -> dict:
        return self.frame_of(index).evaluate(GUARD_JS, {"index": index})

    # --- 动作 ---

    def click(self, index: int, allow_force: bool = False) -> LadderOutcome:
        loc = self.locator(index)
        w, h = self.s.viewport

        def l0() -> None:
            loc.click(timeout=3000)

        def l1() -> None:
            loc.scroll_into_view_if_needed(timeout=2000)
            loc.click(timeout=3000)

        def l2() -> None:
            self.normalize_zoom()
            loc.click(timeout=2000)

        def l3() -> None:
            box = loc.bounding_box(timeout=2000)
            if box is None:
                raise RuntimeError("bounding_box 为空")
            info = self.guard(index)
            if not info.get("reachable"):
                raise RuntimeError(f"命中测试未通过（{info.get('reason')}）")
            x = min(max(box["x"] + box["width"] / 2, 1), w - 1)
            y = min(max(box["y"] + box["height"] / 2, 1), h - 1)
            self.page.mouse.click(x, y)

        def l4() -> None:
            info = self.guard(index)
            if info.get("ok") and not info.get("reachable"):
                # 真正的遮挡：force 也只是跳过 Playwright 的检查，事件仍被上层元素接收
                raise RuntimeError("命中测试未通过：点击会被其他元素接收")
            loc.click(force=True, timeout=2000)

        def l5() -> None:
            loc.evaluate("el => el.click()")

        steps = [("L0", l0), ("L1", l1), ("L2", l2), ("L3", l3)]
        if allow_force:  # L4/L5 只在模型显式选择 CLICK_FORCE 时提供，结果会被标记为缺陷候选
            steps += [("L4", l4), ("L5", l5)]
        return run_ladder(steps)

    def type_text(self, index: int, text: str) -> LadderOutcome:
        loc = self.locator(index)

        def t0() -> None:
            loc.fill(text, timeout=3000)

        def t1() -> None:
            self.click(index)
            loc.fill(text, timeout=2000)

        def t2() -> None:
            loc.click(force=True, timeout=2000)
            loc.press_sequentially(text, delay=30, timeout=5000)

        return run_ladder([("T0", t0), ("T1", t1), ("T2", t2)])

    def select(self, index: int, dom_index: int) -> None:
        self.locator(index).select_option(index=dom_index, timeout=5000)

    def upload(self, index: int, files: list[FileRef], mode: str = "input") -> LadderOutcome:
        """A=input 注入（协议层设置 FileList，隐藏控件也能用）；C=页内合成拖拽事件。"""

        def u1() -> None:
            self.locator(index).set_input_files([str(f.path) for f in files], timeout=8000)

        def u2() -> None:
            payload = {"index": index,
                       "files": [{"name": f.name, "type": f.mime,
                                  "b64": base64.b64encode(f.path.read_bytes()).decode("ascii")}
                                 for f in files]}
            # 在目标自己的 frame 里派发：索引属于哪个文档，事件就得在哪个文档里合成
            result = self.frame_of(index).evaluate(DROP_FILE_JS, payload)
            if not result or not result.get("ok"):
                raise RuntimeError(f"拖拽派发失败：{(result or {}).get('reason') or '未知原因'}")

        steps = {"input": [("U1", u1)], "drop": [("U2", u2)]}.get(mode)
        if steps is None:
            raise ValueError(f"未知的上传方式：{mode}（可用 input / drop）")
        return run_ladder(steps)

    def hover(self, index: int) -> None:
        self.locator(index).hover(timeout=3000)

    def press_key(self, key: str) -> None:
        self.page.keyboard.press(key)

    def scroll(self, direction: str, amount: int = 560) -> None:
        self.page.mouse.wheel(0, amount if direction == "down" else -amount)
        self.page.wait_for_timeout(200)

    def scroll_to(self, index: int) -> None:
        self.locator(index).scroll_into_view_if_needed(timeout=3000)

    def wait(self, ms: int) -> None:
        self.page.wait_for_timeout(min(ms, 5000))

    def settle(self, ms: int | None = None) -> None:
        """动作之后给页面一点时间落定：先让派生事件（新标签/对话框）派发，再等 DOM 就绪与渲染。"""
        budget = self.s.settle_ms if ms is None else ms
        self.page.wait_for_timeout(budget)
        self._sync_pages()
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=budget)
        except PlaywrightError:
            pass
        self.page.wait_for_timeout(budget)

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path))

    def evaluate(self, js: str, arg=None, index: int | None = None):
        """求值：给了索引就在该索引所属的 frame 里跑，否则跑在主页面。"""
        if index is None:
            return self.page.evaluate(js, arg)
        return self.frame_of(index).evaluate(js, arg)
