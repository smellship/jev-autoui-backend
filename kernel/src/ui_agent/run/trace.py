"""trace 时间线的分层与写入：运行级 / 决策级 / 断言级 / 模型调用级都在同一份文件里。

每行都有 seq（本文件内单调，文件里的行序即事件序）与 at（毫秒时间）；行类型 kind：
  run_start / run_end    运行边界（身份、配置快照、最终结果）
  step_start / step_end  计划模式的步边界（只有根 trace 有）
  decision               一次决策模型往返 + 落地的动作（默认行，保留旧的 time 字段）
  assert                 DONE 后的一次验收（轮询次数、耗时、逐条结果）
  model                  没产出决策的模型调用（如决策响应不合规）——模型调用级的事实

聚合进计划根 trace 的只有 AGGREGATED_KINDS（逐决策与验收事实）；运行级行留在各自
文件，聚合文件不做二次搬运。

回放缓存（plan.md 第 4 项）的事件走 kind="cache" 行（运行级，每次运行一行，由收尾的
一层写）口径在此定稿：
  mode        本次运行与缓存的关系：self-finalized=以自身断言收尾（回放/录制结论可信）
              / agent-concluded=以模型自称 DONE 收尾（结论不可信，写不落盘）/ missed=没走回放
  write       写入结果：saved=有更新已落盘 / kept=命中且原样保留 / no-change=重录内容与旧的一致
              / unconfirmed=暂存未获断言确认、未落盘 / evicted=失败牵连的旧条目已淘汰
              / not-written=本次无可写内容
  miss_reason 未命中原因：no-entry=没有录像 / no-variant=没有匹配当前页面起点的变体
              （录像的起点与当前页面不符）/ wrong-context=页面上下文不符（地址/标题变了）
              / invalid-entry=目标元素在当前快照里定位不到 / truncated=录像走到头还没收尾
              / retry=回放出的记录没能成立（动作失败或收尾断言未过），交还重试
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ui_agent.replay.session import CacheOutcome

ROW_KINDS = ("run_start", "run_end", "step_start", "step_end", "decision", "assert", "model",
             "cache")
AGGREGATED_KINDS = ("decision", "assert", "model")   # 进计划根 trace 的行
CACHE_MODES = ("self-finalized", "agent-concluded", "missed")
CACHE_WRITES = ("saved", "kept", "unconfirmed", "evicted", "no-change", "not-written")
CACHE_MISSES = ("retry", "no-entry", "invalid-entry", "truncated", "wrong-context", "no-variant")


def now_at() -> str:
    """事件时间（毫秒）：决策行保留秒级 time 供老消费方，新行一律用 at。"""
    return datetime.now().isoformat(timespec="milliseconds")


def cache_row(outcome: CacheOutcome) -> dict:
    """回放缓存运行级行：mode/write/miss_reason 词表见模块 docstring。"""
    return {
        "kind": "cache",
        "at": now_at(),
        "mode": outcome.mode,
        "write": outcome.word,
        "hits": outcome.hits,
        "misses": outcome.misses,
        "miss_reasons": dict(outcome.miss_reasons),
        "kept_steps": outcome.kept_steps,
        "saved_steps": outcome.saved_steps,
        "no_change_steps": outcome.no_change_steps,
        "evicted_steps": outcome.evicted_steps,
        "dropped_steps": outcome.dropped_steps,
    }


def append_row(path: Path, row: dict) -> dict | None:
    """给已收尾的 trace 补一行（收尾才知道的运行级事实）：seq 接着原文件编号。
    文件不存在（运行在开 trace 前就失败）时不补。"""
    if not path.exists():
        return None
    seq = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                seq = json.loads(line).get("seq", seq)
    with path.open("a", encoding="utf-8") as handle:
        return TraceWriter(handle, start_seq=seq).write(row)


class TraceWriter:
    """给每行盖 seq：同一份 trace 里编号连续，聚合文件与单步文件各自独立编号。"""

    def __init__(self, handle, start_seq: int = 0):
        self.handle = handle
        self.seq = start_seq

    def write(self, row: dict) -> dict:
        kind = row.get("kind", "decision")
        if kind not in ROW_KINDS:
            raise ValueError(f"未知的 trace 行类型：{kind}；可用：{'、'.join(ROW_KINDS)}")
        self.seq += 1
        out = {**row, "seq": self.seq}
        self.handle.write(json.dumps(out, ensure_ascii=False) + "\n")
        self.handle.flush()
        return out
