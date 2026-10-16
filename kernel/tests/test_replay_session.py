"""录制与落盘闸门的离线单测：Runner 真跑一遍（假驱动/假引擎）+ ReplaySession 直接结算。"""

from __future__ import annotations

from fakes import FakeDriver, FakeEngine, click, done, read_rows, settings_for, snapshot

from ui_agent.replay.cache import (
    CACHE_POLICY_VERSION,
    CachedDecision,
    CacheEntry,
    CacheStore,
    cache_key,
    goal_case_hash,
    plan_case_hash,
    state_signature,
)
from ui_agent.replay.session import ReplaySession, open_session, record_from
from ui_agent.run.plan_runner import PlanRunner
from ui_agent.run.runner import Runner
from ui_agent.schema.check import CheckSpec
from ui_agent.schema.decision import Decision
from ui_agent.schema.plan import PlanStep
from ui_agent.schema.plan import TestPlan as Plan
from ui_agent.schema.state import Element, Option, PageInfo, State
from ui_agent.verify.asserts import parse_check

URL = "http://x/"


def state_of(url: str = URL, title: str = "T", elements: list[Element] | None = None) -> State:
    return State(page=PageInfo(url=url, title=title), elements=elements or [], fingerprint="f")


def element(label: str = "查询", role: str = "button", options: list[Option] | None = None) -> Element:
    return Element(index=1, role=role, label=label, operations=["CLICK"], options=options or [])


def make_session(tmp_path, records: list[CachedDecision]) -> tuple[CacheStore, CacheEntry]:
    store = CacheStore(tmp_path / "cache")
    entry = CacheEntry.build(key="k", case_hash="c", base_url=URL, plan_name="t")
    entry.records = records
    return store, entry


# --- record_from：决策语义化 ---


def test_click_record_keeps_semantics_not_index():
    state = state_of(title="T")
    rec = record_from(Decision(operation="CLICK", target="1", target_element=1, model="jev",
                               confidence=0.9), state, element())
    assert rec.operation == "CLICK" and rec.step == 1
    assert rec.target_label == "查询" and rec.target_role == "button"
    assert rec.url == "http://x" and rec.title == "T"
    assert rec.signature == state_signature(state) and len(rec.signature) == 16
    assert rec.source == "jev" and rec.at


def test_select_record_resolves_option_label_and_value():
    opts = [Option(key="1:1", dom_index=0, label="辽宁省", value="210000"),
            Option(key="1:2", dom_index=1, label="吉林省", value="220000")]
    el = element(label="省份", role="combobox", options=opts)
    decision = Decision(operation="SELECT", target="1:2", target_element=1, confidence=0.9)
    rec = record_from(decision, state_of(), el)
    assert (rec.target_option_label, rec.target_option_value) == ("吉林省", "220000")
    # 对不上的旧选项键（如快照里的下标漂了）：留空，不猜
    miss = record_from(Decision(operation="SELECT", target="9:9", target_element=1, confidence=0.9),
                       state_of(), el)
    assert miss.target_option_label == "" and miss.target_option_value == ""


def test_press_key_and_text_fields():
    rec = record_from(Decision(operation="PRESS_KEY", press_key="Enter", confidence=0.9), state_of())
    assert rec.press_key == "Enter" and rec.target_label == ""
    typed = record_from(Decision(operation="TYPE_TEXT", target="1", target_element=1, confidence=0.9),
                        state_of(), element(label="账号"), "hello")
    assert typed.text == "hello"


# --- Runner 端到端：成功才落盘、失败一个字节都不写 ---


def run_goal(tmp_path, snapshots, decisions, checks=(), vars=None, **over):
    specs = [parse_check(c) for c in checks]
    settings = settings_for(tmp_path, **{"ui_agent_cache": True, **over})
    session = open_session(settings, case_hash=goal_case_hash("目标", specs), base_url=URL,
                           plan_name="t")
    driver = FakeDriver(snapshots)
    driver.s = settings
    runner = Runner(settings, "目标", URL, checks=specs, task="t", driver=driver,
                    engine=FakeEngine(decisions), verbose=False, vars=vars, cache=session)
    return runner.run(), session, settings


def load_entry(settings, case_hash: str):
    return CacheStore(settings.cache_dir).load(cache_key(case_hash, URL))


