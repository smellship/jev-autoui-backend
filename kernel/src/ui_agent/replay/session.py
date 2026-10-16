"""回放缓存会话：一次运行与缓存的关系——录制暂存、按步结算（成功续写/失败淘汰）、收尾落盘。

闸门（2026-10-14 拍板）：
  - 只有「自己的断言确认」（self-finalized）才落盘：模型自称成功但没断言
    （agent-concluded）的暂存一律丢弃；
  - 失败牵连淘汰（防投毒）：某步回放过的变体最终却失败，就整组淘汰——只留前缀或
    只留尾巴都会让下次回放从半路开始，等于投毒；
  - 命中且无需改写 = kept；重录内容与旧录像逐字一致就不重写文件。

变体化（v3，格式见 replay/cache.py 的模块 docstring）：
  - 同一 (step, variant) 的行是一条同起点的路径（起点 = 组内首条记录的 url+title）；
  - 结算按变体走：回放成立的前缀 + 模型接管后新录的动作合并回该变体；没走过回放、
    整段由模型录成的路径追加为新变体——不同起点的路径绝不能拼成一条；
  - 回放出的记录没成立（动作执行失败、或回放收尾的断言没过）时 runner 调
    replay_failed()：本步回放就此终止（handover final），余下决策全交模型——
    中途续放会把转写搅乱（v2 的"截断前缀后交模型续录"已废弃）。

回放侧的取录像与语义重定位见 replay/match.py；本模块只立录制、结算与闸门。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ui_agent.config import Settings
from ui_agent.replay.cache import CachedDecision, CacheEntry, CacheStore, cache_key, normalize_url, state_signature
from ui_agent.schema.decision import Decision
from ui_agent.schema.state import Element, State


def record_from(decision: Decision, state: State, element: Element | None = None,
                text: str = "") -> CachedDecision:
    """把一次已执行的决策语义化：不存索引；text 只留文本模型产出的值（--var 的值不落盘）。"""
    option = None
    if decision.operation == "SELECT" and element is not None and decision.target:
        option = next((item for item in element.options if item.key == decision.target), None)
    to_element = None
    if decision.operation == "DRAG" and decision.target_option is not None:
        to_element = next((el for el in state.elements if el.index == decision.target_option), None)
    checked = None
    if decision.operation == "CHECK":
        checked = decision.press_key == "true"
    repeated = decision.operation in ("PRESS_KEY", "SCROLL_UP", "SCROLL_DOWN")
    times = decision.raw_answers.get("times", 1) if repeated else 1
    return CachedDecision(
        operation=decision.operation,
        target_label="" if element is None else element.label,
        target_role="" if element is None else element.role,
        target_option_label="" if option is None else option.label,
        target_option_value="" if option is None else option.value,
        press_key=decision.press_key or "",
        text=text,
        to_label="" if to_element is None else to_element.label,
        to_role="" if to_element is None else to_element.role,
        checked=checked,
        times=times,
        url=normalize_url(state.page.url),
        title=state.page.title,
        signature=state_signature(state),
        source=decision.model,
        at=datetime.now().isoformat(timespec="seconds"),
    )


@dataclass
class CacheOutcome:
    """一次运行的写入结果；词表释义见 run/trace.py 的 kind="cache" 口径。"""

    word: str = "not-written"
    kept_steps: int = 0
    no_change_steps: int = 0
    saved_steps: int = 0
    evicted_steps: int = 0
    dropped_steps: int = 0
    hits: int = 0
    misses: int = 0
    miss_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def mode(self) -> str:
        """运行与缓存的关系：落盘结论可信 / 模型自称收尾 / 没走通回放。"""
        if self.word == "unconfirmed":
            return "agent-concluded"
        if self.word == "not-written":
            return "missed"
        return "self-finalized"


class ReplaySession:
    """由运行方（PlanRunner / CLI 目标模式）持有，逐级传给每个步的 Runner。"""

    def __init__(self, store: CacheStore, entry: CacheEntry, strict: bool = False,
                 run_id: str = "", stale: list[str] | None = None):
        self.store = store
        self.entry = entry
        self.strict = strict
        self.run_id = run_id
        self.stale = list(stale or [])   # 同一用例×环境下被策略升级作废的旧录像版本
        self.current_step = 1
        self._consumed: dict[int, int] = {}   # 步 → 已放过的录像条数（激活变体内）
        self._staged: dict[int, list[CachedDecision]] = {}
        self._active: dict[int, int] = {}     # 步 → 激活的变体号（回放选了哪组）
        self._handed: set[int] = set()        # 已交还模型的步：本步不再从录像取决策
        self._failed: set[int] = set()        # 步 → 回放过但没成立的记录（结算时淘汰该变体）
        self.last_miss = ""                   # 最近一次未命中原因（handover 后讲它）
        self._kept = self._saved = self._evicted = self._dropped = self._nochange = 0
        self.hits = 0
        self.misses = 0
        self.miss_reasons: dict[str, int] = {}

    # --- 运行开始时的现状播报（打印一次） ---

    @property
    def records(self) -> int:
        return len(self.entry.records)

    def boot_lines(self, decider: str = "jev") -> list[str]:
        """运行开始时把缓存现状讲清楚：有没有录像、旧策略是否作废了录像。

        零模型模式下"没有可用录像"是致命事实（第一步就会停），必须比 jev 模式说得
        更重——这是两次实跑踩过的坑：运行起了、缓存开着、录像却是空的/被换键作废的。
        """
        zero_model = decider == "off"
        lines: list[str] = []
        if self.records:
            lines.append(f"回放缓存：可用录像 {self.records} 条 / {self.variant_count()} 组变体"
                         f"（键 {self.entry.key[:12]}，更新于 {self.entry.updated_at}）")
        elif zero_model:
            lines.append("⚠ 回放缓存：该用例×环境没有录像，且决策模型已关闭——"
                         "本次运行会在第一步判缓存未命中停止（先以录制模式跑一次）")
        else:
            lines.append("回放缓存：该用例×环境没有录像，本次运行边跑边录制")
        if self.stale:
            versions = "、".join(self.stale)
            tail = "；决策模型已关闭，本次运行会停在缓存未命中" if zero_model else "；若打算零模型回归，需先重录一次"
            lines.append(f"⚠ 同一用例×环境存在旧策略版本录像（{versions}）已随缓存升级作废{tail}")
        return lines

    # --- 回放侧（match.py 接线） ---

    def begin_step(self, index: int) -> None:
        self.current_step = index

    def records_for(self, step: int | None = None) -> list[CachedDecision]:
        return self.entry.for_step(self.current_step if step is None else step)

    def variant_groups(self, step: int | None = None) -> list[tuple[int, list[CachedDecision]]]:
        """该步的变体组，按录制先后：[(variant, 行列表), …]。"""
        groups: dict[int, list[CachedDecision]] = {}
        for row in self.records_for(step):
            groups.setdefault(row.variant, []).append(row)
        return list(groups.items())

    def variant_count(self) -> int:
        return len({(row.step, row.variant) for row in self.entry.records})

    @property
    def consumed(self) -> int:
        return self._consumed.get(self.current_step, 0)

    def advance(self) -> None:
        self._consumed[self.current_step] = self.consumed + 1
        self.hits += 1

    def active_variant(self) -> int | None:
        return self._active.get(self.current_step)

    def active_rows(self) -> list[CachedDecision] | None:
        """激活变体的行；本步还没选变体时返回 None。"""
        variant = self.active_variant()
        if variant is None:
            return None
        return next((rows for vid, rows in self.variant_groups() if vid == variant), [])

    def activate(self, variant: int) -> None:
        self._active[self.current_step] = variant

    def hand_over(self) -> None:
        """本步回放就此终止：余下决策全交模型，不再进行第二次回放。"""
        self._handed.add(self.current_step)

    def handed_over(self) -> bool:
        return self.current_step in self._handed

    def miss(self, reason: str) -> None:
        self.misses += 1
        self.miss_reasons[reason] = self.miss_reasons.get(reason, 0) + 1
        self.last_miss = reason

    def replay_failed(self, reason: str = "retry") -> None:
        """回放出的这条没能成立（动作失败/收尾断言没过）：记词、整步交还。不成立的
        那条不算已消费——结算时成立前缀只到它前面。"""
        self.miss(reason)
        step = self.current_step
        self._consumed[step] = max(self.consumed - 1, 0)
        self._failed.add(step)
        self.hand_over()

    # --- 录制侧 ---

    def stage(self, record: CachedDecision) -> None:
        record.step = self.current_step
        self._staged.setdefault(self.current_step, []).append(record)

    def settle_step(self, status: str) -> None:
        """步收尾（闸门）：ok=断言确认 → 录像并入变体组；done_unverified=没断言不落盘也
        不株连；其余视为失败 → 淘汰本步回放过的那条变体（防投毒），暂存丢弃。"""
        step = self.current_step
        staged = self._staged.pop(step, [])
        used = self._consumed.pop(step, 0)
        active = self._active.pop(step, None)
        handed = step in self._handed
        self._handed.discard(step)
        broke = step in self._failed      # 回放过但没成立的记录（执行失败/收尾断言没过）
        self._failed.discard(step)
        old = self.entry.for_step(step)
        old_groups = dict(self.variant_groups(step))

        if status == "ok":
            if active is not None:
                # 回放过：成立的前缀 + 模型接管新录的（盖变体戳）合并回该变体组
                rows = old_groups[active]
                merged = [*rows[:used], *self._stamped(staged, active)]
                if merged == rows:
                    if used == len(rows) and not staged:
                        self._kept += 1        # 纯命中：放完录像，原样保留
                    else:
                        self._nochange += 1    # 重录内容与旧的一致：不重写文件
                else:
                    self._replace_variant(step, active, merged)
                    self._saved += 1
                return
            if handed and not broke:
                # 没回放成、整步由模型录成（handover 后接管 / 全新录制）：追加为新变体
                if staged:
                    self._insert_variant(step, staged)
                    self._saved += 1
                return
            # 其余（离线直调/未接线）：保持旧的平铺语义——暂存替换该步录像
            merged = old[:used] + staged
            if merged == old:
                if used == len(old) and not staged:
                    self._kept += 1
                else:
                    self._nochange += 1
            else:
                self._replace(step, merged)
                self._saved += 1
            return
        if status == "done_unverified":
            # 模型自称 DONE 但没断言：结论不可信——暂存丢弃，旧录像原样留着
            if staged:
                self._dropped += 1
            return
        # 失败：淘汰回放过但没成立的录像（防投毒）——有变体只淘汰那条变体，平铺淘汰整步
        if active is not None:
            if used or broke:
                self._evict_variant(step, active)
                self._evicted += 1
        elif broke or (used and old):
            self._replace(step, [])
            self._evicted += 1
        if staged:
            self._dropped += 1

    def finalize(self) -> CacheOutcome:
        """收尾：有实质变化才落盘（原子写），并给出运行级写入结果词。"""
        outcome = CacheOutcome(kept_steps=self._kept, no_change_steps=self._nochange,
                               saved_steps=self._saved, evicted_steps=self._evicted,
                               dropped_steps=self._dropped, hits=self.hits, misses=self.misses,
                               miss_reasons=dict(self.miss_reasons))
        if self._saved or self._evicted:
            self.entry.updated_at = datetime.now().isoformat(timespec="seconds")
            if self.run_id:
                self.entry.run_id = self.run_id
            self.store.save(self.entry)
            outcome.word = "saved" if self._saved else "evicted"
        elif self._dropped:
            outcome.word = "unconfirmed"
        elif self._nochange:
            outcome.word = "no-change"
        elif self._kept:
            outcome.word = "kept"
        return outcome

    @staticmethod
    def _stamped(rows: list[CachedDecision], variant: int) -> list[CachedDecision]:
        for row in rows:
            row.variant = variant
        return rows

    def _replace_variant(self, step: int, variant: int, rows: list[CachedDecision]) -> None:
        """该变体组的全部行原地换成 rows（组在文件里的位置保持）。"""
        out: list[CachedDecision] = []
        placed = False
        for row in self.entry.records:
            if row.step == step and row.variant == variant:
                if not placed:
                    out.extend(rows)
                    placed = True
                continue
            out.append(row)
        self.entry.records = out

    def _insert_variant(self, step: int, rows: list[CachedDecision]) -> None:
        """追加为新变体（变体号取该步现值 + 1），排在该步已有录像之后。"""
        variant = max((vid for vid, _ in self.variant_groups(step)), default=0) + 1
        self._stamped(rows, variant)
        head = [row for row in self.entry.records if row.step < step]
        same = [row for row in self.entry.records if row.step == step]
        tail = [row for row in self.entry.records if row.step > step]
        self.entry.records = [*head, *same, *rows, *tail]

    def _evict_variant(self, step: int, variant: int) -> None:
        self.entry.records = [row for row in self.entry.records
                              if not (row.step == step and row.variant == variant)]

    def _replace(self, step: int, rows: list[CachedDecision]) -> None:
        head = [row for row in self.entry.records if row.step < step]
        tail = [row for row in self.entry.records if row.step > step]
        self.entry.records = [*head, *rows, *tail]


def open_session(settings: Settings, *, case_hash: str, base_url: str, plan_name: str = "",
                 run_id: str = "") -> ReplaySession | None:
    """按配置开一次缓存会话；主开关关、或没有环境地址取不到键，就不开。"""
    if not settings.ui_agent_cache or not base_url:
        return None
    store = CacheStore(settings.cache_dir)
    key = cache_key(case_hash, base_url)
    entry = store.load(key)
    stale: list[str] = []
    if entry is None:
        # 没有现键录像时顺带查一遍被策略升级作废的旧录像：有的话，运行开始的播报
        # 会把"为什么没有录像"说出来（键换了，不是没录过）
        stale = store.scan_other_policies(case_hash, base_url)
        entry = CacheEntry.build(key=key, case_hash=case_hash, base_url=base_url,
                                 plan_name=plan_name, run_id=run_id)
    return ReplaySession(store, entry, strict=settings.ui_agent_cache_strict, run_id=run_id,
                         stale=stale)
