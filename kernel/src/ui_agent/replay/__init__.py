"""回放缓存：把一次断言确认过的运行录成「状态 → 决策」，下次零模型重放、差异交还给模型。"""

from ui_agent.replay.cache import (
    CACHE_POLICY_VERSION,
    CACHE_SOURCE,
    TOOL_SOURCE_PREFIX,
    CachedDecision,
    CacheEntry,
    CacheStore,
    cache_key,
    goal_case_hash,
    normalize_url,
    plan_case_hash,
    state_signature,
)
from ui_agent.replay.match import match_next
from ui_agent.replay.session import CacheOutcome, ReplaySession, open_session, record_from

__all__ = ["CACHE_POLICY_VERSION", "CACHE_SOURCE", "TOOL_SOURCE_PREFIX", "CacheEntry",
           "CacheOutcome", "CacheStore", "CachedDecision", "ReplaySession", "cache_key",
           "goal_case_hash", "match_next", "normalize_url", "open_session", "plan_case_hash",
           "record_from", "state_signature"]
