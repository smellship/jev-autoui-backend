"""回放执行的离线单测：语义重定位、上下文守卫、严判探针、零模型命中与失配停机。"""

from __future__ import annotations

from fakes import FakeDriver, FakeEngine, click, done, read_rows, settings_for, snapshot

from ui_agent.driver.ladder import LadderOutcome
from ui_agent.replay.cache import CachedDecision, CacheEntry, CacheStore, goal_case_hash, state_signature
from ui_agent.replay.match import match_next
from ui_agent.replay.session import ReplaySession, open_session
from ui_agent.run.runner import Runner
from ui_agent.run.trace import append_row, cache_row
from ui_agent.schema.decision import Decision
from ui_agent.schema.state import Element, Option, PageInfo, State
from ui_agent.verify.asserts import parse_check

URL = "http://x/"
CHECKS = ["text_contains=查询完成"]


def state_of(labels: list[str] | None = None, url: str = URL, title: str = "T") -> State:
    labels = ["查询"] if labels is None else labels
    elements = [Element(index=i + 1, role="button", label=label, operations=["CLICK"])
                for i, label in enumerate(labels)]
    return State(page=PageInfo(url=url, title=title), elements=elements, fingerprint="f")


def rec(operation: str = "CLICK", label: str = "查询", **kw) -> CachedDecision:
    kw.setdefault("url", URL)
    kw.setdefault("title", "T")
    return CachedDecision(step=1, operation=operation, target_label=label, **kw)


def session_with(tmp_path, records: list[CachedDecision]) -> ReplaySession:
    store = CacheStore(tmp_path / "cache")
    entry = CacheEntry.build(key="k", case_hash="c", base_url=URL)
    entry.records = records
    session = ReplaySession(store, entry)
    session.begin_step(1)
    return session


# --- match_next：守卫与重定位 ---


def test_no_entry_and_truncated(tmp_path):
    session = session_with(tmp_path, [])
    assert match_next(session, state_of()) == (None, "no-entry")
    session = session_with(tmp_path, [rec("DONE")])
    session.advance()
    assert match_next(session, state_of()) == (None, "truncated")


def test_context_guard_url_and_title(tmp_path):
    # 变体选择的头匹配：录像起点与当前页面不符 → 没有可用的变体
    session = session_with(tmp_path, [rec("CLICK", url="http://y/")])
    assert match_next(session, state_of())[1] == "no-variant"
    session = session_with(tmp_path, [rec("CLICK", title="别的页面")])
    assert match_next(session, state_of())[1] == "no-variant"
    # 地址比对忽略 query 与尾斜杠；标题两边都有才算身份
    session = session_with(tmp_path, [rec("CLICK", url="http://x?a=1", title="")])
    decision, _ = match_next(session, state_of(title="随便"))
    assert decision is not None and decision.operation == "CLICK"


def test_click_relocates_by_label_and_role(tmp_path):
    session = session_with(tmp_path, [rec("CLICK")])
    decision, miss = match_next(session, state_of(["别的", "查询"]))
    assert miss == "" and decision.model == "cache"
    assert decision.target_element == 2, "重定位到当次快照里的元素，而不是录像里的旧索引"
    assert session.consumed == 1
    # 标签唯一、角色变了也认（button → link 这类皮肤调整）
    session = session_with(tmp_path, [rec("CLICK", role="link")])
    decision, _ = match_next(session, state_of(["查询"]))
    assert decision is not None and decision.target_element == 1
    # 同名元素没法靠角色消歧：按未命中交还，绝不乱点
    session = session_with(tmp_path, [rec("CLICK", role="link")])
    state = State(page=PageInfo(url=URL, title="T"), elements=[
        Element(index=1, role="button", label="查询"), Element(index=2, role="button", label="查询")])
    assert match_next(session, state)[1] == "invalid-entry"


def test_element_gone_is_invalid_entry(tmp_path):
    session = session_with(tmp_path, [rec("CLICK", label="已经删掉的按钮")])
    assert match_next(session, state_of())[1] == "invalid-entry"


def test_select_relocates_option_by_label_then_value(tmp_path):
    def combobox() -> State:
        options = [Option(key="1:1", dom_index=0, label="辽宁省", value="210000"),
                   Option(key="1:2", dom_index=1, label="吉林省", value="220000")]
        return State(page=PageInfo(url=URL, title="T"),
                     elements=[Element(index=1, role="combobox", label="省份", options=options)])

    session = session_with(tmp_path, [rec("SELECT", label="省份", role="combobox",
                                          target_option_label="吉林省", target_option_value="220000")])
    decision, _ = match_next(session, combobox())
    assert decision is not None and decision.operation == "SELECT" and decision.target == "1:2"
    # 选项标签变了但 value 没变：按 value 兜底
    session = session_with(tmp_path, [rec("SELECT", label="省份", role="combobox",
                                          target_option_label="旧名", target_option_value="220000")])
    decision, _ = match_next(session, combobox())
    assert decision is not None and decision.target == "1:2"
    session = session_with(tmp_path, [rec("SELECT", label="省份", role="combobox",
                                          target_option_label="不存在", target_option_value="9")])
    assert match_next(session, combobox())[1] == "invalid-entry"


