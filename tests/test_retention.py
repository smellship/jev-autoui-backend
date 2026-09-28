"""保留策略（§13）：只清 artifacts/ 下可再生成的产物，台账与 summary/trace 永不参与。

每个用例用独立 SQLite 库，和接口流程测试互不干扰。
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.db import Base
from app.models import Run, utcnow
from app.services import retention

FILE_BYTES = {"screenshots/01-click.png": 2048, "snapshots/001.json": 4096,
              "report.html": 1024, "summary.json": 2, "trace.jsonl": 3}


@pytest.fixture()
def store(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'retention.db').as_posix()}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)


def make_run(db, root: Path, *, name: str, mode: str = "ci", status: str = "passed",
             age_days: int = 0) -> Path:
    run_dir = root / name
    for rel, size in FILE_BYTES.items():
        path = run_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    hit = utcnow() - timedelta(days=age_days)
    db.add(Run(code=f"r-{name}", case_id=1, mode=mode, status=status, artifact_dir=str(run_dir),
               created_at=hit, finished_at=hit, steps=1))
    db.commit()
    return run_dir


def tweak(monkeypatch, **overrides) -> None:
    base = get_settings()
    monkeypatch.setattr(retention, "get_settings", lambda: base.model_copy(update=overrides))


def test_old_ci_run_loses_only_regenerable_artifacts(store, tmp_path, monkeypatch):
    tweak(monkeypatch, retention_keep_snapshot_runs=0)
    with store() as db:
        old = make_run(db, tmp_path, name="old", mode="ci", age_days=40)
        fresh = make_run(db, tmp_path, name="fresh", mode="ci", age_days=1)
        report = retention.sweep(db)
    assert not (old / "screenshots").exists()
    assert not (old / "snapshots").exists()
    assert not (old / "report.html").exists()
    assert (old / "summary.json").exists() and (old / "trace.jsonl").exists()
    assert all((fresh / rel).exists() for rel in FILE_BYTES)
    assert (report["screenshots"], report["snapshots"], report["reports"]) == (1, 1, 1), report


def test_debug_screenshots_keep_last_n_runs(store, tmp_path, monkeypatch):
    tweak(monkeypatch, retention_keep_debug_runs=1, retention_keep_snapshot_runs=0)
    with store() as db:
        older = make_run(db, tmp_path, name="debug-old", mode="debug", age_days=40)
        newest = make_run(db, tmp_path, name="debug-new", mode="debug", age_days=40)
        retention.sweep(db)
    assert (newest / "screenshots").exists(), "最近 1 次 debug 的截图要留着"
    assert not (older / "screenshots").exists()


def test_snapshots_keep_last_n_runs(store, tmp_path, monkeypatch):
    tweak(monkeypatch, retention_keep_snapshot_runs=1)
    with store() as db:
        older = make_run(db, tmp_path, name="snap-old", mode="ci", age_days=40)
        newest = make_run(db, tmp_path, name="snap-new", mode="ci", age_days=40)
        retention.sweep(db)
    assert (newest / "snapshots").exists(), "最近 1 次运行的快照要留着"
    assert not (older / "snapshots").exists()


def test_active_run_is_never_touched(store, tmp_path, monkeypatch):
    tweak(monkeypatch, retention_keep_snapshot_runs=0)
    with store() as db:
        running = make_run(db, tmp_path, name="running", mode="ci", status="running", age_days=40)
        report = retention.sweep(db)
    assert all((running / rel).exists() for rel in FILE_BYTES)
    assert report["freed_bytes"] == 0, report


def test_missing_artifact_dir_is_skipped(store, tmp_path, monkeypatch):
    tweak(monkeypatch, retention_keep_snapshot_runs=0)
    with store() as db:
        gone = make_run(db, tmp_path, name="gone", mode="ci", age_days=40)
        shutil.rmtree(gone)
        report = retention.sweep(db)
    assert report == {"screenshots": 0, "snapshots": 0, "reports": 0, "capped": 0, "freed_bytes": 0}


def test_total_cap_trims_oldest_passed_ci(store, tmp_path, monkeypatch):
    tweak(monkeypatch, retention_total_gb=0.000001)  # 上限约 1 KB，逼出总量裁剪
    with store() as db:
        old = make_run(db, tmp_path, name="cap-old", mode="ci", status="passed")
        new = make_run(db, tmp_path, name="cap-new", mode="debug", status="passed")
        report = retention.sweep(db)
    assert report["capped"] == 1, report
    assert not (old / "screenshots").exists() and not (old / "report.html").exists()
    assert (old / "summary.json").exists(), "summary 不参与裁剪"
    assert (new / "screenshots").exists(), "只有成功 ci 运行参与总量裁剪"
    assert report["freed_bytes"] >= FILE_BYTES["screenshots/01-click.png"]
