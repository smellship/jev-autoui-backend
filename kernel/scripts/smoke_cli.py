"""离线冒烟：P0 命令行契约（平台按 argv 调的那些），零模型调用。

覆盖：`case compile`（好/坏两版 + exit code + 编译出的 plan 能被 run 吃进去）、
`snapshot --json`（summary 事实集）、`run --json`（目标不可达也要给出合法 JSON 与产物）、
`report --json`。用真子进程调 CLI，验的就是平台看到的那层接口。
用法（默认无头）：
    uv run python scripts/smoke_cli.py [--headed]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ui_agent.llm.planner import plan_from_json  # noqa: E402

FIXTURE = PROJECT_ROOT / "examples" / "local_fixture.html"
FAILURES: list[str] = []

CASE_OK = """\
title: 平台契约冒烟
vars:
  账号: ${{secret.账号}}
  密码: ${{secret.密码}}
steps:
  - goal: 打开页面，用账号密码登录
    checks:
      - text_contains: 已登录
  - goal: 把行政区域选成 2102 大连并查询
    checks:
      - element_value: 行政区域|2102 大连
"""

CASE_BAD = """\
title: 坏用例
steps:
  - goal: 空断言
    checks: []
  - goal: 引用写错地方 ${{secret.账号}}
    checks:
      - text_containsx: 结果
"""


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}{'' if ok else '  ← ' + detail}")
    if not ok:
        FAILURES.append(f"{name} {detail}".strip())


def run_cli(args: list[str], timeout: int = 180) -> tuple[int, str, str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    proc = subprocess.run([sys.executable, "-m", "ui_agent.cli", *args], cwd=PROJECT_ROOT,
                          capture_output=True, timeout=timeout, env=env)
    return (proc.returncode, proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"))


def load_json(text: str, where: str) -> dict:
    """stdout 必须整份就是 JSON：平台就是这么回读的。"""
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{where} 的 stdout 不是纯 JSON：{exc}；开头 {text[:120]!r}") from None
    if not isinstance(data, dict):
        raise AssertionError(f"{where} 的 JSON 顶层不是对象")
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true", help="可视化跑一遍（会弹窗）")
    args = parser.parse_args()
    mode = "debug" if args.headed else "ci"

    with tempfile.TemporaryDirectory(prefix="ui-agent-smoke-") as tmp:
        tmp_dir = Path(tmp)
        good, bad = tmp_dir / "case_ok.yaml", tmp_dir / "case_bad.yaml"
        good.write_text(CASE_OK, encoding="utf-8")
        bad.write_text(CASE_BAD, encoding="utf-8")
        plan_file = tmp_dir / "plan.json"
        run_dir = tmp_dir / "run"

        print("[1] case compile（好用例）")
        code, out, err = run_cli(["case", "compile", "--in", str(good),
                                  "--secret-keys", "账号,密码", "--json"])
        data = load_json(out, "case compile")
        check("退出码 0", code == 0, f"code={code} err={err[-200:]}")
        check("ok=True 且无错误", data.get("ok") is True and data.get("errors") == [], json.dumps(data)[:200])
        plan = data.get("plan") or {}
        check("编译出 2 步计划", len(plan.get("steps") or []) == 2, json.dumps(plan, ensure_ascii=False)[:200])
        check("vars 原样带出引用（值不进模型）", plan.get("vars", {}).get("密码") == "${{secret.密码}}",
              json.dumps(plan.get("vars"), ensure_ascii=False))
        check("断言编译成 kind/value/expected",
              plan["steps"][1]["checks"][0] == {"kind": "element_value", "value": "行政区域",
                                                "expected": "2102 大连", "role": ""},
              json.dumps(plan["steps"][1]["checks"], ensure_ascii=False))

        print("\n[2] 编译结果原样喂给 run --plan-file")
        plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        loaded = plan_from_json(plan_file.read_text(encoding="utf-8"))
        check("plan.json 能被计划执行器读回", [step.goal for step in loaded.steps] ==
              [step["goal"] for step in plan["steps"]], loaded.name)

        print("\n[3] case compile（坏用例：行号 + exit 1）")
        code, out, err = run_cli(["case", "compile", "--in", str(bad), "--json"])
        data = load_json(out, "case compile(bad)")
        codes = {item["code"] for item in data.get("errors") or []}
        check("退出码 1（用例失败，JSON 仍有效）", code == 1, f"code={code}")
        check("报出 checks_empty / ref_in_goal / bad_kind",
              {"checks_empty", "ref_in_goal", "bad_kind"} <= codes, str(sorted(codes)))
        check("每条错误都有行号", all(isinstance(item.get("line"), int) and item["line"] > 0
                                      for item in data["errors"]), json.dumps(data["errors"], ensure_ascii=False))
        check("errors 非空时 plan 为 null", data.get("plan") is None, str(data.get("plan"))[:80])

        print("\n[4] snapshot --json（探针事实集）")
        code, out, err = run_cli(["snapshot", "--url", FIXTURE.as_uri(), "--json", "--mode", mode])
        data = load_json(out, "snapshot")
        summary = data.get("summary") or {}
        check("退出码 0 且有 summary", code == 0 and summary, f"code={code} err={err[-200:]}")
        check("summary 字段齐全（final_url/title/element_count/needs_login/has_password_field）",
              {"final_url", "title", "element_count", "needs_login", "has_password_field"} <= set(summary),
              json.dumps(summary, ensure_ascii=False))
        check("元素数为正、识别出密码框（needs_login）",
              summary.get("element_count", 0) > 0 and summary.get("needs_login") is True
              and summary.get("has_password_field") is True, json.dumps(summary, ensure_ascii=False))
        check("state 仍在（离线预览不丢）", bool((data.get("state") or {}).get("elements")),
              str(len((data.get("state") or {}).get("elements") or [])))

        print("\n[5] run --json（目标不可达：也要给出合法 JSON 与产物）")
        code, out, err = run_cli(["run", "--plan-file", str(plan_file), "--run-dir", str(run_dir),
                                  "--mode", mode, "--url", "http://127.0.0.1:9/", "--json"])
        data = load_json(out, "run")
        check("退出码 1（用例没成功）", code == 1, f"code={code}")
        check("JSON 里 ok=false、status=error", data.get("ok") is False and data.get("status") == "error",
              json.dumps(data, ensure_ascii=False)[:200])
        check("--run-dir 生效：产物落在指定目录",
              (run_dir / "summary.json").exists() and (run_dir / "trace.jsonl").exists()
              and (run_dir / "report.html").exists(), str(sorted(p.name for p in run_dir.iterdir())))
        check("人类话术走 stderr，stdout 干净", "运行目录" in err and "运行目录" not in out,
              f"out={out[:80]!r}")

        print("\n[6] report --json（重出报告）")
        code, out, err = run_cli(["report", "--run-dir", str(run_dir), "--json"])
        data = load_json(out, "report")
        report = Path(str(data.get("report") or ""))
        check("退出码 0 且报告路径有效", code == 0 and report.is_file(), json.dumps(data, ensure_ascii=False))

    print("")
    if FAILURES:
        print(f"冒烟失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("冒烟通过：P0 命令行契约（compile / snapshot / run / report）全部符合平台约定，未调用任何模型")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
