"""调 ui_agent 内核：argv 数组（不过 shell）、JSON 回读、进程树可终止。

约定（spec §4.2/§10）：
- 所有需要结构化返回的调用都带 `--json`，内核把 JSON 打到 stdout；
- 退出码 0 = 成功、1 = 用例失败（JSON 仍有效）、其他 = 内核错误；
- 长跑（run）用 KernelProcess：stdout 落文件，避免管道写满死锁，同时留一份 kernel.log。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from app.core.config import get_settings


class KernelError(Exception):
    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def child_env() -> dict:
    env = os.environ.copy()
    # 内核 print 到管道时默认用 locale 编码（中文 Windows = GBK），强制 UTF-8
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _spawn_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def build_argv(args: list[str]) -> list[str]:
    return [*get_settings().kernel_argv, *args]


def _tail(text: str, size: int = 800) -> str:
    text = text or ""
    return text[-size:]


def parse_json(text: str, argv: list[str]) -> dict:
    body = (text or "").strip()
    if not body:
        raise KernelError("kernel_empty_output", "内核没有输出任何内容", {"argv": argv})
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        data = None
        for line in reversed(body.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        if data is None:
            raise KernelError("kernel_bad_json", "内核输出不是合法 JSON",
                              {"argv": argv, "stdout_tail": _tail(body)})
    if not isinstance(data, dict):
        raise KernelError("kernel_bad_json", "内核 JSON 顶层不是对象", {"argv": argv})
    return data


def call(args: list[str], timeout: int | None = None) -> dict:
    """跑一次短命令，返回 {code, stdout, stderr, argv, duration_ms}。"""
    settings = get_settings()
    argv = build_argv(args)
    started = time.time()
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout or settings.kernel_timeout_s,
                              cwd=settings.kernel_cwd or None, env=child_env())
    except subprocess.TimeoutExpired as exc:
        raise KernelError("kernel_timeout", f"内核调用超时（{timeout or settings.kernel_timeout_s}s）",
                          {"argv": argv}) from exc
    except FileNotFoundError as exc:
        raise KernelError("kernel_missing", f"找不到内核命令：{argv[0]}", {"argv": argv}) from exc
    return {
        "code": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", errors="replace"),
        "stderr": proc.stderr.decode("utf-8", errors="replace"),
        "argv": argv,
        "duration_ms": int((time.time() - started) * 1000),
    }


def call_json(args: list[str], timeout: int | None = None, ok_codes: tuple[int, ...] = (0, 1)) -> dict:
    """跑一次短命令并解析 JSON；退出码不在 ok_codes 里则抛 KernelError。"""
    result = call(args, timeout=timeout)
    if result["code"] not in ok_codes:
        raise KernelError("kernel_failed", f"内核退出码 {result['code']}",
                          {"argv": result["argv"], "exit_code": result["code"],
                           "stderr_tail": _tail(result["stderr"]), "stdout_tail": _tail(result["stdout"])})
    return parse_json(result["stdout"], result["argv"])


def kill_tree(pid: int) -> None:
    """连同子进程一起终止（Windows taskkill /T /F；POSIX 进程组 SIGTERM → SIGKILL）。"""
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, check=False)
        return
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(20):
        time.sleep(0.25)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return


class KernelProcess:
    """长跑进程：输出落 kernel.log，可随时整棵树终止。"""

    def __init__(self, args: list[str], log_path: Path):
        settings = get_settings()
        self.argv = build_argv(args)
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.log_path.open("wb")
        self.proc = subprocess.Popen(
            self.argv, cwd=settings.kernel_cwd or None, env=child_env(),
            stdout=self._handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **_spawn_kwargs(),
        )
        self.pid = self.proc.pid
        self.killed = False

    def wait(self, timeout: int) -> int:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            return self.proc.wait(timeout=30)

    def kill(self) -> None:
        if self.proc.poll() is None:
            kill_tree(self.pid)
            self.killed = True

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self) -> None:
        try:
            self._handle.close()
        except OSError:
            pass

    def output(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def result(self) -> dict:
        """解析 kernel.log 尾部的 JSON；解析不出来时给出 exit/日志尾部。"""
        code = self.proc.returncode if self.proc.returncode is not None else -1
        text = self.output()
        if code not in (0, 1):
            raise KernelError("kernel_failed", f"内核退出码 {code}",
                              {"argv": self.argv, "exit_code": code, "log_tail": _tail(text)})
        return parse_json(text, self.argv)
