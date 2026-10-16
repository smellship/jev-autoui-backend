"""时间线写入的离线单测：行类型白名单、seq 单调、毫秒时间。"""

from __future__ import annotations

import io
import json

import pytest

from ui_agent.replay.session import CacheOutcome
from ui_agent.run.trace import AGGREGATED_KINDS, ROW_KINDS, TraceWriter, append_row, cache_row, now_at


def test_writer_stamps_seq_and_flushes_each_row():
    buf = io.StringIO()
    writer = TraceWriter(buf)
    first = writer.write({"kind": "run_start", "at": now_at()})
    second = writer.write({"kind": "decision", "step": 0})
    rows = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert [row["seq"] for row in rows] == [1, 2]
    assert first["seq"] == 1 and second["seq"] == 2


def test_writer_defaults_to_decision_and_rejects_unknown_kinds():
    writer = TraceWriter(io.StringIO())
    assert writer.write({"operation": "CLICK"})["seq"] == 1  # 老行没有 kind，视作 decision
    with pytest.raises(ValueError, match="未知的 trace 行类型"):
        writer.write({"kind": "nope"})


def test_lifecycle_rows_are_not_aggregated():
    assert "decision" in AGGREGATED_KINDS and "assert" in AGGREGATED_KINDS
    assert "run_start" not in AGGREGATED_KINDS and "step_end" not in AGGREGATED_KINDS
    assert {"run_start", "run_end", "step_start", "step_end", "model"} <= set(ROW_KINDS)


def test_now_at_has_milliseconds():
    assert len(now_at().split(".")[-1]) == 3


def test_cache_row_maps_outcome_vocabulary():
    row = cache_row(CacheOutcome(word="saved", hits=2, misses=1, miss_reasons={"no-entry": 1},
                                 saved_steps=1, dropped_steps=0))
    assert row["kind"] == "cache" and row["at"]
    assert row["mode"] == "self-finalized" and row["write"] == "saved"
    assert row["hits"] == 2 and row["misses"] == 1 and row["miss_reasons"] == {"no-entry": 1}
    assert {"kept_steps", "saved_steps", "no_change_steps", "evicted_steps",
            "dropped_steps"} <= set(row)


def test_cache_row_modes_from_write_word():
    assert cache_row(CacheOutcome(word="unconfirmed"))["mode"] == "agent-concluded"
    assert cache_row(CacheOutcome(word="not-written"))["mode"] == "missed"
    assert cache_row(CacheOutcome(word="evicted"))["mode"] == "self-finalized"
    assert cache_row(CacheOutcome(word="kept"))["mode"] == "self-finalized"


def test_append_row_continues_seq_and_gates_missing_file(tmp_path):
    path = tmp_path / "trace.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        writer = TraceWriter(handle)
        writer.write({"kind": "run_start"})
        writer.write({"kind": "run_end"})
    row = append_row(path, {"kind": "cache", "write": "kept"})
    assert row is not None and row["seq"] == 3
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [r["seq"] for r in rows] == [1, 2, 3] and rows[-1]["kind"] == "cache"
    assert append_row(tmp_path / "missing.jsonl", {"kind": "cache"}) is None
