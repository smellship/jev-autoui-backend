"""离线冒烟：断言增强（计数 / 属性 / 轮询重试）——真实 Chromium + 本地 HTTP 服务，零模型调用。

1. 计数断言   —— element_count 数快照里的元素个数
2. 计数负例   —— 清空后个数对不上：必须判失败，不许假成功（总闸开但无 key = M0 二次机会）
3. 计数直停   —— 总闸关时的收尾语义：首次断言失败即停，不再给第二次 DONE
4. 属性断言   —— element_attribute 读的是原始属性值（href 原值，不是浏览器解析后的绝对 URL）
5. 轮询重试   —— 异步就绪的文本：首评不过要自动重试，assert 行记下轮次与耗时

用法：
    uv run python scripts/smoke_assert.py [--headed] [--only count|count-fail|count-stop|attr|poll]
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ui_agent.config import Settings  # noqa: E402
from ui_agent.driver.playwright_ import Driver  # noqa: E402
from ui_agent.run.runner import Runner  # noqa: E402
from ui_agent.schema.decision import Decision  # noqa: E402
from ui_agent.schema.state import Element, State  # noqa: E402
from ui_agent.verify.asserts import parse_check  # noqa: E402

EXAMPLES = PROJECT_ROOT / "examples"
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}{'' if ok else '  ← ' + detail}")
    if not ok:
        FAILURES.append(f"{name} {detail}".strip())


def flat(text: str) -> str:
    return "".join(text.split())


def find(state: State, label: str) -> Element:
    hits = [el for el in state.elements if flat(label) in flat(el.label)]
    if not hits:
        raise AssertionError(f"夹具里没有『{label}』：{[el.label for el in state.elements]}")
    return hits[0]


class ScriptedEngine:
    """按脚本顶替 Jev 的决策位置：确定性地选操作与目标，零模型调用。"""

    def __init__(self, script: list[tuple[str, str | None]]):
        self.script = list(script)
        self.calls: list[str] = []

    def decide(self, state: State, goal: str, rules=None, error_context=None) -> Decision:
        if not self.script:
            raise AssertionError(f"『{goal}』的脚本用完了（第 {len(self.calls) + 1} 次决策）")
        operation, label = self.script.pop(0)
        self.calls.append(operation)
        if operation in ("DONE", "WAIT", "BLOCKED"):
            return Decision(operation=operation, confidence=0.99)
        element = find(state, label or "")
        return Decision(operation=operation, target=str(element.index), target_element=element.index,
                        confidence=0.99)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


def serve(directory: Path) -> tuple[str, socketserver.TCPServer]:
    handler = functools.partial(QuietHandler, directory=str(directory))
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd


def run_case(tag: str, goal: str, script, checks: list[str], base: str, ui_mode: str, **over):
    # 默认关总闸（升级点直接停）；要用 M0 二次机会的用例在 over 里传 ui_agent_supervisor=True
    settings = Settings(ui_agent_mode=ui_mode, **{"ui_agent_supervisor": False, "ui_agent_cache": False, **over})
    driver = Driver(settings)
    runner = Runner(settings, goal, f"{base}/local_fixture.html",
                    checks=[parse_check(c) for c in checks], task=f"assert-{tag}",
                    driver=driver, engine=ScriptedEngine(script))
    result = runner.run()
    print(f"    产物：{result.run_dir}")
    return result


def read_rows(run_dir: Path) -> list[dict]:
    """不筛层：本脚本要看 assert 行自己的轮次与耗时。"""
    text = (run_dir / "trace.jsonl").read_text(encoding="utf-8").strip()
    return [json.loads(line) for line in text.splitlines()] if text else []


def assert_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row.get("kind") == "assert"]


def count_case(base: str, ui_mode: str) -> None:
    print("\n[1 count] 数一数『模拟行』有几个：期望 3")
    result = run_case("count", "核对模拟行有三个", [("DONE", None)],
                      ["element_count=模拟行|3"], base, ui_mode)
    check("[count] 运行 ok 且断言通过", result.success, f"{result.status} {result.detail}")
    rows = assert_rows(read_rows(result.run_dir))
    check("[count] 有且只有一行 assert 行", len(rows) == 1, json.dumps(rows, ensure_ascii=False))
    row = rows[-1] if rows else {}
    check("[count] 首评即过（attempts=1）", row.get("attempts") == 1,
          json.dumps(row, ensure_ascii=False))
    evidence = (row.get("checks") or [{}])[0].get("evidence", "")
    check("[count] 证据里写了匹配到 3 个", "匹配到 3 个" in evidence, evidence)


def count_fail_case(base: str, ui_mode: str) -> None:
    print("\n[2 count-fail] 清空后再数：期望 3、实际 0，必须判失败")
    # 总闸开但没配 key：监督 present-but-unavailable，走 M0「再给一次机会」→ 两次 DONE 各留一行 assert
    result = run_case("count-fail", "清空后核对模拟行仍有三个",
                      [("CLICK", "清空全部"), ("DONE", None), ("DONE", None)],
                      ['{"kind":"element_count","value":"模拟行","expected":"3"}'],
                      base, ui_mode, ui_agent_supervisor=True, ui_agent_assert_timeout_ms=0)
    check("[count-fail] 状态是 check_failed", result.status == "check_failed",
          f"{result.status} {result.detail}")
    check("[count-fail] 结果断言列表全是失败", [c.ok for c in result.checks] == [False],
          json.dumps([c.model_dump() for c in result.checks], ensure_ascii=False))
    rows = assert_rows(read_rows(result.run_dir))
    check("[count-fail] 两次 DONE 各留一行 assert，都是失败",
          len(rows) == 2 and all(not row.get("ok") for row in rows),
          json.dumps(rows, ensure_ascii=False))
    check("[count-fail] 窗口 0 = 只评一次（attempts=1）",
          all(row.get("attempts") == 1 for row in rows), json.dumps(rows, ensure_ascii=False))
    evidence = (rows[-1].get("checks") or [{}])[0].get("evidence", "") if rows else ""
    check("[count-fail] 证据写了实际匹配 0 个", "实际匹配 0 个" in evidence, evidence)


def count_stop_case(base: str, ui_mode: str) -> None:
    print("\n[3 count-stop] 总闸关：第一次断言失败即直停，不再给第二次 DONE")
    # 脚本只备两次决策：若直停语义被破坏（又给一次机会），第三次决策会因脚本用尽而报错
    result = run_case("count-stop", "清空后核对模拟行仍有三个",
                      [("CLICK", "清空全部"), ("DONE", None)],
                      ["element_count=模拟行|3"], base, ui_mode,
                      ui_agent_assert_timeout_ms=0)
    check("[count-stop] 状态是 check_failed", result.status == "check_failed",
          f"{result.status} {result.detail}")
    rows = assert_rows(read_rows(result.run_dir))
    check("[count-stop] 只留一行 assert 且失败", len(rows) == 1 and not rows[0].get("ok"),
          json.dumps(rows, ensure_ascii=False))
    check("[count-stop] 只评一次（attempts=1）",
          bool(rows) and rows[0].get("attempts") == 1, json.dumps(rows, ensure_ascii=False))


def attr_case(base: str, ui_mode: str) -> None:
    print("\n[4 attr] 读手册链接的 href 原始属性值")
    result = run_case("attr", "确认手册链接指向 /help/manual.pdf", [("DONE", None)],
                      ['{"kind":"element_attribute","value":"下载使用手册","attr":"href",'
                       '"expected":"/help/manual.pdf"}'], base, ui_mode)
    check("[attr] 运行 ok 且断言通过", result.success, f"{result.status} {result.detail}")
    rows = assert_rows(read_rows(result.run_dir))
    row = rows[-1] if rows else {}
    check("[attr] 首评即过（attempts=1）", row.get("attempts") == 1,
          json.dumps(row, ensure_ascii=False))
    item = (row.get("checks") or [{}])[0]
    check("[attr] 断言类型是 element_attribute", item.get("kind") == "element_attribute",
          json.dumps(item, ensure_ascii=False))
    check("[attr] 证据是原始属性值（未解析成绝对 URL）",
          item.get("evidence") == "href = /help/manual.pdf", json.dumps(item, ensure_ascii=False))


def poll_case(base: str, ui_mode: str) -> None:
    print("\n[5 poll] 异步就绪的文本（夹具延迟 2.5s）：首评不过要自动重试到过")
    result = run_case("poll", "等异步查询出结果", [("CLICK", "异步查询"), ("DONE", None)],
                      ["text_contains=异步结果已就绪"], base, ui_mode,
                      ui_agent_assert_timeout_ms=5000)
    check("[poll] 运行 ok 且断言通过", result.success, f"{result.status} {result.detail}")
    rows = assert_rows(read_rows(result.run_dir))
    row = rows[-1] if rows else {}
    check("[poll] 确实重试了（attempts≥2）", (row.get("attempts") or 0) >= 2,
          json.dumps(row, ensure_ascii=False))
    evidence = (row.get("checks") or [{}])[0].get("evidence", "")
    check("[poll] 等到的是就绪后的文本", "异步结果已就绪" in evidence, evidence)
    print(f"    轮询事实：attempts={row.get('attempts')} elapsed_ms={row.get('elapsed_ms')}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true", help="可视化跑一遍（会弹窗）")
    parser.add_argument("--only", default="", help="只跑某一类：count/count-fail/count-stop/attr/poll")
    args = parser.parse_args()

    base, httpd = serve(EXAMPLES)
    ui_mode = "debug" if args.headed else "ci"
    print(f"[0] 夹具 {base}/local_fixture.html · 模式 {'可视化' if args.headed else '无头'}")
    try:
        if args.only in ("", "count"):
            count_case(base, ui_mode)
        if args.only in ("", "count-fail"):
            count_fail_case(base, ui_mode)
        if args.only in ("", "count-stop"):
            count_stop_case(base, ui_mode)
        if args.only in ("", "attr"):
            attr_case(base, ui_mode)
        if args.only in ("", "poll"):
            poll_case(base, ui_mode)
    finally:
        httpd.shutdown()

    print("")
    if FAILURES:
        print(f"冒烟失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("冒烟通过：计数 / 计数负例 / 计数直停 / 属性 / 轮询重试 都在真实浏览器上跑通，零模型调用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
