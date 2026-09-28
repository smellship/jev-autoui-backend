"""真内核联调冒烟：平台起真 uvicorn（UI_WEB_KERNEL_CMD 指向 kernel/ 副本），真打 HTTP。

只走零模型路径：
- 用例校验 / 编译 → 内核 `case compile --in … --json`（含坏用例的行号与错误码）；
- 老脚本导入 → 平台导入器（离线）把 midscene 脚本转成 YAML，产物再过一遍真内核 lint；
- 探针 → 内核 `snapshot --url … --json`（打本地夹具页，验证 summary 与页清单）；
- 运行 → 内核 `run --plan-file … --run-dir … --mode ci --url … --json`（入口故意不可达：
  退出码 1 也必须是合法回读，产物要落在平台指定的 run 目录里）；
- 重出报告 → 内核 `report --run-dir … --json`。
数据全落 .smoke-real/；不起前端、不碰生产库、不调模型（没有 API key 也能跑）。

用法：uv run python scripts/smoke_kernel_real.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SMOKE = ROOT / ".smoke-real"
KERNEL = ROOT / "kernel"
FIXTURE_DIR = KERNEL / "examples"
PORT = int(os.environ.get("SMOKE_REAL_PORT", "8794"))
FIXTURE_PORT = int(os.environ.get("SMOKE_REAL_FIXTURE_PORT", "8795"))
BASE = f"http://127.0.0.1:{PORT}"
API = f"{BASE}/api/v1"
FIXTURE_URL = f"http://127.0.0.1:{FIXTURE_PORT}/local_fixture.html"
DEAD_URL = "http://127.0.0.1:8899/"  # 本机没人听的端口：Chromium 直接就拒绝连接（不是 unsafe port）
ACCOUNT, PASSWORD = "admin", "admin123"
SECRET_ACCOUNT = "real-smoke-account-7311"
SECRET_PASSWORD = "real-smoke-password-4402"

CASE_YAML = """\
id: sc-0001-01
title: 真内核联调-登录
priority: P1
tags: [冒烟, 真内核]
env: 本地夹具

vars:
  账号: ${{secret.账号}}
  密码: ${{secret.密码}}

steps:
  - goal: 打开页面并用账号密码登录
    checks:
      - text_contains: 已登录
"""

BAD_YAML = """\
title: 坏用例
vars:
  密码: ${{secret.密码}}
steps:
  - goal: 把口令明文写进来 ${{secret.密码}}
  - goal: 这一步没有断言
"""

MIDSCENE_SCRIPT = """\
import { test, expect } from '@midscene/web';

