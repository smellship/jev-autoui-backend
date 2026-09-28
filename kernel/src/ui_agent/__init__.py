"""ui_agent: LLM 编排 + Jev 决策 + Playwright 执行。"""

import sys

# 中文 Windows 控制台/管道默认 GBK，统一为 UTF-8，避免日志与报告乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure") and (_stream.encoding or "").lower() not in ("utf-8", "utf8"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

__version__ = "0.1.0"