def test_success_run_saves_semantic_records(tmp_path):
    specs = [parse_check("text_contains=查询完成")]
    result, session, settings = run_goal(
        tmp_path,
        [snapshot("a"), snapshot("b", text="查询完成")],
        [click(), done()],
        checks=["text_contains=查询完成"],
    )
    assert result.status == "ok"
    session.run_id = result.run_dir.name
    outcome = session.finalize()
    assert outcome.word == "saved" and outcome.saved_steps == 1 and outcome.dropped_steps == 0

    entry = load_entry(settings, goal_case_hash("目标", specs))
    assert entry is not None and entry.policy == CACHE_POLICY_VERSION
    assert [r.operation for r in entry.records] == ["CLICK", "DONE"]
    assert [r.step for r in entry.records] == [1, 1]
    assert entry.records[0].target_label == "查询" and entry.records[0].target_role == "button"
    assert entry.records[0].url == "http://x"
    assert entry.run_id == result.run_dir.name


def test_var_sourced_text_is_not_persisted(tmp_path):
    specs = [parse_check("text_contains=已登录")]
    result, session, settings = run_goal(
        tmp_path,
        [snapshot("a", labels=["账号"]), snapshot("b", text="已登录")],
        [Decision(operation="TYPE_TEXT", target="1", target_element=1, confidence=0.9), done()],
        checks=["text_contains=已登录"],
        vars={"账号": "sysadmin"},
    )
    session.finalize()
    assert result.status == "ok"
    entry = load_entry(settings, goal_case_hash("目标", specs))
    assert entry.records[0].operation == "TYPE_TEXT" and entry.records[0].target_label == "账号"
    assert entry.records[0].text == "", "--var 的值落盘了；值可能含凭据，回放时按标签现取"


def test_failed_run_writes_nothing(tmp_path):
    specs = [parse_check("text_contains=不会出现")]
    result, session, settings = run_goal(
        tmp_path,
        [snapshot("a"), snapshot("a")],
        [click(), done()],
        checks=["text_contains=不会出现"],
        ui_agent_assert_timeout_ms=0,
        ui_agent_supervisor=False,
    )
    assert result.status == "check_failed"
    outcome = session.finalize()
    assert outcome.word == "unconfirmed" and outcome.dropped_steps == 1
    assert outcome.saved_steps == 0
    assert load_entry(settings, goal_case_hash("目标", specs)) is None


def test_done_unverified_run_writes_nothing(tmp_path):
    result, session, settings = run_goal(
        tmp_path,
        [snapshot("a"), snapshot("a")],
        [click(), done()],
        checks=[],
    )
    assert result.status == "done_unverified"
    outcome = session.finalize()
    assert outcome.word == "unconfirmed" and outcome.dropped_steps == 1
    assert load_entry(settings, goal_case_hash("目标", [])) is None


def test_cache_off_opens_no_session(tmp_path):
    settings = settings_for(tmp_path)
    assert open_session(settings, case_hash="c", base_url=URL) is None
    result, _, _ = run_goal(tmp_path, [snapshot("a")], [done()], checks=["element_exists=查询"],
                            ui_agent_cache=False)
    assert result.status == "ok"
    assert not (tmp_path / "cache").exists()


def test_cache_sourced_decisions_are_not_restaged(tmp_path):
    settings = settings_for(tmp_path, ui_agent_cache=True)
    session = open_session(settings, case_hash="c", base_url=URL)
    runner = Runner(settings, "目标", URL, task="t", driver=FakeDriver([snapshot("a")]),
                    engine=FakeEngine([]), verbose=False, cache=session)
    state = state_of()
    runner._stage(Decision(operation="CLICK", model="cache", confidence=0.9), state)
    assert list(session._staged) == [], "回放命中的决策不该重录（消费已计数）"
    runner._stage(Decision(operation="CLICK", target="1", confidence=0.9), state)
    assert [r.operation for r in session._staged[1]] == ["CLICK"]


def test_plan_runner_auto_opens_and_settles_per_step(tmp_path):
    settings = settings_for(tmp_path, ui_agent_cache=True)
    plan = Plan(name="两步", url=URL, steps=[
        PlanStep(goal="第一步", checks=[CheckSpec(kind="text_contains", expected="ok")]),
    ])
    driver = FakeDriver([snapshot("a"), snapshot("b", text="ok")])
    driver.s = settings
    runner = PlanRunner(settings, plan, driver=driver, engine=FakeEngine([click(), done()]),
                        verbose=False)
    result = runner.run()

    assert result.status == "ok"
    assert runner.cache_outcome is not None and runner.cache_outcome.word == "saved"
    entry = CacheStore(settings.cache_dir).load(cache_key(plan_case_hash(plan), URL))
    assert [r.step for r in entry.records] == [1, 1]
    assert [r.operation for r in entry.records] == ["CLICK", "DONE"]

    rows = read_rows(result.run_dir)
    cache_rows = [row for row in rows if row["kind"] == "cache"]
    assert len(cache_rows) == 1 and cache_rows[0]["write"] == "saved"
    assert cache_rows[0]["seq"] < rows[-1]["seq"] and rows[-1]["kind"] == "run_end", \
        "cache 行写在根 trace 里、排在 run_end 之前"


