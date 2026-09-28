"""离线冒烟：上传文件的两条通道（真实 Chromium，零模型调用）。

A 通道 = 给隐藏 <input type=file> 注入文件（--upload-mode input，默认）
C 通道 = 页内合成 DataTransfer 拖拽事件（--upload-mode drop）
外加一条负例：页面自己的格式校验必须拒绝，并把错误文案露在页面上。

上传文件默认复用 .artifacts/runs 下已有截图；没有就现场生成一张最小 PNG。
用法：
    uv run python scripts/smoke_upload.py [--headed] [--files a.png b.png]
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ui_agent.config import Settings  # noqa: E402
from ui_agent.driver.playwright_ import Driver  # noqa: E402
from ui_agent.run.runner import Runner  # noqa: E402
from ui_agent.schema.decision import Decision  # noqa: E402
from ui_agent.schema.state import Element, State  # noqa: E402
from ui_agent.verify.asserts import parse_check  # noqa: E402

FIXTURE = PROJECT_ROOT / "examples" / "local_fixture.html"
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


def find(state: State, label: str, role: str = "") -> Element:
    hits = [el for el in state.elements
            if flat(label) in flat(el.label) and (not role or el.role == role)]
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
        if operation in ("DONE", "WAIT"):
            return Decision(operation=operation, confidence=0.99)
        element = find(state, label or "", role="file" if operation == "UPLOAD" else "")
        return Decision(operation=operation, target=str(element.index), target_element=element.index,
                        confidence=0.99)


def pick_files(explicit: list[str], scratch: Path) -> list[Path]:
    if explicit:
        files = [Path(item).resolve() for item in explicit]
    else:
        shots = sorted((PROJECT_ROOT / ".artifacts" / "runs").glob("*/screenshots/*.png"))
        files = [shots[0], shots[-1]] if len(shots) >= 2 else []
        if not files:
            scratch.mkdir(parents=True, exist_ok=True)
            files = []
            for name in ("fixture-1.png", "fixture-2.png"):
                path = scratch / name
                if not path.exists():
                    path.write_bytes(TINY_PNG)
                files.append(path)
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        raise SystemExit(f"上传文件不存在：{'、'.join(missing)}")
    return files


def snapshot_text(run_dir: Path, step: int) -> str:
    path = run_dir / "snapshots" / f"{step:03d}.json"
    if not path.exists():
        return ""
    return json.loads(path.read_text(encoding="utf-8")).get("page", {}).get("text", "")


def run_case(tag: str, goal: str, script, checks: list[str], vars: dict, ui_mode: str, **over):
    settings = Settings(ui_agent_mode=ui_mode, ui_agent_supervisor=False, **over)
    driver = Driver(settings)
    runner = Runner(settings, goal, FIXTURE.as_uri(), checks=[parse_check(c) for c in checks],
                    task=f"upload-{tag}", driver=driver, engine=ScriptedEngine(script), vars=vars)
    result = runner.run()
    print(f"    产物：{result.run_dir}")
    return result


def artifacts_text(run_dir: Path) -> str:
    parts = [(run_dir / "trace.jsonl").read_text(encoding="utf-8"),
             (run_dir / "actions.json").read_text(encoding="utf-8")]
    return "\n".join(parts)


def upload_case(tag: str, mode: str, files: list[Path], ui_mode: str) -> None:
    level = "U1" if mode == "input" else "U2"
    print(f"\n[{tag}] {mode} 通道上传 {len(files)} 个文件，再点提交（{level}）")
    result = run_case(tag, "上传发票文件并提交",
                      [("UPLOAD", "发票文件"), ("CLICK", "提 交"), ("DONE", None)],
                      ["text_contains=提交成功"], {"发票文件": ";".join(str(f) for f in files)},
                      ui_mode, ui_agent_upload_mode=mode)

    check(f"[{tag}] 运行状态 ok 且断言通过", result.success, f"{result.status} {result.detail}")
    trace = [json.loads(line) for line in
             (result.run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    first = trace[0]
    check(f"[{tag}] 上传动作走的是 {level}",
          first["operation"] == "UPLOAD" and first["action"]["ok"] and first["action"]["level"] == level,
          json.dumps(first["action"], ensure_ascii=False))
    text = snapshot_text(result.run_dir, 1)
    check(f"[{tag}] 页面上出现了全部文件名", all(f.name in text for f in files), text[-260:])
    dumped = artifacts_text(result.run_dir)
    check(f"[{tag}] 产物里只有文件名与大小，没有本地路径",
          files[0].name in dumped and str(files[0].parent) not in dumped, "")
    check(f"[{tag}] trace 记录了变量键名与文件清单",
          first.get("upload", {}).get("key") == "发票文件"
          and len(first.get("upload", {}).get("files", [])) == len(files),
          json.dumps(first.get("upload"), ensure_ascii=False))


def negative_case(bad: Path, ui_mode: str) -> None:
    print(f"\n[负例] 上传 {bad.name}（未声明 accept 的控件）：页面自己必须拒绝")
    result = run_case("bad", "上传一个格式不对的补充材料",
                      [("UPLOAD", "补充材料"), ("WAIT", None), ("DONE", None)],
                      ["text_contains=仅支持"], {"补充材料": str(bad)}, ui_mode)

    check("[负例] 断言拿到页面的拒绝文案", result.success, f"{result.status} {result.detail}")
    text = snapshot_text(result.run_dir, 2)
    check("[负例] 文件没有被接受（页面没有出现『已选择』）", "已选择" not in text, text[-260:])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true", help="可视化跑一遍（会弹窗）")
    parser.add_argument("--files", nargs="*", default=[], help="上传用的文件，默认复用已有截图")
    args = parser.parse_args()

    scratch = PROJECT_ROOT / ".artifacts" / "tmp" / "upload-smoke"
    files = pick_files(args.files, scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    bad = scratch / "说明.txt"
    bad.write_text("这不是发票\n", encoding="utf-8")
    ui_mode = "debug" if args.headed else "ci"

    print(f"[0] 夹具 {FIXTURE.name} · 模式 {'可视化' if args.headed else '无头'}")
    for item in files:
        print(f"    上传文件：{item}（{item.stat().st_size} 字节）")
    upload_case("A", "input", files, ui_mode)
    upload_case("C", "drop", files, ui_mode)
    negative_case(bad, ui_mode)

    print("")
    if FAILURES:
        print(f"冒烟失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("冒烟通过：A（input 注入）与 C（合成拖拽）两条通道都在真实浏览器上跑通，零模型调用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
