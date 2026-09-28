"""离线冒烟：M3 的四类页面结构（真实 Chromium + 本地 HTTP 服务，零模型调用）。

1. iframe      —— 子页里的按钮要能被索引、被点到（帧内解析）
2. shadow DOM  —— open shadow 树里的按钮与文本要进快照，点击要穿透
3. 遮挡负例    —— 被 light DOM 盖住的影子按钮必须判为不可达，不许假成功
4. 新标签      —— 点开 target=_blank 要跟过去，页面事实进 trace
5. 原生对话框  —— alert/confirm/prompt 按策略应答，confirm/prompt 记缺陷候选

用法：
    uv run python scripts/smoke_m3.py [--headed] [--only tab]
"""

from __future__ import annotations

import argparse
import base64
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
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
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

    def decide(self, state: State, goal: str, rules=None) -> Decision:
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


def run_case(tag: str, goal: str, script, checks: list[str], base: str, ui_mode: str,
             vars: dict | None = None, **over):
    settings = Settings(ui_agent_mode=ui_mode, ui_agent_supervisor=False, **over)
    driver = Driver(settings)
    runner = Runner(settings, goal, f"{base}/m3_fixture.html",
                    checks=[parse_check(c) for c in checks], task=f"m3-{tag}",
                    driver=driver, engine=ScriptedEngine(script), vars=vars)
    result = runner.run()
    print(f"    产物：{result.run_dir}")
    return result


def read_trace(run_dir: Path) -> list[dict]:
    text = (run_dir / "trace.jsonl").read_text(encoding="utf-8").strip()
    return [json.loads(line) for line in text.splitlines()] if text else []


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def snap_elements(run_dir: Path, step: int) -> list[dict]:
    return read_json(run_dir / "snapshots" / f"{step:03d}.json").get("elements", [])


def by_label(elements: list[dict], label: str) -> dict:
    hits = [el for el in elements if flat(label) == flat(el.get("label", ""))]
    return hits[0] if hits else {}


def frame_case(base: str, ui_mode: str) -> None:
    print("\n[1 iframe] 点子页（iframe）里的按钮")
    result = run_case("iframe", "点一下子页里的按钮", [("CLICK", "子页按钮"), ("DONE", None)],
                      ["text_contains=子页按钮已点击"], base, ui_mode)
    check("[iframe] 运行 ok 且断言通过", result.success, f"{result.status} {result.detail}")
    element = by_label(snap_elements(result.run_dir, 0), "子页按钮")
    check("[iframe] 快照把子页元素标了 frame", "m3_child.html" in element.get("frame", ""),
          json.dumps(element, ensure_ascii=False))
    text = read_json(result.run_dir / "snapshots" / "001.json").get("page", {}).get("text", "")
    check("[iframe] 子页的文本进了合并文本", "子页按钮已点击" in text, text[-200:])


def iframe_upload_case(base: str, ui_mode: str) -> None:
    print("\n[2b iframe+上传] 在子页（iframe）里走 drop 通道上传")
    scratch = PROJECT_ROOT / ".artifacts" / "tmp" / "m3-smoke"
    scratch.mkdir(parents=True, exist_ok=True)
    upload = scratch / "m3-drop.png"
    if not upload.exists():
        upload.write_bytes(TINY_PNG)
    result = run_case("iframe-drop", "把材料拖进子页的上传区",
                      [("UPLOAD", "子页拖拽区"), ("DONE", None)],
                      [f"text_contains={upload.name}"], base, ui_mode,
                      vars={"子页拖拽区": str(upload)}, ui_agent_upload_mode="drop")
    check("[子页上传] 运行 ok 且断言通过", result.success, f"{result.status} {result.detail}")
    trace = read_trace(result.run_dir)
    check("[子页上传] 拖拽在子页里派发成功（U2）",
          trace[0]["action"]["ok"] and trace[0]["action"]["level"] == "U2",
          json.dumps(trace[0]["action"], ensure_ascii=False))
    text = read_json(result.run_dir / "snapshots" / "001.json").get("page", {}).get("text", "")
    check("[子页上传] 子页真的收到了文件", upload.name in text, text[-200:])


def shadow_case(base: str, ui_mode: str) -> None:
    print("\n[2 shadow] 点 open shadow 树里的按钮")
    result = run_case("shadow", "点一下影子按钮", [("CLICK", "影子按钮"), ("DONE", None)],
                      ["text_contains=影子区：已点击"], base, ui_mode)
    check("[shadow] 运行 ok 且断言通过", result.success, f"{result.status} {result.detail}")
    element = by_label(snap_elements(result.run_dir, 0), "影子按钮")
    check("[shadow] 快照把影子元素标了 shadow", element.get("shadow") is True,
          json.dumps(element, ensure_ascii=False))
    check("[shadow] 影子元素没有被误判为遮挡", element.get("occluded_by", "") == "",
          json.dumps(element, ensure_ascii=False))
    trace = read_trace(result.run_dir)
    check("[shadow] 点击走的是常规阶梯且成功",
          trace[0]["action"]["ok"] and trace[0]["action"]["level"] in ("L0", "L1"),
          json.dumps(trace[0]["action"], ensure_ascii=False))
    check("[shadow] 没有产生缺陷候选", result.defect_candidates == [],
          json.dumps(result.defect_candidates, ensure_ascii=False))


