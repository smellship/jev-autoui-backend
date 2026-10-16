"""离线冒烟：回放缓存（录制 → 零模型回放 → 零模型失配停机）——真实 Chromium + 本地服务，零模型调用。

1. 录制     —— 脚本引擎跑通并过断言：缓存条目落盘（写入词 saved）
2. 零模型回放 —— 走真 CLI（--decider off）：命中免模型、断言照过；模型一次不问、录像原样保留（kept）
3. 零模型失配 —— 没有录像的新目标：立刻停（cache_missed、退出码 1），缓存目录不多写一个文件

用法：
    uv run python scripts/smoke_replay.py [--headed]
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ui_agent.config import Settings  # noqa: E402
from ui_agent.driver.playwright_ import Driver  # noqa: E402
from ui_agent.replay.cache import CacheStore, goal_case_hash  # noqa: E402
from ui_agent.replay.session import open_session  # noqa: E402
from ui_agent.run.runner import Runner  # noqa: E402
from ui_agent.schema.decision import Decision  # noqa: E402
from ui_agent.schema.state import Element, State  # noqa: E402
from ui_agent.verify.asserts import parse_check  # noqa: E402

EXAMPLES = PROJECT_ROOT / "examples"
FAILURES: list[str] = []
GOAL = "清空模拟行并核对"
CHECK = "element_count=模拟行|0"


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
    """按脚本顶替决策模型的位置：确定性地选操作与目标，零模型调用。"""

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


def run_cli(args: list[str], timeout: int = 180) -> tuple[int, str, str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    proc = subprocess.run([sys.executable, "-m", "ui_agent.cli", *args], cwd=PROJECT_ROOT,
                          capture_output=True, timeout=timeout, env=env)
    return (proc.returncode, proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"))


def load_json(text: str, where: str) -> dict:
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{where} 的 stdout 不是纯 JSON：{exc}；开头 {text[:120]!r}") from None
    if not isinstance(data, dict):
        raise AssertionError(f"{where} 的 JSON 顶层不是对象")
    return data


def read_rows(run_dir: Path) -> list[dict]:
    text = (run_dir / "trace.jsonl").read_text(encoding="utf-8").strip()
    return [json.loads(line) for line in text.splitlines()] if text else []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true", help="可视化跑一遍（会弹窗）")
    args = parser.parse_args()
    ui_mode = "debug" if args.headed else "ci"

    with tempfile.TemporaryDirectory(prefix="ui-agent-replay-smoke-") as tmp:
        cache_dir = Path(tmp) / "cache"
        base, httpd = serve(EXAMPLES)
        url = f"{base}/local_fixture.html"
        os.environ["UI_AGENT_CACHE_DIR"] = str(cache_dir)  # 真 CLI 子进程也读这份录像
        print(f"[0] 夹具 {url} · 模式 {'可视化' if args.headed else '无头'} · 缓存 {cache_dir}")
        try:
            print("\n[1] 录制：脚本引擎跑通并过断言，缓存条目落盘")
            settings = Settings(ui_agent_mode=ui_mode, ui_agent_supervisor=False,
                                ui_agent_cache=True, ui_agent_cache_dir=str(cache_dir))
            specs = [parse_check(CHECK)]
            session = open_session(settings, case_hash=goal_case_hash(GOAL, specs), base_url=url,
                                   plan_name="replay-smoke")
            runner = Runner(settings, GOAL, url, checks=specs, task="replay-smoke",
                            driver=Driver(settings),
                            engine=ScriptedEngine([("CLICK", "清空全部"), ("DONE", None)]),
                            cache=session)
            result = runner.run()
            print(f"    产物：{result.run_dir}")
            check("[1] 录制运行 ok", result.success, f"{result.status} {result.detail}")
            session.run_id = result.run_dir.name
            outcome = session.finalize()
            check("[1] 写入词 saved（断言确认后才落盘）", outcome.word == "saved" and outcome.saved_steps == 1,
                  json.dumps({"word": outcome.word, "saved": outcome.saved_steps}))
            entry_path = CacheStore(cache_dir).path_for(session.entry.key)
            check("[1] 缓存文件已落盘", entry_path.is_file(), str(entry_path))
            before = entry_path.read_text(encoding="utf-8")

            print("\n[2] 零模型回放：CLI --decider off，全命中、不碰模型、录像原样")
            code, out, err = run_cli(["run", "--goal", GOAL, "--url", url, "--check", CHECK,
                                      "--task", "replay-smoke-cli", "--decider", "off",
                                      "--supervisor", "off", "--mode", ui_mode, "--json"])
            data = load_json(out, "run(replay)")
            check("[2] 退出码 0 且 ok=true", code == 0 and data.get("ok") is True,
                  f"code={code} err={err[-200:]}")
            rows = read_rows(Path(str(data.get("run_dir") or "")))
            decision_rows = [row for row in rows if row.get("kind", "decision") == "decision"]
            check("[2] 两次决策都来自录像（model=cache）",
                  [row.get("model") for row in decision_rows] == ["cache", "cache"],
                  json.dumps([row.get("model") for row in decision_rows]))
            check("[2] 模型调用行零条（一次都没问模型）",
                  not [row for row in rows if row.get("kind") == "model"], str(len(rows)))
            assert_rows = [row for row in rows if row.get("kind") == "assert"]
            check("[2] 断言行存在且通过", bool(assert_rows) and assert_rows[-1].get("ok") is True,
                  json.dumps(assert_rows, ensure_ascii=False)[:200])
            cache_rows = [row for row in rows if row.get("kind") == "cache"]
            check("[2] cache 行：mode=self-finalized / write=kept",
                  len(cache_rows) == 1 and cache_rows[0]["mode"] == "self-finalized"
                  and cache_rows[0]["write"] == "kept",
                  json.dumps(cache_rows, ensure_ascii=False))
            check("[2] 录像一个字节没动（kept 不重写）",
                  entry_path.read_text(encoding="utf-8") == before)

            print("\n[3] 零模型失配：没有录像的新目标，立刻停且不写缓存")
            code, out, err = run_cli(["run", "--goal", "全新的目标没有录像", "--url", url,
                                      "--check", CHECK, "--task", "replay-smoke-miss",
                                      "--decider", "off", "--supervisor", "off",
                                      "--mode", ui_mode, "--json"])
            data = load_json(out, "run(miss)")
            check("[3] 退出码 1、status=cache_missed",
                  code == 1 and data.get("status") == "cache_missed",
                  f"code={code} status={data.get('status')} err={err[-200:]}")
            check("[3] 说明里带 no-entry", "no-entry" in (data.get("detail") or ""),
                  str(data.get("detail"))[:120])
            rows = read_rows(Path(str(data.get("run_dir") or "")))
            cache_rows = [row for row in rows if row.get("kind") == "cache"]
            check("[3] cache 行：mode=missed / write=not-written",
                  len(cache_rows) == 1 and cache_rows[0]["mode"] == "missed"
                  and cache_rows[0]["write"] == "not-written",
                  json.dumps(cache_rows, ensure_ascii=False))
            check("[3] 缓存目录仍只有那一条录像",
                  len(list(cache_dir.glob("*.json"))) == 1,
                  str(sorted(p.name for p in cache_dir.iterdir())))
        finally:
            httpd.shutdown()

    print("")
    if FAILURES:
        print(f"冒烟失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("冒烟通过：录制 / 零模型回放（真 CLI）/ 零模型失配停机 都在真实浏览器上跑通，零模型调用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
