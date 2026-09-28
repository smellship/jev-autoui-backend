"""UPLOAD（上传）的离线单测：取值纪律、执行前校验、动作空间与产物——不起浏览器、不调模型。"""

from __future__ import annotations

import json

import pytest
from fakes import FakeDriver, FakeEngine, blocked, done, read_trace, settings_for

from ui_agent.act.executor import Executor
from ui_agent.act.upload import accepted, resolve_files
from ui_agent.config import Settings
from ui_agent.decide.jev import (
    JevEngine,
    available_operations,
    build_action_space,
    build_questions,
    to_jev_state,
)
from ui_agent.driver.playwright_ import file_ref
from ui_agent.observe.snapshot import build_state
from ui_agent.run.runner import Runner
from ui_agent.schema.decision import Decision
from ui_agent.schema.state import Element, PageInfo, State

UPLOAD_LABEL = "将发票文件拖拽至此处 或 单击上传文件按钮 或 单击本框均可上传文件"


def elem(label: str = UPLOAD_LABEL, operations=("UPLOAD",), **over) -> Element:
    fields = {"role": "file", "accept": ".pdf,.jpg,.jpeg,.png", "multiple": True, "hidden": True, **over}
    return Element(index=1, label=label, operations=list(operations), **fields)


def state_with(elements: list[Element]) -> State:
    return State(page=PageInfo(url="http://x/", title="T"), elements=elements)


def make_png(tmp_path, name: str = "发票.png") -> "object":
    path = tmp_path / name
    path.write_bytes(b"png-bytes")
    return path


class TestAcceptMatching:
    @pytest.mark.parametrize(
        ("name", "accept", "ok"),
        [
            ("a.png", ".pdf,.jpg,.jpeg,.png", True),
            ("a.PNG", ".png", True),
            ("a.txt", ".pdf,.jpg,.jpeg,.png", False),
            ("a.txt", "", True),                     # 页面没声明 accept → 交给页面自己校验
            ("a.png", "image/*", True),              # 通配 MIME
            ("a.png", "image/png", True),
            ("a.pdf", "application/pdf", True),
            ("a.pdf", "image/*", False),
        ],
    )
    def test_accept_declarations(self, tmp_path, name, accept, ok):
        path = tmp_path / name
        path.write_bytes(b"x")
        assert accepted(file_ref(path), accept) is ok


class TestResolveFiles:
    def test_paths_come_from_vars_and_only_names_are_recorded(self, tmp_path):
        png = make_png(tmp_path)
        files, meta = resolve_files(elem(), {"发票文件": str(png)})
        assert [f.name for f in files] == ["发票.png"]
        assert meta["source"] == "var" and meta["key"] == "发票文件"
        assert meta["files"] == [{"name": "发票.png", "size": 9}]
        assert str(tmp_path) not in json.dumps(meta, ensure_ascii=False)  # 路径不落盘

    def test_semicolon_separates_multiple_files(self, tmp_path):
        first, second = make_png(tmp_path, "a.png"), make_png(tmp_path, "b.png")
        files, _ = resolve_files(elem(), {"发票文件": f"{first}; {second}"})
        assert [f.name for f in files] == ["a.png", "b.png"]

    def test_no_matching_var_is_refused_not_guessed(self, tmp_path):
        make_png(tmp_path)
        with pytest.raises(RuntimeError, match="没有匹配的文件路径变量"):
            resolve_files(elem(), {"账号": "sysadmin"})
        with pytest.raises(RuntimeError, match="未执行上传"):
            resolve_files(elem(), None)

    def test_empty_value_is_refused(self):
        with pytest.raises(RuntimeError, match="没有给出文件路径"):
            resolve_files(elem(), {"发票文件": "  ;  "})

    def test_missing_file_is_refused(self, tmp_path):
        with pytest.raises(RuntimeError, match="文件不存在"):
            resolve_files(elem(), {"发票文件": str(tmp_path / "缺.png")})

    def test_extension_outside_accept_is_refused(self, tmp_path):
        txt = tmp_path / "a.txt"
        txt.write_bytes(b"x")
        with pytest.raises(RuntimeError, match="不在控件 accept 声明里"):
            resolve_files(elem(), {"发票文件": str(txt)})
        files, _ = resolve_files(elem(accept=""), {"发票文件": str(txt)})  # 没声明就放行
        assert [f.name for f in files] == ["a.txt"]

    def test_two_files_need_a_multiple_input(self, tmp_path):
        first, second = make_png(tmp_path, "a.png"), make_png(tmp_path, "b.png")
        with pytest.raises(RuntimeError, match="只接受一个文件"):
            resolve_files(elem(multiple=False), {"发票文件": f"{first};{second}"})