def covered_case(base: str, ui_mode: str) -> None:
    print("\n[3 遮挡负例] 被 light DOM 盖住的影子按钮：必须判不可达")
    result = run_case("covered", "点一下被盖住的影子按钮", [("CLICK", "被盖住的影子按钮"), ("DONE", None)],
                      ["text_contains=标记：无"], base, ui_mode)
    check("[遮挡] 运行 ok（标记没被改）", result.success, f"{result.status} {result.detail}")
    element = by_label(snap_elements(result.run_dir, 0), "被盖住的影子按钮")
    check("[遮挡] 快照标出了遮挡者", element.get("occluded_by", "") != "",
          json.dumps(element, ensure_ascii=False))
    trace = read_trace(result.run_dir)
    check("[遮挡] 点击失败且记了失败原因",
          trace[0]["action"]["ok"] is False and trace[0]["click_failures"],
          json.dumps(trace[0]["action"], ensure_ascii=False))
    check("[遮挡] 页面标记没有被改（没有假成功）",
          "不该发生" not in read_json(result.run_dir / "snapshots" / "001.json")
          .get("page", {}).get("text", ""), "")


def tab_case(base: str, ui_mode: str) -> None:
    print("\n[4 新标签] 点开后跟随新标签页")
    result = run_case("tab", "在新标签里打开子页", [("CLICK", "在新标签打开子页"), ("DONE", None)],
                      ["url_contains=m3_child.html"], base, ui_mode)
    check("[新标签] 运行 ok 且断言通过", result.success, f"{result.status} {result.detail}")
    trace = read_trace(result.run_dir)
    facts = trace[0].get("page_facts") or []
    check("[新标签] trace 记了页面事实", any(f.get("fact") == "tab" for f in facts),
          json.dumps(facts, ensure_ascii=False))
    url = read_json(result.run_dir / "snapshots" / "001.json").get("page", {}).get("url", "")
    check("[新标签] 当前页已经切到子页", "m3_child.html" in url, url)
    actions = read_json(result.run_dir / "actions.json")
    check("[新标签] 动作流里有一行页面事实",
          any(a.get("action") == "PAGE" for a in actions), json.dumps(actions[-1:], ensure_ascii=False))


def dialog_case(tag: str, label: str, policy: str, expected: str, kind: str, base: str, ui_mode: str) -> None:
    print(f"\n[5 对话框 {tag}] {label} · 策略 {policy} · 期望 {expected}")
    result = run_case(f"dialog-{tag}", f"点一下{label}", [("CLICK", label), ("DONE", None)],
                      [f"text_contains={expected}"], base, ui_mode,
                      ui_agent_dialog_policy=policy)
    check(f"[对话框 {tag}] 运行 ok 且断言通过", result.success, f"{result.status} {result.detail}")
    trace = read_trace(result.run_dir)
    facts = trace[0].get("page_facts") or []
    hit = next((f for f in facts if f.get("kind") == kind), {})
    check(f"[对话框 {tag}] trace 记了 {kind} 与应答方式", hit.get("handled") == policy,
          json.dumps(facts, ensure_ascii=False))
    if kind == "alert":
        check(f"[对话框 {tag}] alert 不产生缺陷候选", result.defect_candidates == [],
              json.dumps(result.defect_candidates, ensure_ascii=False))
    else:
        check(f"[对话框 {tag}] {kind} 记成缺陷候选（配置代替了用户）",
              any(kind in item for item in result.defect_candidates),
              json.dumps(result.defect_candidates, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true", help="可视化跑一遍（会弹窗）")
    parser.add_argument("--only", default="", help="只跑某一类：iframe/shadow/covered/tab/dialog")
    args = parser.parse_args()

    base, httpd = serve(EXAMPLES)
    ui_mode = "debug" if args.headed else "ci"
    print(f"[0] 夹具 {base}/m3_fixture.html · 模式 {'可视化' if args.headed else '无头'}")
    try:
        if args.only in ("", "iframe"):
            frame_case(base, ui_mode)
            iframe_upload_case(base, ui_mode)
        if args.only in ("", "shadow"):
            shadow_case(base, ui_mode)
        if args.only in ("", "covered"):
            covered_case(base, ui_mode)
        if args.only in ("", "tab"):
            tab_case(base, ui_mode)
        if args.only in ("", "dialog"):
            dialog_case("alert", "弹个提示", "dismiss", "提示已关掉", "alert", base, ui_mode)
            dialog_case("confirm-dismiss", "弹个确认", "dismiss", "确认返回了 false", "confirm", base, ui_mode)
            dialog_case("confirm-accept", "弹个确认", "accept", "确认返回了 true", "confirm", base, ui_mode)
            dialog_case("prompt-accept", "弹个输入", "accept", "输入返回：默认值", "prompt", base, ui_mode)
    finally:
        httpd.shutdown()

    print("")
    if FAILURES:
        print(f"冒烟失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("冒烟通过：iframe / shadow / 遮挡 / 新标签 / 对话框 都在真实浏览器上跑通，零模型调用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