# --- ReplaySession：结算闸门的组合 ---


def test_full_hit_keeps_file_untouched(tmp_path):
    store, entry = make_session(tmp_path, [
        CachedDecision(step=1, operation="CLICK", target_label="查询"),
        CachedDecision(step=1, operation="DONE"),
    ])
    store.save(entry)
    before = store.path_for("k").read_text(encoding="utf-8")
    session = ReplaySession(store, entry, run_id="r2")
    session.begin_step(1)
    session.advance()
    session.advance()
    session.settle_step("ok")
    outcome = session.finalize()
    assert outcome.word == "kept" and outcome.kept_steps == 1 and outcome.hits == 2
    assert store.path_for("k").read_text(encoding="utf-8") == before


def test_prefix_plus_staged_rewrites_suffix(tmp_path):
    store, entry = make_session(tmp_path, [
        CachedDecision(step=1, operation="CLICK", target_label="一"),
        CachedDecision(step=1, operation="CLICK", target_label="二"),
        CachedDecision(step=1, operation="DONE"),
    ])
    store.save(entry)
    session = ReplaySession(store, entry, run_id="r2")
    session.begin_step(1)
    session.advance()  # 命中第一条，第二条起失配交还模型
    session.stage(CachedDecision(step=1, operation="CLICK", target_label="改后"))
    session.stage(CachedDecision(step=1, operation="DONE"))
    session.settle_step("ok")
    outcome = session.finalize()
    assert outcome.word == "saved" and outcome.saved_steps == 1 and outcome.hits == 1

    loaded = store.load("k")
    assert [r.target_label for r in loaded.records] == ["一", "改后", ""]
    assert loaded.run_id == "r2"


def test_failure_evicts_whole_step_and_keeps_others(tmp_path):
    store, entry = make_session(tmp_path, [
        CachedDecision(step=1, operation="CLICK", target_label="一"),
        CachedDecision(step=1, operation="CLICK", target_label="二"),
        CachedDecision(step=2, operation="CLICK", target_label="别的步"),
    ])
    store.save(entry)
    session = ReplaySession(store, entry, run_id="r2")
    session.begin_step(1)
    session.advance()
    session.stage(CachedDecision(step=1, operation="CLICK", target_label="重录"))
    session.settle_step("stuck")
    outcome = session.finalize()

    assert outcome.word == "evicted" and outcome.evicted_steps == 1 and outcome.dropped_steps == 1
    loaded = store.load("k")
    assert [r.target_label for r in loaded.records] == ["别的步"], "失败株连只淘汰该步，别的步原样"


def test_done_unverified_keeps_old_records(tmp_path):
    store, entry = make_session(tmp_path, [
        CachedDecision(step=1, operation="CLICK", target_label="一"),
        CachedDecision(step=1, operation="DONE"),
    ])
    store.save(entry)
    before = store.path_for("k").read_text(encoding="utf-8")
    session = ReplaySession(store, entry, run_id="r2")
    session.begin_step(1)
    session.advance()
    session.stage(CachedDecision(step=1, operation="CLICK", target_label="重录"))
    session.settle_step("done_unverified")
    outcome = session.finalize()

    assert outcome.word == "unconfirmed" and outcome.dropped_steps == 1
    assert outcome.evicted_steps == 0, "没有断言的自称成功不该株连旧录像"
    assert store.path_for("k").read_text(encoding="utf-8") == before


def test_rerecord_identical_is_no_change(tmp_path):
    store, entry = make_session(tmp_path, [
        CachedDecision(step=1, operation="CLICK", target_label="一"),
        CachedDecision(step=1, operation="DONE"),
    ])
    store.save(entry)
    before = store.path_for("k").read_text(encoding="utf-8")
    session = ReplaySession(store, entry, run_id="r2")
    session.begin_step(1)
    session.stage(CachedDecision(step=1, operation="CLICK", target_label="一"))
    session.stage(CachedDecision(step=1, operation="DONE"))
    session.settle_step("ok")
    outcome = session.finalize()

    assert outcome.word == "no-change" and outcome.no_change_steps == 1
    assert store.path_for("k").read_text(encoding="utf-8") == before


