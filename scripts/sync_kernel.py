"""把内核仓库同步成平台内的一份副本（单向：源仓库 → kernel/）。

用法：uv run python scripts/sync_kernel.py [--from <内核仓库路径>] [--dry-run]

- 只在源仓库侧开发，部署前同步一次；副本不带 .git，服务器上就是普通目录树；
- 排除运行态与密钥：.git / .venv / .browsers / .artifacts / .env / 缓存；
- 副本里同名的排除项不会被删（服务器上填的 kernel/.env、装好的 .browsers 都会留下），
  但 __pycache__/.pytest_cache/.ruff_cache 这类缓存会顺手清掉；
- 每次同步写 kernel/KERNEL_SOURCE.md：来源仓库、commit、时间、源仓库是否有未提交改动。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = ROOT.parents[1] / "ui_agent"
DEST = ROOT / "kernel"

EXCLUDE_DIRS = {".git", ".venv", ".browsers", ".artifacts", "__pycache__", ".pytest_cache",
                ".ruff_cache", ".mypy_cache", "dist", "build", ".idea", ".vscode"}
EXCLUDE_FILES = {".env", ".DS_Store"}
# 这些只在副本里被跑出来、不值得跟着上传，同步时顺手删掉（.env/.browsers/.venv 等运行态要留）
CACHE_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}


def skipped(rel: Path) -> bool:
    if any(part in EXCLUDE_DIRS or part.endswith(".egg-info") for part in rel.parts[:-1]):
        return True
    name = rel.name
    return name in EXCLUDE_FILES or name.endswith(".pyc") or name.endswith(".egg-info")


def source_files(src: Path) -> dict[Path, Path]:
    out: dict[Path, Path] = {}
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        if skipped(rel):
            continue
        out[rel] = path
    return out


def prune(dest: Path, keep: set[Path]) -> list[Path]:
    removed: list[Path] = []
    for path in sorted(dest.rglob("*"), reverse=True):
        rel = path.relative_to(dest)
        if any(part in CACHE_DIRS for part in rel.parts):
            if path.is_dir():
                shutil.rmtree(path)
                removed.append(rel)
            continue
        if skipped(rel):
            continue
        if path.is_file() and rel not in keep:
            path.unlink()
            removed.append(rel)
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    return removed


def git_info(src: Path) -> dict:
    def run(*args: str) -> str:
        try:
            proc = subprocess.run(["git", "-C", str(src), *args], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return proc.stdout.decode("utf-8", errors="replace").strip() if proc.returncode == 0 else ""

    return {"commit": run("rev-parse", "HEAD"), "remote": run("config", "--get", "remote.origin.url"),
            "dirty": bool(run("status", "--porcelain"))}


def write_provenance(dest: Path, src: Path, info: dict, count: int) -> None:
    dirty = "有（副本不是源仓库某个干净提交的快照）" if info["dirty"] else "无"
    dest.joinpath("KERNEL_SOURCE.md").write_text(
        "# 内核副本来源\n\n"
        "本目录是内核仓库的**单向同步副本**，不要在这里改代码——改动会被下一次同步覆盖。\n"
        "开发在源仓库进行，部署前跑一次：`uv run python scripts/sync_kernel.py`。\n\n"
        f"- 源仓库：`{src}`\n"
        f"- 远端：{info['remote'] or '（未配置）'}\n"
        f"- commit：`{info['commit'] or '（不是 git 仓库）'}`\n"
        f"- 源仓库有未提交改动：{dirty}\n"
        f"- 同步时间：{datetime.now().isoformat(timespec='seconds')}\n"
        f"- 同步文件数：{count}\n\n"
        f"- 排除（不会进副本）：{'、'.join(sorted(EXCLUDE_DIRS | EXCLUDE_FILES))}；副本内同名项也不会被同步删除\n",
        encoding="utf-8")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows 控制台默认 GBK
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="同步内核副本到 kernel/")
    parser.add_argument("--from", dest="src", default=os.environ.get("UI_WEB_KERNEL_SRC") or str(DEFAULT_SRC),
                        help=f"内核源仓库路径（默认 {DEFAULT_SRC}）")
    parser.add_argument("--dest", default=str(DEST), help=f"副本落点（默认 {DEST}）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将发生的动作")
    args = parser.parse_args()

    src, dest = Path(args.src).expanduser().resolve(), Path(args.dest).expanduser().resolve()
    if not (src / "pyproject.toml").is_file():
        print(f"✗ 源仓库不像内核仓库（找不到 pyproject.toml）：{src}\n"
              f"  用 --from 指定，或设 UI_WEB_KERNEL_SRC。", file=sys.stderr)
        return 2
    if dest == src or src in dest.parents:
        print(f"✗ 副本不能落在源仓库内部：{dest}", file=sys.stderr)
        return 2

    files = source_files(src)
    info = git_info(src)
    if args.dry_run:
        print(f"源 {src} → 副本 {dest}：将写入 {len(files)} 个文件"
              f"（commit {info['commit'][:8] or 'n/a'}，未提交改动：{'有' if info['dirty'] else '无'}）")
        return 0

    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for rel, path in sorted(files.items()):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.stat().st_size != path.stat().st_size or \
                target.stat().st_mtime < path.stat().st_mtime:
            shutil.copy2(path, target)
            copied += 1
    removed = prune(dest, set(files))
    write_provenance(dest, src, info, len(files))

    print(f"✓ 内核副本已同步：{dest}")
    print(f"  源 {src}（commit {info['commit'][:8] or 'n/a'}，"
          f"未提交改动：{'有' if info['dirty'] else '无'}）")
    print(f"  写入/更新 {copied} 个文件，清理 {len(removed)} 个已删除文件，共 {len(files)} 个文件在管")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
