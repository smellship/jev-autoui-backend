"""回放缓存（plan.md 第 4 项）的存储层：缓存键、单键 JSON 文件、原子写。

口径（2026-10-14 拍板）：
  - 键 = sha256(策略版本 + 用例内容哈希 + 环境地址)。用例里 vars 的值在哈希前换成占位——
    值可能含凭据，且回放时按 --var 现取，值变化不该让缓存作废；页面级 URL 差异
    （重定向、尾斜杠、会话串）交给回放匹配阶段归一化，不进键。
  - 一个键一个文件，放在 <artifacts>/cache/<键>.json；写入先落同目录 .tmp 再 os.replace，
    读侧读不动一律按未命中——缓存坏了不该把运行拖崩，下次成功运行会自愈覆盖。
  - 文件里逐条记「状态 → 决策」，只存语义身份（角色/标签/选项），不存旧索引：
    回放时元素按当次快照重定位，索引跨运行没有意义。
  - 变体化（v3）：同一用例的起点可能不止一个（登录态有/无、入口页不同），录好的路径
    按「起点上下文」（组内第一条记录的 url+title）分成变体，每条记录带 variant 戳。
    回放时按当前页面自动选变体（同一起点可有多条路径，后录的优先），选不中整步交还
    模型；从零重跑出的新路径保序追加为新变体——不同起点的路径绝不能拼成一条。

运行级的写入结果词表（saved/kept/…）与未命中原因词表（no-entry/wrong-context/…）
的释义在 run/trace.py 的 kind="cache" 口径处。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, ValidationError

from ui_agent.schema.check import CheckSpec
from ui_agent.schema.plan import TestPlan
from ui_agent.schema.state import State

CACHE_POLICY_VERSION = "v3"  # 记录格式或匹配语义变化时递增：旧键自然失效（v3=录像变体化）

# 回放命中的决策在 Decision.model 里挂这个标记：动作照常执行，但不再重录（消费已计数）
CACHE_SOURCE = "cache"

# 工具环产的决策用这个前缀：model=f"tool:{operation}"，既不和 cache 撞，也能在 trace 里区分
TOOL_SOURCE_PREFIX = "tool:"


def normalize_url(url: str, keep_query: bool = True) -> str:
    """键与匹配共用的轻归一化：大小写、尾斜杠、锚点不参与身份。

    keep_query=False 用于页面级比较：query 里常挂会话串之类的易变参数。
    """
    parts = urlsplit((url or "").strip())
    query = parts.query if keep_query else ""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, ""))


def _sha256_json(data) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan_case_hash(plan: TestPlan) -> str:
    """用例内容哈希：只认计划结构；vars 值模板化占位、入口地址除外（那是环境身份的一半）。"""
    data = plan.model_dump()
    data.pop("url", None)
    data["vars"] = {key: "" for key in sorted(data.get("vars") or {})}
    return _sha256_json(data)


def goal_case_hash(goal: str, checks: list[CheckSpec]) -> str:
    """目标模式（没有用例文件）的等价物：目标文本 + 断言。"""
    return _sha256_json({"goal": goal, "checks": [item.model_dump() for item in checks]})


def cache_key(case_hash: str, base_url: str) -> str:
    """缓存键：策略版本 + 用例哈希 + 环境地址。"""
    joined = "\n".join([CACHE_POLICY_VERSION, case_hash, normalize_url(base_url)])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def state_signature(state: State) -> str:
    """状态签名：页面身份 + 结构（角色/标签清单），不含滚动、视口、文本长度这类易变项。

    与 snapshot.js 的 fingerprint 同源但更稳：回放问的是「还是这个页面吗」。
    严判模式拿它比对记录时的页面是否已变。
    """
    data = [normalize_url(state.page.url, keep_query=False), state.page.title,
            [[el.role, el.label] for el in state.elements]]
    return _sha256_json(data)[:16]


class CachedDecision(BaseModel):
    """一条可回放的决策：只留语义身份，回放时按当次快照重新定位元素。

    text 只存文本模型产出的字段值；来自 --var 的值不落盘（回放时按标签现取当前值）。
    工具环的对话文本、参数 JSON、页面 diff、护栏提示只进 trace 行，不进缓存——
    缓存只记动作的语义身份（角色/标签/选项/按键/值），不记模型轮次。
    """

    step: int = 1                  # 计划步序号（目标模式恒为 1）；回放按步取用
    variant: int = 1               # 变体号：同一 (step, variant) 的行是一条同起点的路径
    operation: str
    target_label: str = ""         # 目标元素的标签
    target_role: str = ""          # 目标元素的角色（标签不够唯一时的第二判据）
    target_option_label: str = ""  # SELECT：选中项的可见标签
    target_option_value: str = ""  # SELECT：选中项的 value（标签动态时的兜底）
    press_key: str = ""
    text: str = ""
    to_label: str = ""             # DRAG：目标元素的标签（拖拽终点）
    to_role: str = ""              # DRAG：目标元素的角色
    checked: bool | None = None    # CHECK：期望的勾选态（True=勾，False=取消，None=未指定）
    times: int = 1                 # PRESS_KEY/SCROLL：重复次数
    url: str = ""                  # 决策时的页面地址（归一化，留 query 供排查）
    title: str = ""
    signature: str = ""            # 记录时的状态签名（严判与上下文比对用）
    source: str = ""               # 决策来自谁：jev / llm / supervisor / tool:<op>
    at: str = ""                   # 记录时刻（ISO 秒）


class CacheEntry(BaseModel):
    """一个「用例 × 环境」的全部录像：键身份 + 记录表。"""

    policy: str = CACHE_POLICY_VERSION
    key: str = ""
    case_hash: str = ""
    base_url: str = ""
    plan_name: str = ""
    run_id: str = ""               # 最近一次写入的运行目录名（追溯谁写的）
    created_at: str = ""
    updated_at: str = ""
    records: list[CachedDecision] = Field(default_factory=list)

    @classmethod
    def build(cls, *, key: str, case_hash: str, base_url: str, plan_name: str = "",
              run_id: str = "") -> CacheEntry:
        now = datetime.now().isoformat(timespec="seconds")
        return cls(key=key, case_hash=case_hash, base_url=normalize_url(base_url),
                   plan_name=plan_name, run_id=run_id, created_at=now, updated_at=now)

    def for_step(self, step: int) -> list[CachedDecision]:
        return [row for row in self.records if row.step == step]


class CacheStore:
    """单键单文件，无锁 last-write-wins（平台并发同键时后写的赢，与 e2e 同款）。"""

    def __init__(self, root: Path):
        self.root = Path(root)

    def path_for(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def load(self, key: str) -> CacheEntry | None:
        try:
            data = json.loads(self.path_for(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            entry = CacheEntry.model_validate(data)
        except ValidationError:
            return None
        if entry.policy != CACHE_POLICY_VERSION:
            return None
        return entry

    def save(self, entry: CacheEntry) -> Path:
        path = self.path_for(entry.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(entry.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def delete(self, key: str) -> bool:
        """淘汰（防投毒）：删得掉返回 True；本来就没有返回 False。"""
        try:
            self.path_for(key).unlink()
            return True
        except FileNotFoundError:
            return False

    def scan_other_policies(self, case_hash: str, base_url: str) -> list[str]:
        """找同一用例×环境下被策略版本升级作废的旧录像，返回它们的策略版本清单。

        键里含策略版本，升级后旧文件仍躺在缓存目录、但任何运行的 load 都不会再看到它；
        这个口让 open_session 能把"为什么没有录像"讲出来（否则零模型运行死在第 1 步
        no-entry 只能靠人翻目录猜）。
        """
        versions: list[str] = []
        if not self.root.is_dir():
            return versions
        wanted = normalize_url(base_url)
        for path in self.root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or data.get("policy") == CACHE_POLICY_VERSION:
                continue
            if data.get("case_hash") != case_hash:
                continue
            if normalize_url(str(data.get("base_url") or "")) != wanted:
                continue
            versions.append(str(data.get("policy") or "?"))
        return sorted(set(versions))