def test_freeband_operations(tmp_path):
    session = session_with(tmp_path, [rec("DONE")])
    decision, _ = match_next(session, state_of())
    assert decision is not None and decision.operation == "DONE" and decision.model == "cache"
    session = session_with(tmp_path, [rec("PRESS_KEY", label="", press_key="Enter")])
    decision, _ = match_next(session, state_of())
    assert decision.press_key == "Enter"
    session = session_with(tmp_path, [rec("PRESS_KEY", label="", press_key="")])
    assert match_next(session, state_of())[1] == "invalid-entry"
    session = session_with(tmp_path, [rec("SCROLL_DOWN", label="")])
    assert match_next(session, state_of())[0].operation == "SCROLL_DOWN"
    session = session_with(tmp_path, [rec("TELEPORT", label="")])
    assert match_next(session, state_of())[1] == "invalid-entry"


def test_type_text_carries_stored_value(tmp_path):
    session = session_with(tmp_path, [rec("TYPE_TEXT", label="账号", text="sysadmin")])
    decision, _ = match_next(session, state_of(["账号"]))
    assert decision.operation == "TYPE_TEXT" and decision.raw_answers == {"text": "sysadmin"}
    session = session_with(tmp_path, [rec("TYPE_TEXT", label="账号", text="")])
    decision, _ = match_next(session, state_of(["账号"]))
    assert decision.raw_answers == {}, "var 来源的值不落盘：回放时按 --var 现取"


def test_strict_probe_flags_signature_change(tmp_path):
    state = state_of()
    session = session_with(tmp_path, [rec("CLICK", signature=state_signature(state))])
    decision, _ = match_next(session, state_of())
    assert decision is not None, "签名一致时严判照常命中"
    session = session_with(tmp_path, [rec("CLICK", signature=state_signature(state))])
    session.strict = True
    assert match_next(session, state_of(["查询", "多出来的元素"]))[1] == "wrong-context"
    # 非严判下结构变了也照常按语义重定位
    session = session_with(tmp_path, [rec("CLICK", signature=state_signature(state))])
    decision, _ = match_next(session, state_of(["查询", "多出来的元素"]))
    assert decision is not None


# --- Runner 端到端：零模型命中、失配停机、接力回填 ---


def run_goal(tmp_path, snapshots, decisions, checks=(), engine=None, **over):
    specs = [parse_check(c) for c in checks]
    settings = settings_for(tmp_path, **{"ui_agent_cache": True, **over})
    session = open_session(settings, case_hash=goal_case_hash("目标", specs), base_url=URL,
                           plan_name="t")
    driver = FakeDriver(snapshots)
    driver.s = settings
    runner = Runner(settings, "目标", URL, checks=specs, task="t", driver=driver,
                    engine=engine or FakeEngine(decisions), verbose=False, cache=session)
    result = runner.run()
    session.run_id = result.run_dir.name
    outcome = session.finalize()
    append_row(result.run_dir / "trace.jsonl", cache_row(outcome))  # 与 CLI 目标模式同路径
    return result, session, outcome


def test_pure_replay_is_zero_model(tmp_path):
    run_goal(tmp_path, [snapshot("a"), snapshot("b", text="查询完成")],
             [click(), done()], checks=CHECKS)
    engine = FakeEngine([])
    result, session, outcome = run_goal(
        tmp_path, [snapshot("a"), snapshot("b", text="查询完成")], [],
        checks=CHECKS, engine=engine, ui_agent_decider="off")

    assert result.status == "ok", f"纯回放应命中并过断言：{result.detail}"
    assert engine.seen_states == [], "零模型模式下一次决策模型都不该被问"
    assert outcome.word == "kept" and outcome.hits == 2 and outcome.misses == 0

    rows = read_rows(result.run_dir)
    cache_rows = [row for row in rows if row["kind"] == "cache"]
    assert len(cache_rows) == 1 and cache_rows[0]["mode"] == "self-finalized"
    assert cache_rows[0]["write"] == "kept" and cache_rows[0]["hits"] == 2
    assert cache_rows[0]["seq"] == max(row["seq"] for row in rows), "收尾补写的行要接在原有行序后"