def test_miss_reasons_are_counted(tmp_path):
    store, entry = make_session(tmp_path, [])
    session = ReplaySession(store, entry)
    session.miss("wrong-context")
    session.miss("wrong-context")
    session.miss("truncated")
    outcome = session.finalize()
    assert outcome.word == "not-written" and outcome.misses == 3
    assert outcome.miss_reasons == {"wrong-context": 2, "truncated": 1}


def test_replay_failed_truncates_prefix_and_abandons_rest(tmp_path):
    store, entry = make_session(tmp_path, [
        CachedDecision(step=1, operation="CLICK", target_label="一"),
        CachedDecision(step=1, operation="CLICK", target_label="二"),
        CachedDecision(step=1, operation="DONE"),
    ])
    session = ReplaySession(store, entry, run_id="r2")
    session.begin_step(1)
    session.advance()
    session.advance()
    session.replay_failed()
    assert session.consumed == 1, "失败的那条不算已消费，成立前缀只到它前面"
    assert session.handed_over(), "一旦失败，本步回放就此终止（不再续放）"
    assert session.miss_reasons == {"retry": 1}
    session.stage(CachedDecision(step=1, operation="CLICK", target_label="重来"))
    session.stage(CachedDecision(step=1, operation="DONE"))
    session.settle_step("ok")
    outcome = session.finalize()

    assert outcome.word == "saved"
    loaded = store.load("k")
    assert [r.target_label for r in loaded.records] == ["一", "重来", ""], "只留成立过的前缀 + 重录"


def test_replay_failed_then_identical_rerecord_is_no_change(tmp_path):
    store, entry = make_session(tmp_path, [
        CachedDecision(step=1, operation="CLICK", target_label="一"),
        CachedDecision(step=1, operation="DONE"),
    ])
    store.save(entry)
    before = store.path_for("k").read_text(encoding="utf-8")
    session = ReplaySession(store, entry, run_id="r2")
    session.begin_step(1)
    session.advance()
    session.advance()
    session.replay_failed()
    session.stage(CachedDecision(step=1, operation="DONE"))
    session.settle_step("ok")
    outcome = session.finalize()

    assert outcome.word == "no-change" and outcome.kept_steps == 0, "失败过一次就不算纯命中"
    assert store.path_for("k").read_text(encoding="utf-8") == before


def test_replay_failed_then_failure_still_evicts(tmp_path):
    store, entry = make_session(tmp_path, [
        CachedDecision(step=1, operation="CLICK", target_label="一"),
    ])
    session = ReplaySession(store, entry, run_id="r2")
    session.begin_step(1)
    session.advance()
    session.replay_failed()
    session.settle_step("cache_missed")
    outcome = session.finalize()
    assert outcome.word == "evicted" and outcome.evicted_steps == 1


def test_handover_rerecord_is_appended_as_new_variant(tmp_path):
    store, entry = make_session(tmp_path, [
        CachedDecision(step=1, operation="CLICK", target_label="旧起点"),
        CachedDecision(step=1, operation="DONE"),
    ])
    session = ReplaySession(store, entry, run_id="r2")
    session.begin_step(1)
    session.miss("no-variant")
    session.hand_over()
    session.stage(CachedDecision(step=1, operation="CLICK", target_label="新起点"))
    session.stage(CachedDecision(step=1, operation="DONE"))
    session.settle_step("ok")
    outcome = session.finalize()

    assert outcome.word == "saved" and outcome.saved_steps == 1
    loaded = store.load("k")
    assert [(r.variant, r.target_label) for r in loaded.records] == [
        (1, "旧起点"), (1, ""), (2, "新起点"), (2, "")], "新起点的路径保序追加，不拼进旧变体"


def test_failure_evicts_only_played_variant(tmp_path):
    store, entry = make_session(tmp_path, [
        CachedDecision(step=1, operation="CLICK", target_label="A路", variant=1),
        CachedDecision(step=1, operation="DONE", variant=1),
        CachedDecision(step=1, operation="CLICK", target_label="B路", variant=2),
        CachedDecision(step=1, operation="DONE", variant=2),
    ])
    session = ReplaySession(store, entry, run_id="r2")
    session.begin_step(1)
    session.activate(2)
    session.advance()
    session.stage(CachedDecision(step=1, operation="CLICK", target_label="重录"))
    session.settle_step("cache_missed")
    outcome = session.finalize()

    assert outcome.word == "evicted" and outcome.evicted_steps == 1 and outcome.dropped_steps == 1
    loaded = store.load("k")
    assert [(r.variant, r.target_label) for r in loaded.records] == [(1, "A路"), (1, "")], \
        "失败只淘汰本步回放过的那条变体，别的变体原样"


