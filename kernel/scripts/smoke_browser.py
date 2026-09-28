"""无网络冒烟：验证工作区内置 Chromium 在两种模式下都能启动并读 DOM。

用法:
    uv run python scripts\\smoke_browser.py          # 无头（回归模式）
    uv run python scripts\\smoke_browser.py headed   # 可视化（会弹窗约 3 秒）
"""

import os
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure") and (_s.encoding or "").lower() not in ("utf-8", "utf8"):
        _s.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".browsers"))

from playwright.sync_api import sync_playwright  # noqa: E402


def main() -> int:
    headed = len(sys.argv) > 1 and sys.argv[1] == "headed"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chromium", headless=not headed)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.set_content("<h1 id='t'>ui_agent ok</h1>")
        print(f"mode      : {'headed (可视化)' if headed else 'headless (回归)'}")
        print(f"version   : {browser.version}")
        print(f"executable: {browser.browser_type.executable_path}")
        print(f"viewport  : {page.viewport_size}")
        print(f"dpr       : {page.evaluate('window.devicePixelRatio')}")
        print(f"dom read  : {page.eval_on_selector('#t', 'e => e.textContent')}")
        if headed:
            page.wait_for_timeout(3000)
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
