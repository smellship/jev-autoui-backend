"""回放缓存存储层的离线单测：键口径、状态签名、单键文件、原子写、坏文件按未命中。"""

from __future__ import annotations

from ui_agent.replay import cache as cache_mod
from ui_agent.replay.cache import (
    CACHE_POLICY_VERSION,
    CachedDecision,
    CacheEntry,
    CacheStore,
    cache_key,
    goal_case_hash,
    normalize_url,
    plan_case_hash,
    state_signature,
)
from ui_agent.schema.check import CheckSpec
from ui_agent.schema.plan import PlanStep
from ui_agent.schema.plan import TestPlan as Plan
from ui_agent.schema.state import Element, PageInfo, State


def plan(**over) -> Plan:
    base = {
        "name": "登录查询",
        "url": "http://app.test/",
        "steps": [PlanStep(goal="打开登录页", url="http://app.test/login"),
                  PlanStep(goal="查询发票", checks=[CheckSpec(kind="text_contains", value="发票")])],
        "vars": {"账号": "alice", "密码": "${{secret.密码}}"},
    }
    return Plan(**{**base, **over})


def state(url: str = "http://app.test/login", title: str = "登录", labels=("账号", "登录"),
          **page_over) -> State:
    page = {"url": url, "title": title, "text": "x" * 40, "viewport": {"w": 1920, "h": 1080},
            "scroll": {"y": 0, "max": 0}, **page_over}
    return State(
        page=PageInfo(**page),
        elements=[Element(index=i, role="button", label=label)
                  for i, label in enumerate(labels, start=1)],
    )


class TestCacheKey:
    def test_var_values_do_not_change_the_key(self):
        # 值可能含凭据，且回放按 --var 现取：换值不该让缓存作废
        one = plan(vars={"账号": "alice", "密码": "${{secret.密码}}"})
        other = plan(vars={"账号": "bob", "密码": "${{secret.密码}}"})
        assert plan_case_hash(one) == plan_case_hash(other)
        assert cache_key(plan_case_hash(one), "http://app.test/") == \
            cache_key(plan_case_hash(other), "http://app.test/")

    def test_var_value_vs_secret_reference_do_not_change_the_key(self):
        one = plan(vars={"账号": "alice"})
        other = plan(vars={"账号": "${{secret.账号}}"})
        assert plan_case_hash(one) == plan_case_hash(other)

    def test_structure_changes_the_key(self):
        one = plan()
        other = plan(steps=[PlanStep(goal="打开登录页"), PlanStep(goal="查询发票")])
        assert plan_case_hash(one) != plan_case_hash(other)

    def test_assertion_changes_the_key(self):
        one = plan()
        other = plan(steps=[PlanStep(goal="打开登录页", url="http://app.test/login")])
        assert plan_case_hash(one) != plan_case_hash(other)

    def test_plan_url_is_env_identity_not_case_identity(self):
        # 入口地址属于「环境」那一半：平台把它烘进 plan.url，但键里只算一次
        assert plan_case_hash(plan(url="http://a.test/")) == plan_case_hash(plan(url="http://b.test/"))

    def test_envs_get_different_keys(self):
        case = plan_case_hash(plan())
        assert cache_key(case, "http://a.test/") != cache_key(case, "http://b.test/")

    def test_url_spelling_normalized_in_key(self):
        case = plan_case_hash(plan())
        assert cache_key(case, "HTTP://App.test/x/") == cache_key(case, "http://app.test/x")

    def test_policy_version_is_part_of_the_key(self, monkeypatch):
        case = plan_case_hash(plan())
        before = cache_key(case, "http://app.test/")
        monkeypatch.setattr(cache_mod, "CACHE_POLICY_VERSION", "v99")
        assert cache_key(case, "http://app.test/") != before

    def test_goal_hash_tracks_goal_and_checks(self):
        checks = [CheckSpec(kind="text_contains", value="发票")]
        assert goal_case_hash("查询", checks) != goal_case_hash("查询发票", checks)
        assert goal_case_hash("查询", checks) != goal_case_hash("查询", [])
        assert goal_case_hash("查询", checks) == goal_case_hash("查询", list(checks))