def test_zero_model_miss_stops_immediately(tmp_path):
    engine = FakeEngine([])
    result, session, outcome = run_goal(
        tmp_path, [snapshot("a")], [], checks=CHECKS, engine=engine, ui_agent_decider="off")

    assert result.status == "cache_missed" and "no-entry" in result.detail
    assert "没有可用录像" in result.detail, "零模型下空录像要直接点名，不能只说'回放未命中'"
    assert engine.seen_states == []
    assert outcome.misses == 1 and outcome.miss_reasons == {"no-entry": 1}
    assert outcome.word == "not-written", "没命中也没有新录制，缓存应一个字节不动"


def test_miss_in_model_mode_relays_and_backfills(tmp_path):
    run_goal(tmp_path, [snapshot("a"), snapshot("b", text="查询完成")],
             [click(), done()], checks=CHECKS)
    # 页面改版：按钮标签换了——回放对不上，交还模型；模型跑完后整步重录（自愈回填）
    engine = FakeEngine([Decision(operation="CLICK", target="1", target_element=1, confidence=0.9),
                         done()])
    result, session, outcome = run_goal(
        tmp_path, [snapshot("a", labels=["新按钮"]), snapshot("b", text="查询完成", labels=["新按钮"])],
        [], checks=CHECKS, engine=engine, ui_agent_decider="llm")

    assert result.status == "ok"
    assert len(engine.seen_states) == 2, "失配后该由模型接管本步剩余决策"
    assert outcome.word == "saved" and outcome.miss_reasons == {"invalid-entry": 1}

    entry = CacheStore(tmp_path / "cache").load(session.entry.key)
    assert [r.target_label for r in entry.records] == ["新按钮", ""], "接力跑完的重录覆盖旧录像"


def test_strict_run_probes_page_change(tmp_path):
    run_goal(tmp_path, [snapshot("a"), snapshot("b", text="查询完成")],
             [click(), done()], checks=CHECKS)
    engine = FakeEngine([])
    result, session, outcome = run_goal(
        tmp_path, [snapshot("a", labels=["查询", "多出来的元素"]),
                   snapshot("b", text="查询完成", labels=["查询", "多出来的元素"])],
        [], checks=CHECKS, engine=engine, ui_agent_decider="off", ui_agent_cache_strict=True)

    assert result.status == "cache_missed"
    assert outcome.miss_reasons == {"wrong-context": 1}
    assert outcome.evicted_steps == 0, "只是探到页面变了，旧录像不该被淘汰"


def test_replayed_done_failing_assert_hands_back_then_evicts(tmp_path):
    run_goal(tmp_path, [snapshot("a"), snapshot("b", text="查询完成")],
             [click(), done()], checks=CHECKS)
    # 页面真的变了：回放放到收尾断言没过——结论不成立，交还模型；模型也没救回来则整步淘汰
    engine = FakeEngine([done()])
    result, session, outcome = run_goal(
        tmp_path, [snapshot("a"), snapshot("b")], [], checks=CHECKS, engine=engine,
        ui_agent_assert_timeout_ms=0)

    assert result.status == "check_failed" and result.steps == 2
    assert len(engine.seen_states) == 1, "收尾失败后该把决策交还模型"
    assert outcome.miss_reasons.get("retry") == 1, "回放收尾没过断言要记 retry"
    assert outcome.word == "evicted" and outcome.evicted_steps == 1
    entry = CacheStore(tmp_path / "cache").load(session.entry.key)
    assert entry.for_step(1) == [], "结论不成立的录像整步淘汰，不留给下次继续投毒"


def test_replayed_action_failure_in_zero_model_mode_stops(tmp_path):
    run_goal(tmp_path, [snapshot("a"), snapshot("b", text="查询完成")],
             [click(), done()], checks=CHECKS)
    specs = [parse_check(CHECKS[0])]
    settings = settings_for(tmp_path, **{"ui_agent_cache": True, "ui_agent_decider": "off"})
    session = open_session(settings, case_hash=goal_case_hash("目标", specs), base_url=URL,
                           plan_name="t")
    driver = FakeDriver([snapshot("a")])
    driver.s = settings
    driver.click = lambda index, allow_force=False: LadderOutcome(
        ok=False, attempts=[("L0", "目标不可达")])
    runner = Runner(settings, "目标", URL, checks=specs, task="t", driver=driver,
                    engine=FakeEngine([]), verbose=False, cache=session)
    result = runner.run()
    outcome = session.finalize()

    assert result.status == "cache_missed", "回放执行失败后，零模型模式立刻停"
    assert outcome.miss_reasons == {"retry": 1}
    assert outcome.word == "evicted" and outcome.evicted_steps == 1


