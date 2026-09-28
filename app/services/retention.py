"""产物保留策略（§13）：只清理 artifacts/ 下可重新生成的产物。

- summary / trace / actions 不参与清理；用例版本与运行台账永不自动删；
- 顺序：先截图 → 再快照 → 再 report；
- 总量超上限时，从"最旧的成功 ci 运行"起删可再生成产物。
"""

from __future__ import annotations

import logging
import shutil
import threading
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db import SessionLocal, session_scope
from app.models import Run, utcnow

log = logging.getLogger("ui_web.retention")

ACTIVE = ("queued", "running")


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def _remove(path: Path) -> int:
    if not path.exists():
        return 0
    freed = _size(path)
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError:
        return 0
    return freed


def _run_dir(run: Run) -> Path:
    return Path(run.artifact_dir) if run.artifact_dir else Path("")


def sweep(db: Session | None = None) -> dict:
    settings = get_settings()
    own = db is None
    db = db or SessionLocal()
    report = {"screenshots": 0, "snapshots": 0, "reports": 0, "capped": 0, "freed_bytes": 0}
    try:
        runs = list(db.scalars(select(Run).order_by(Run.id.desc())).all())
        finished = timedelta(days=settings.retention_days_screenshots)
        snap_days = timedelta(days=settings.retention_days_snapshots)
        report_days = timedelta(days=settings.retention_days_report)
        now = utcnow()
        debug_seen = 0
        for index, run in enumerate(runs):
            if run.status in ACTIVE:
                continue
            run_dir = _run_dir(run)
            if not run_dir or not run_dir.exists():
                continue
            if run.mode == "debug":
                debug_seen += 1
            hit = run.finished_at or run.created_at or now

            shots = run_dir / "screenshots"
            keep_debug = run.mode == "debug" and debug_seen <= settings.retention_keep_debug_runs
            keep_shots = (now - hit) < finished or keep_debug
            if not keep_shots and shots.exists():
                report["freed_bytes"] += _remove(shots)
                report["screenshots"] += 1

            snaps = run_dir / "snapshots"
            keep_snaps = (now - hit) < snap_days or index < settings.retention_keep_snapshot_runs
            if not keep_snaps and snaps.exists():
                report["freed_bytes"] += _remove(snaps)
                report["snapshots"] += 1

            page = run_dir / "report.html"
            if (now - hit) >= report_days and page.exists():
                report["freed_bytes"] += _remove(page)
                report["reports"] += 1

        # 总量上限：从最旧的成功 ci 运行起删可再生成产物
        cap = int(settings.retention_total_gb * 1024 ** 3)
        total = sum(_size(_run_dir(run)) for run in runs if run.artifact_dir and _run_dir(run).exists())
        for run in reversed(runs):
            if total <= cap:
                break
            if run.status in ACTIVE or run.mode != "ci" or run.status != "passed":
                continue
            run_dir = _run_dir(run)
            freed = 0
            for name in ("screenshots", "snapshots", "report.html"):
                freed += _remove(run_dir / name)
            if freed:
                total -= freed
                report["capped"] += 1
                report["freed_bytes"] += freed
        return report
    finally:
        if own:
            db.close()


def _loop_forever(interval_h: int) -> None:  # pragma: no cover - 后台线程
    while True:
        threading.Event().wait(interval_h * 3600)
        try:
            with session_scope() as db:
                result = sweep(db)
            log.info("保留策略清理完成：%s", result)
        except Exception as exc:  # 后台任务不能拖垮服务
            log.warning("保留策略清理失败：%s", exc)


def start_background() -> None:  # pragma: no cover - 后台线程
    settings = get_settings()
    if not settings.retention_enabled:
        return
    thread = threading.Thread(target=_loop_forever, args=(settings.retention_interval_h,),
                              name="retention", daemon=True)
    thread.start()


__all__ = ["sweep", "start_background"]