def test_probe_miss_without_playback_evicts_nothing(tmp_path):
    store, entry = make_session(tmp_path, [
        CachedDecision(step=1, operation="CLICK", target_label="查询"),
    ])
    session = ReplaySession(store, entry, run_id="r2")
    session.begin_step(1)
    session.activate(1)
    session.miss("wrong-context")
    session.hand_over()
    session.settle_step("cache_missed")
    outcome = session.finalize()
    assert outcome.word == "not-written" and outcome.evicted_steps == 0, \
        "一条都没放过（探针场景）：旧变体不该被株连"


def test_outcome_mode_vocabulary(tmp_path):
    store, entry = make_session(tmp_path, [])
    assert ReplaySession(store, entry).finalize().mode == "missed"

    dropped = ReplaySession(store, entry)
    dropped.begin_step(1)
    dropped.stage(CachedDecision(step=1, operation="CLICK"))
    dropped.settle_step("done_unverified")
    assert dropped.finalize().mode == "agent-concluded"

    hit_store, hit_entry = make_session(tmp_path, [CachedDecision(step=1, operation="CLICK")])
    hit = ReplaySession(hit_store, hit_entry)
    hit.begin_step(1)
    hit.advance()
    hit.settle_step("ok")
    assert hit.finalize().mode == "self-finalized"


def test_stage_stamps_current_step(tmp_path):
    store, entry = make_session(tmp_path, [])
    session = ReplaySession(store, entry)
    session.begin_step(2)
    rec = CachedDecision(operation="CLICK")
    session.stage(rec)
    assert rec.step == 2


# --- open_session 的门 ---


def test_open_session_gates_and_identity(tmp_path):
    assert open_session(settings_for(tmp_path), case_hash="c", base_url=URL) is None
    on = settings_for(tmp_path, ui_agent_cache=True)
    assert open_session(on, case_hash="c", base_url="") is None

    session = open_session(on, case_hash="c", base_url=URL, plan_name="p", run_id="r1")
    assert session is not None and session.strict is False
    assert session.entry.key == cache_key("c", URL)
    assert session.entry.plan_name == "p" and session.entry.created_at

    session.entry.records = [CachedDecision(step=1, operation="CLICK")]
    session.store.save(session.entry)
    reloaded = open_session(on, case_hash="c", base_url=URL)
    assert [r.operation for r in reloaded.entry.records] == ["CLICK"]


def test_open_session_strict_flag(tmp_path):
    settings = settings_for(tmp_path, ui_agent_cache=True, ui_agent_cache_strict=True)
    session = open_session(settings, case_hash="c", base_url=URL)
    assert session.strict is True


# --- 运行开始的缓存现状播报（boot_lines）与旧策略检测 ---


def test_boot_lines_report_records_and_zero_model(tmp_path):
    settings = settings_for(tmp_path, ui_agent_cache=True)
    session = open_session(settings, case_hash="c", base_url=URL)
    assert any("没有录像" in line and "边跑边录制" in line for line in session.boot_lines("jev"))
    assert any("没有录像" in line and "决策模型已关闭" in line for line in session.boot_lines("off"))

    session.entry.records = [CachedDecision(step=1, operation="CLICK")]
    session.store.save(session.entry)
    reloaded = open_session(settings, case_hash="c", base_url=URL)
    assert reloaded.records == 1
    assert any("可用录像 1 条" in line for line in reloaded.boot_lines("off"))


def test_stale_policy_recordings_are_reported(tmp_path):
    settings = settings_for(tmp_path, ui_agent_cache=True)
    store = CacheStore(settings.cache_dir)
    stale = CacheEntry.build(key="oldk", case_hash="c", base_url=URL, plan_name="t")
    stale.policy = "v1"
    store.save(stale)
    other = CacheEntry.build(key="oldk2", case_hash="别的用例", base_url=URL)
    other.policy = "v1"
    store.save(other)

    session = open_session(settings, case_hash="c", base_url=URL)
    assert session.stale == ["v1"], "只认同一用例×环境的旧录像"
    lines = session.boot_lines("off")
    assert any("v1" in line and "已随缓存升级作废" in line and "缓存未命中" in line for line in lines)

    fresh = open_session(settings, case_hash="c", base_url=URL)
    fresh.entry.records = [CachedDecision(step=1, operation="CLICK")]
    fresh.store.save(fresh.entry)
    happy = open_session(settings, case_hash="c", base_url=URL)
    assert happy.stale == [], "现键录像可用时不再播报旧策略噪音"