# --- 变体化：多起点录制/回放、中途失配的终局交还、回放豁免卡住 ---


def test_two_start_variants_record_and_replay(tmp_path):
    login_url = "http://x/login"
    # 起点 A（登录页）先录一遍
    result, session, outcome = run_goal(
        tmp_path, [snapshot("a", labels=["登录"], url=login_url),
                   snapshot("b", text="查询完成", labels=["登录"], url=login_url)],
        [click(), done()], checks=CHECKS)
    assert result.status == "ok" and outcome.word == "saved"

    # 起点 B（已登录页）：旧录像起点不符 → 整步交还模型，从头重录成第二条变体
    result, session, outcome = run_goal(
        tmp_path, [snapshot("c", labels=["查询"], url=URL),
                   snapshot("d", text="查询完成", labels=["查询"], url=URL)],
        [click(), done()], checks=CHECKS)
    assert result.status == "ok" and outcome.word == "saved"
    assert outcome.miss_reasons == {"no-variant": 1}, "起点不符要报 no-variant，不能硬套旧录像"

    entry = CacheStore(tmp_path / "cache").load(session.entry.key)
    assert [(r.variant, r.target_label) for r in entry.records] == [
        (1, "登录"), (1, ""), (2, "查询"), (2, "")], "第二条起点的路径保序追加为新变体"

    # 零模型分别回放两个起点：各自选中匹配的变体
    result, session, outcome = run_goal(
        tmp_path, [snapshot("e", labels=["登录"], url=login_url),
                   snapshot("f", text="查询完成", labels=["登录"], url=login_url)],
        [], checks=CHECKS, engine=FakeEngine([]), ui_agent_decider="off")
    assert result.status == "ok" and outcome.word == "kept" and outcome.misses == 0

    result, session, outcome = run_goal(
        tmp_path, [snapshot("g", labels=["查询"], url=URL),
                   snapshot("h", text="查询完成", labels=["查询"], url=URL)],
        [], checks=CHECKS, engine=FakeEngine([]), ui_agent_decider="off")
    assert result.status == "ok" and outcome.word == "kept" and outcome.misses == 0

    rows = read_rows(result.run_dir)
    decisions = [row for row in rows if row.get("kind", "decision") == "decision"]
    assert [row.get("model") for row in decisions] == ["cache", "cache"]


def test_mid_step_miss_hands_over_and_rerecords(tmp_path):
    specs = [parse_check(CHECKS[0])]
    settings = settings_for(tmp_path, ui_agent_cache=True)
    seeded = open_session(settings, case_hash=goal_case_hash("目标", specs), base_url=URL)
    seeded.entry.records = [
        CachedDecision(step=1, operation="CLICK", target_label="一", target_role="button",
                       url=URL, title="T"),
        CachedDecision(step=1, operation="CLICK", target_label="二", target_role="button",
                       url=URL, title="T"),
        CachedDecision(step=1, operation="DONE", url=URL, title="T"),
    ]
    seeded.store.save(seeded.entry)

    engine = FakeEngine([Decision(operation="CLICK", target="1", target_element=1, confidence=0.9),
                         done()])
    result, session, outcome = run_goal(
        tmp_path, [snapshot("a", labels=["一"]), snapshot("b", labels=["三"]),
                   snapshot("c", text="查询完成", labels=["三"])],
        [], checks=CHECKS, engine=engine, ui_agent_decider="llm")

    assert result.status == "ok"
    assert outcome.miss_reasons == {"invalid-entry": 1}, "中途失配记一次，之后不再续放"
    assert len(engine.seen_states) == 2, "失配后余下决策全交模型"
    loaded = session.store.load(session.entry.key)
    assert [r.target_label for r in loaded.records] == ["一", "三", ""], \
        "成立过的前缀 + 模型接管后的动作合并回原变体"


def test_replayed_repeat_clicks_are_exempt_from_stuck(tmp_path):
    # 录制：三连点 + 收尾（页面指纹逐跳变化，不触发卡住）
    run_goal(tmp_path, [snapshot("a1"), snapshot("a2"), snapshot("a3"),
                        snapshot("a4", text="查询完成")],
             [click(), click(), click(), done()], checks=CHECKS)
    # 回放：页面纹丝不动——无豁免时三连点会被 repeat_no_progress 拦下
    engine = FakeEngine([])
    result, session, outcome = run_goal(
        tmp_path, [snapshot("a"), snapshot("a"), snapshot("a"), snapshot("a", text="查询完成")],
        [], checks=CHECKS, engine=engine, ui_agent_decider="off")

    assert result.status == "ok", "回放是照播录制时验证过的录像，不该重新判卡住"
    assert engine.seen_states == []
    assert outcome.word == "kept" and outcome.hits == 4