class TestNormalizeUrl:
    def test_strips_case_slash_and_fragment(self):
        assert normalize_url("https://APP.test/Path/#frag") == "https://app.test/Path"
        assert normalize_url("http://app.test") == "http://app.test"

    def test_query_kept_by_default_dropped_on_demand(self):
        assert normalize_url("http://x/a?session=1") == "http://x/a?session=1"
        assert normalize_url("http://x/a?session=1", keep_query=False) == "http://x/a"


class TestStateSignature:
    def test_ignores_scroll_viewport_text_length_and_query(self):
        one = state()
        other = state(text="y" * 900, viewport={"w": 800, "h": 600}, scroll={"y": 400, "max": 900},
                      url="http://app.test/login?session=abc")
        assert state_signature(one) == state_signature(other)

    def test_tracks_title_url_path_and_elements(self):
        one = state()
        assert state_signature(one) != state_signature(state(title="首页"))
        assert state_signature(one) != state_signature(state(url="http://app.test/home"))
        assert state_signature(one) != state_signature(state(labels=("账号", "登录", "注册")))

    def test_is_stable_short_hex(self):
        sig = state_signature(state())
        assert len(sig) == 16 and sig == state_signature(state())


class TestCacheStore:
    @staticmethod
    def entry(key: str = "a" * 64) -> CacheEntry:
        entry = CacheEntry.build(key=key, case_hash="h" * 64, base_url="http://app.test/",
                                 plan_name="登录查询", run_id="run-1")
        entry.records.append(CachedDecision(step=1, operation="CLICK", target_label="登录",
                                            target_role="button", url="http://app.test/login",
                                            title="登录", signature="s1", source="jev",
                                            at="2026-10-14T10:00:00"))
        entry.records.append(CachedDecision(step=2, operation="DONE", source="jev",
                                            at="2026-10-14T10:00:05"))
        return entry

    def test_roundtrip(self, tmp_path):
        store = CacheStore(tmp_path / "cache")
        saved = store.save(self.entry())
        assert saved == store.root / f"{'a' * 64}.json"
        loaded = store.load("a" * 64)
        assert loaded is not None
        assert loaded.plan_name == "登录查询" and loaded.base_url == "http://app.test"
        assert loaded.records[0].target_label == "登录" and loaded.records[0].target_role == "button"
        assert loaded.records[1].operation == "DONE"
        assert list(store.root.glob("*.tmp")) == []   # 原子写不留临时文件

    def test_save_overwrites(self, tmp_path):
        store = CacheStore(tmp_path / "cache")
        store.save(self.entry())
        again = self.entry()
        again.records = again.records[:1]
        store.save(again)
        assert len(store.load("a" * 64).records) == 1   # type: ignore[union-attr]

    def test_missing_corrupt_or_stale_policy_is_a_miss(self, tmp_path):
        store = CacheStore(tmp_path / "cache")
        assert store.load("b" * 64) is None
        (store.root).mkdir(parents=True)
        store.path_for("c" * 64).write_text("不是 JSON", encoding="utf-8")
        store.path_for("d" * 64).write_text("[1, 2]", encoding="utf-8")
        stale = self.entry("e" * 64)
        stale.policy = "v0"
        store.save(stale)
        assert store.load("c" * 64) is None
        assert store.load("d" * 64) is None
        assert store.load("e" * 64) is None

    def test_delete(self, tmp_path):
        store = CacheStore(tmp_path / "cache")
        store.save(self.entry())
        assert store.delete("a" * 64) is True
        assert store.load("a" * 64) is None
        assert store.delete("a" * 64) is False

    def test_for_step_filters_records(self):
        entry = self.entry()
        assert [row.operation for row in entry.for_step(1)] == ["CLICK"]
        assert entry.for_step(3) == []

    def test_build_normalizes_base_url_and_stamps_time(self):
        entry = CacheEntry.build(key="f" * 64, case_hash="h", base_url="HTTP://App.test/")
        assert entry.base_url == "http://app.test"
        assert entry.policy == CACHE_POLICY_VERSION
        assert entry.created_at and entry.updated_at