class TestActionSpace:
    def test_upload_is_offered_only_when_a_file_control_exists(self):
        without = state_with([elem(role="button", operations=["CLICK"], accept="", multiple=False)])
        targets = build_action_space(without.elements)
        assert "UPLOAD" not in available_operations(without, targets)

        with_file = state_with([elem()])
        targets = build_action_space(with_file.elements)
        assert "UPLOAD" in available_operations(with_file, targets)
        assert targets["UPLOAD"]["1"]["accept"] == ".pdf,.jpg,.jpeg,.png"
        assert targets["UPLOAD"]["1"]["multiple"] is True
        assert targets["UPLOAD"]["1"]["hidden"] is True

    def test_upload_question_head_exists(self):
        questions, _ = build_questions(state_with([elem()]), "上传发票")
        assert "upload_target" in questions and "UPLOAD" in questions["operation"]["criteria"]

    def test_upload_answer_maps_to_the_file_element(self, monkeypatch):
        import ui_agent.decide.jev as jev_mod

        state = state_with([elem()])
        questions, _ = build_questions(state, "上传发票")
        ops = {op: 0.0 for op in questions["operation"]["criteria"]}
        ops.update({"UPLOAD": 0.8, "WAIT": 0.2})
        answer = {
            "answers": {
                "operation": {"choice": "UPLOAD", "confidence": 0.8, "probabilities": ops},
                "upload_target": {"choice": "1", "confidence": 0.9, "probabilities": {"1": 1.0}},
            },
            "model": "jev-test",
        }
        monkeypatch.setattr(jev_mod, "post_json", lambda *a, **k: answer)
        decision = JevEngine(Settings(typesafe_api_key="test")).decide(state, "上传发票")
        assert decision.operation == "UPLOAD" and decision.target_element == 1

    def test_jev_state_carries_file_facts_without_paths(self):
        state = state_with([elem(value="")])
        payload = to_jev_state(state)["elements"][0]
        assert payload["accept"] == ".pdf,.jpg,.jpeg,.png"
        assert payload["multiple"] is True and payload["hidden"] is True
        assert "rect" not in payload and "dom_index" not in payload

    def test_build_state_keeps_file_fields(self):
        raw = {"page": {"url": "http://x/"}, "fingerprint": "a", "elements": [
            {"index": 1, "role": "file", "label": UPLOAD_LABEL, "accept": ".png", "multiple": True,
             "hidden": True, "in_viewport": False, "disabled": False, "operations": ["UPLOAD"]}]}
        element = build_state(raw, [], {}, 250, 6000).elements[0]
        assert element.accept == ".png" and element.multiple and element.hidden


class TestExecutorUpload:
    def test_upload_calls_the_driver_and_reports_the_level(self, tmp_path):
        png = make_png(tmp_path)
        driver = FakeDriver([file_snapshot()])
        driver.s = settings_for(tmp_path)
        files, _ = resolve_files(elem(), {"发票文件": str(png)})
        action = Executor(driver).apply(
            Decision(operation="UPLOAD", target="1", target_element=1, confidence=0.9), files=files)
        assert action.ok and action.level == "U1" and "发票.png" in action.detail
        assert driver.uploads == [(1, ["发票.png"], "input")]

    def test_drop_mode_is_a_settings_decision_not_a_model_decision(self, tmp_path):
        png = make_png(tmp_path)
        driver = FakeDriver([file_snapshot()])
        driver.s = settings_for(tmp_path, ui_agent_upload_mode="drop")
        files, _ = resolve_files(elem(), {"发票文件": str(png)})
        action = Executor(driver).apply(
            Decision(operation="UPLOAD", target="1", target_element=1, confidence=0.9), files=files)
        assert action.ok and action.level == "U2"
        assert driver.uploads == [(1, ["发票.png"], "drop")]

    def test_upload_without_files_is_refused(self, tmp_path):
        driver = FakeDriver([file_snapshot()])
        driver.s = settings_for(tmp_path)
        action = Executor(driver).apply(
            Decision(operation="UPLOAD", target="1", target_element=1, confidence=0.9))
        assert not action.ok and "没有可用的文件" in action.detail and driver.uploads == []


def file_snapshot(fingerprint: str = "a") -> dict:
    return {"page": {"url": "http://x/", "title": "T", "text": "", "viewport": {}, "scroll": {}},
            "elements": [{"index": 1, "role": "file", "label": UPLOAD_LABEL, "accept": ".png",
                          "multiple": True, "hidden": True, "rect": [0, 0, 0, 0],
                          "in_viewport": False, "occluded_by": "", "disabled": False,
                          "operations": ["UPLOAD"]}],
            "fingerprint": fingerprint, "omitted": 0}


def make_runner(tmp_path, decisions, vars=None, **over):
    settings = settings_for(tmp_path, **over)
    driver = FakeDriver([file_snapshot("a"), file_snapshot("b"), file_snapshot("b")])
    driver.s = settings
    runner = Runner(settings, "上传发票", "http://x/", task="t", driver=driver,
                    engine=FakeEngine(decisions), verbose=False, vars=vars or {})
    return runner, driver


def upload_decision() -> Decision:
    return Decision(operation="UPLOAD", target="1", target_element=1, confidence=0.9)


class TestRunnerUpload:
    def test_trace_records_file_names_but_not_paths(self, tmp_path):
        png = make_png(tmp_path)
        runner, driver = make_runner(tmp_path, [upload_decision(), done()],
                                     vars={"发票文件": str(png)})
        result = runner.run()

        assert driver.uploads == [(1, ["发票.png"], "input")]
        line = read_trace(result.run_dir)[0]
        assert line["operation"] == "UPLOAD" and line["action"]["ok"] is True
        assert line["action"]["level"] == "U1"
        assert line["upload"] == {"source": "var", "key": "发票文件",
                                  "files": [{"name": "发票.png", "size": 9}]}
        assert str(tmp_path) not in json.dumps(line, ensure_ascii=False)
        actions = json.loads((result.run_dir / "actions.json").read_text(encoding="utf-8"))
        assert actions[0]["action"] == "UPLOAD" and "发票.png" in actions[0]["text"]

    def test_without_a_var_the_upload_is_never_attempted(self, tmp_path):
        runner, driver = make_runner(tmp_path, [upload_decision(), blocked()])
        result = runner.run()

        assert driver.uploads == []
        line = read_trace(result.run_dir)[0]
        assert line["action"]["ok"] is False and "未执行上传" in line["action"]["detail"]
        assert "upload" not in line