test('发票查验-数电二手车', async ({ page }) => {
  await page.goto('http://fixture.local/invoice/check');
  await aiInput('25152000000000000001', '发票号码输入框');
  await aiInput('105000.00', '价税合计输入框');
  await aiTap('查验按钮');
  await expect(page.getByText('发票类型：数电票（二手车销售统一发票）')).toBeVisible();
  await expect(page.getByText('发票状态：正常')).toBeVisible();
});
"""


def kernel_cmd() -> str:
    """平台要用的内核命令：同一个 venv 里的控制台脚本（就当 PATH 上的 ui-agent 用）。"""
    exe = Path(sys.executable).parent / ("ui-agent.exe" if os.name == "nt" else "ui-agent")
    if exe.is_file():
        return f'"{exe}"'
    return f'"{sys.executable}" -m ui_agent.cli'


class Smoke:
    def __init__(self) -> None:
        self.client = httpx.Client(base_url=API, timeout=httpx.Timeout(20.0, read=60.0))
        self.results: list[tuple[bool, str, str]] = []
        self.token = ""
        self.ids: dict[str, object] = {}

    def header(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def call(self, method: str, path: str, **kwargs) -> httpx.Response:
        kwargs.setdefault("headers", {}).update(self.header())
        return self.client.request(method, path, **kwargs)

    def step(self, name: str, func) -> None:
        try:
            evidence = func(self) or ""
            self.results.append((True, name, str(evidence)))
        except AssertionError as exc:
            self.results.append((False, name, f"断言失败：{exc}"))
        except Exception as exc:
            self.results.append((False, name, f"{exc.__class__.__name__}: {exc}"))

    def report(self) -> int:
        width = max(len(name) for _, name, _ in self.results)
        print("\n" + "=" * 100)
        for ok, name, evidence in self.results:
            print(f"{'✓' if ok else '✗'} {name.ljust(width)}  {evidence}")
        failed = [name for ok, name, _ in self.results if not ok]
        print("=" * 100)
        print(f"共 {len(self.results)} 项，通过 {len(self.results) - len(failed)}，失败 {len(failed)}")
        return 1 if failed else 0


def expect(response: httpx.Response, status: int, what: str) -> dict:
    assert response.status_code == status, f"{what} 期望 {status}，实际 {response.status_code}：{response.text[:300]}"
    return response.json() if response.content else {}


def wait_run(smoke: Smoke, run_id: str, timeout: float = 300.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = expect(smoke.call("GET", f"/runs/{run_id}"), 200, "运行详情")
        if data["status"] not in ("queued", "running"):
            return data
        time.sleep(0.4)
    raise TimeoutError(f"运行 {run_id} 超时未结束")


class SilentHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # 夹具服务的访问日志不参与结论
        pass


def start_fixture() -> ThreadingHTTPServer:
    handler = partial(SilentHandler, directory=str(FIXTURE_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", FIXTURE_PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def start_server() -> subprocess.Popen:
    env = os.environ.copy()
    env.update({
        "UI_WEB_DB_URL": f"sqlite:///{(SMOKE / 'smoke.db').as_posix()}",
        "UI_WEB_ARTIFACTS_DIR": str(SMOKE / "artifacts"),
        "UI_WEB_SECRETS_DIR": str(SMOKE / "secrets"),
        "UI_WEB_KERNEL_CMD": kernel_cmd(),
        "UI_WEB_KERNEL_CWD": str(KERNEL),
        "UI_WEB_RETENTION_ENABLED": "false",
        "UI_WEB_JWT_SECRET": "smoke-real-only-secret",
    })
    env.pop("UI_WEB_ADMIN_PASSWORD", None)
    log = (SMOKE / "server.log").open("wb")
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    return subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                             "--port", str(PORT), "--log-level", "warning"],
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, **kwargs)


def stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def wait_health(timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{BASE}/api/v1/health", timeout=3).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise TimeoutError("平台没起来，看 .smoke-real/server.log")


# --- 各步骤 ---


def step_login(smoke: Smoke) -> str:
    data = expect(smoke.call("POST", "/auth/login", json={"account": ACCOUNT, "password": PASSWORD}), 200, "登录")
    smoke.token = data["token"]
    return "token 已签发"


def step_setup(smoke: Smoke) -> str:
    node = expect(smoke.call("POST", "/nodes", json={"name": "真内核联调"}), 201, "建节点")
    env = expect(smoke.call("POST", "/envs", json={
        "name": "本地夹具", "label": "本地夹具页", "auth": "login_form",
        "secret_keys": ["账号", "密码"], "base_url": DEAD_URL,
    }), 201, "建环境")
    scenario = expect(smoke.call("POST", "/scenarios", json={
        "node_id": node["id"], "name": "联调场景", "priority": "P1", "tags": ["冒烟"],
    }), 201, "建场景")
    case = expect(smoke.call("POST", f"/scenarios/{scenario['id']}/cases", json={"name": "登录"}), 201, "建用例")
    smoke.ids.update({"env": env["id"], "case": case["id"], "node": node["id"]})
    saved = expect(smoke.call("PUT", f"/cases/{case['id']}",
                              json={"yaml_text": CASE_YAML, "version": 1, "comment": "首版"}), 200, "保存用例")
    assert saved["version"] == 2, saved
    return f"环境 #{env['id']}（入口 {DEAD_URL}）· 用例 {case['id']} 已存 v2"


def step_validate_good(smoke: Smoke) -> str:
    data = expect(smoke.call("POST", f"/cases/{smoke.ids['case']}/validate"), 200, "编译预览（真内核 case compile）")
    assert data["ok"] is True and not data["errors"], data
    plan = data["plan"] or {}
    steps = plan.get("steps") or []
    assert len(steps) == 1 and steps[0]["goal"], plan
    assert steps[0]["checks"][0]["kind"] == "text_contains", plan
    assert plan.get("vars", {}).get("账号") == "${{secret.账号}}", plan
    return f"1 步计划，变量引用原样带出；警告 {len(data['warnings'])} 条"


def step_validate_bad(smoke: Smoke) -> str:
    data = expect(smoke.call("POST", f"/cases/{smoke.ids['case']}/validate",
                             json={"yaml_text": BAD_YAML}), 200, "坏用例校验")
    assert data["ok"] is False and data["plan"] is None, data
    codes = {err.get("code") for err in data["errors"]}
    lines = [err.get("line") for err in data["errors"]]
    assert {"ref_in_goal", "checks_empty"} <= codes, codes
    assert all(isinstance(line, int) and line > 0 for line in lines), lines
    return f"{len(data['errors'])} 个错误（行号 {sorted(lines)}）：{'/'.join(sorted(codes))}"


def step_import_midscene(smoke: Smoke) -> str:
    data = expect(smoke.call("POST", f"/cases/{smoke.ids['case']}/imports",
                             json={"script_text": MIDSCENE_SCRIPT}), 200, "导入老脚本")
    report = data["report"]
    assert report["steps"] == 4 and report["checks"] == 5, report
    assert (report["explicit"], report["lookahead"], report["fallback"]) == (2, 3, 0), report
    assert set(report["vars"]) == {"发票号码", "价税合计"}, report
    assert report["issues"] == [], report
    assert data["yaml_text"].count("text_contains") == 2, data["yaml_text"]
    assert data["yaml_text"].count("25152000000000000001") == 1, "输入值只该出现在 vars 里"
    lint = expect(smoke.call("POST", f"/cases/{smoke.ids['case']}/validate",
                             json={"yaml_text": data["yaml_text"]}), 200, "导入产物过真内核 lint")
    assert lint["ok"] is True and not lint["errors"], lint
    assert not lint["warnings"], lint
    return (f"4 步 / 5 条断言（前瞻 3）· 真内核 lint 0 错 0 警告 · "
            f"变量 {report['vars']}，输入值只出现在 vars")


def step_probe(smoke: Smoke) -> str:
    data = expect(smoke.call("POST", f"/envs/{smoke.ids['env']}/probe",
                             json={"url": FIXTURE_URL}), 200, "探针（真内核 snapshot --json）")
    assert data["element_count"] > 0, data
    assert data["needs_login"] is True and data["has_password_field"] is True, data
    pages = expect(smoke.call("GET", f"/envs/{smoke.ids['env']}/pages"), 200, "页清单")
    urls = [item["url"] for item in pages["items"]]
    assert FIXTURE_URL in urls, urls
    return f"元素 {data['element_count']} 个 · 标题「{data['title']}」· 识别为登录页；页清单 {len(urls)} 条"


def step_secrets(smoke: Smoke) -> str:
    expect(smoke.call("PUT", f"/envs/{smoke.ids['env']}/secrets", json={
        "secrets": {"账号": SECRET_ACCOUNT, "密码": SECRET_PASSWORD},
    }), 200, "写密钥（只写不读）")
    envs = expect(smoke.call("GET", "/envs"), 200, "环境列表")
    row = next(item for item in envs["items"] if item["id"] == smoke.ids["env"])
    blob = json.dumps(row, ensure_ascii=False)
    keys = {item["key"] for item in row["secret_keys"]}
    assert keys == {"账号", "密码"}, row
    assert all(item["set"] is True for item in row["secret_keys"]), row
    assert SECRET_ACCOUNT not in blob and SECRET_PASSWORD not in blob, row
    return "键名与「已设置」可见，实际值不出现在响应里（只在运行前解析成 --var）"


def step_run(smoke: Smoke) -> str:
    run = expect(smoke.call("POST", "/runs", json={
        "case_id": smoke.ids["case"], "env_id": smoke.ids["env"], "mode": "ci",
    }), 202, "发起运行")
    smoke.ids["run"] = run["id"]
    data = wait_run(smoke, run["id"])
    assert data["status"] == "error", data
    artifact_dir = SMOKE / "artifacts" / "runs" / run["id"]
    assert artifact_dir.is_dir(), f"内核产物没落在平台指定的目录：{artifact_dir}"
    for name in ("plan.json", "summary.json", "trace.jsonl", "report.html", "kernel.log"):
        assert (artifact_dir / name).exists(), f"缺产物 {name}：{sorted(p.name for p in artifact_dir.iterdir())}"
    summary = json.loads((artifact_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary.get("status") == "error" and summary.get("mode") == "ci", summary
    assert "headless" in summary, summary
    assert data["kernel_status"] == "error", data
    files = len(list(artifact_dir.iterdir()))
    return (f"状态 {data['status']}（内核 status={summary.get('status')}）· 产物 {files} 项 · "
            f"明细：{str(data.get('detail'))[:60]}")


def step_report_and_artifacts(smoke: Smoke) -> str:
    run_id = smoke.ids["run"]
    artifact_dir = SMOKE / "artifacts" / "runs" / run_id
    before = (artifact_dir / "report.html").read_bytes()
    html = smoke.call("GET", f"/runs/{run_id}/artifacts/report.html")
    assert html.status_code == 200 and b"<html" in html.content.lower(), html.status_code
    trav = smoke.call("GET", f"/runs/{run_id}/artifacts/../../app/main.py")
    assert trav.status_code in (400, 404), trav.status_code
    rebuilt = expect(smoke.call("POST", f"/runs/{run_id}/report"), 200, "重出报告（真内核 report --json）")
    assert rebuilt.get("exists") is True and Path(rebuilt["report"]).is_file(), rebuilt
    assert (artifact_dir / "report.html").read_bytes() == before, "重出报告应该覆盖出同一份 HTML"
    return "report.html 可下载 · 路径穿越被拒 · 重出报告覆盖了同一份 HTML"


def step_leak_scan(smoke: Smoke) -> str:
    hits = []
    for path in SMOKE.rglob("*"):
        if not path.is_file() or path.suffix in (".db", ".log"):
            continue
        if (SMOKE / "secrets") in path.parents:
            continue
        blob = path.read_bytes()
        if SECRET_ACCOUNT.encode() in blob or SECRET_PASSWORD.encode() in blob:
            hits.append(str(path.relative_to(SMOKE)))
    assert not hits, f"这些文件里出现了密钥明文：{hits}"
    log = (SMOKE / "server.log").read_text(encoding="utf-8", errors="replace")
    assert "Traceback" not in log, "平台日志里有异常堆栈，看 .smoke-real/server.log"
    return "产物 / 台账 / 平台日志均无密钥明文，服务日志无异常堆栈"


def browser_hint() -> str:
    """内核默认把浏览器目录钉在自己工程根的 .browsers/ 下；没有就给出两条出路。"""
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env:
        return "" if Path(env).is_dir() else f"PLAYWRIGHT_BROWSERS_PATH 指向的目录不存在：{env}"
    if (KERNEL / ".browsers").is_dir():
        return ""
    return (f"找不到浏览器目录：内核会用 {KERNEL / '.browsers'}（不存在）。\n"
            f"  要么按 README 装一次：PLAYWRIGHT_BROWSERS_PATH=\"{KERNEL / '.browsers'}\" "
            f"uv run python -m playwright install chromium\n"
            f"  要么把 PLAYWRIGHT_BROWSERS_PATH 指向本机已有的浏览器目录")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows 控制台默认 GBK
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if not (KERNEL / "src" / "ui_agent" / "cli.py").is_file():
        print(f"✗ 找不到内核副本：{KERNEL}（先跑 scripts/sync_kernel.py）", file=sys.stderr)
        return 2
    hint = browser_hint()
    if hint:
        print(f"✗ {hint}", file=sys.stderr)
        return 2
    shutil.rmtree(SMOKE, ignore_errors=True)
    SMOKE.mkdir(parents=True)
    fixture = start_fixture()
    proc = start_server()
    smoke = Smoke()
    try:
        wait_health()
        print(f"平台已起：{BASE} · 内核命令 {kernel_cmd()} · 夹具 {FIXTURE_URL}")
        smoke.step("[1] 登录与建模", step_login)
        smoke.step("[2] 环境 / 场景 / 用例（真内核台账）", step_setup)
        smoke.step("[3] 用例编译（case compile）", step_validate_good)
        smoke.step("[4] 坏用例：错误码 + 行号 + plan 为 null", step_validate_bad)
        smoke.step("[4b] 老脚本导入（midscene → YAML）+ 真内核 lint", step_import_midscene)
        smoke.step("[5] 探针（snapshot --json）+ 页清单", step_probe)
        smoke.step("[6] 密钥只写不读", step_secrets)
        smoke.step("[7] 运行（run --plan-file --run-dir --mode ci --json）", step_run)
        smoke.step("[8] 产物与重出报告（report --json）", step_report_and_artifacts)
        smoke.step("[9] 密钥明文扫描 + 日志体检", step_leak_scan)
        code = smoke.report()
        print(f"数据目录：{SMOKE}")
        return code
    finally:
        smoke.client.close()
        stop_server(proc)
        fixture.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
