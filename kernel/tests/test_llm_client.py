"""文本模型缝的离线单测：思考开关映射、缺 key、400 回退、严格 JSON——不联网。"""

from __future__ import annotations

import json

import pytest

from ui_agent.config import Settings
from ui_agent.llm import client as client_mod
from ui_agent.llm.client import chat_json, reasoning_options
from ui_agent.llm.textfill import field_context, resolve_text
from ui_agent.schema.state import Element, PageInfo, State


def settings(**over) -> Settings:
    base = {"text_model_api_key": "k", "text_model_base_url": "https://api.deepseek.com/v1",
            "text_model": "deepseek-chat", "text_model_reasoning": "none"}
    return Settings(**{**base, **over})


def reply(content: str, usage: dict | None = None) -> dict:
    return {"choices": [{"message": {"content": content}}], "usage": usage or {"total_tokens": 7}}


class TestReasoningOptions:
    def test_none_mode_maps_per_provider(self):
        assert reasoning_options("https://dashscope.aliyuncs.com/compatible-mode/v1", "none") == \
            {"reasoning": {"enabled": False}}
        assert reasoning_options("https://api.deepseek.com/v1", "none") == {"thinking": {"type": "disabled"}}

    def test_other_modes_send_nothing(self):
        assert reasoning_options("https://api.deepseek.com/v1", "low") == {}


def test_json_mode_prompts_contain_the_word_json():
    """json_object 模式要求消息里出现 "json" 一词，否则网关 400；各缝 prompt 都必须带上。"""
    from ui_agent.decide import prompts

    for name in ("DELEGATE", "THINKING_DELEGATE", "PLANNER", "SUPERVISOR", "TEXT_VALUE"):
        assert "json" in getattr(prompts, name).lower(), name


class TestChatJson:
    def test_requires_key(self):
        with pytest.raises(RuntimeError, match="TEXT_MODEL_API_KEY"):
            chat_json(settings(text_model_api_key=""), "sys", {"a": 1})

    def test_parses_json_object(self, monkeypatch):
        monkeypatch.setattr(client_mod, "post_json", lambda *a, **k: reply('{"text": "sysadmin"}'))
        data, meta = chat_json(settings(), "sys", {"goal": "登录"})
        assert data == {"text": "sysadmin"}
        assert meta["model"] == "deepseek-chat" and meta["reasoning"] == "none"

    def test_repairs_extra_closing_brace(self, monkeypatch):
        """模型偶尔多生成一个 }，修复后应能正常解析。"""
        monkeypatch.setattr(client_mod, "post_json", lambda *a, **k: reply('{"answers": {"op": "CLICK"}}}'))
        data, meta = chat_json(settings(), "sys", {"goal": "点击"})
        assert data == {"answers": {"op": "CLICK"}}

    def test_repairs_multiple_extra_braces(self, monkeypatch):
        """多个多余括号也能修。"""
        monkeypatch.setattr(client_mod, "post_json", lambda *a, **k: reply('{"x": 1}}}'))
        data, meta = chat_json(settings(), "sys", {"goal": "测试"})
        assert data == {"x": 1}

    def test_repairs_truncated_string_tail(self, monkeypatch):
        """尾部被截断（字符串没收尾、括号没闭合）——真实监督模型出过的形态，补全后应能解析。"""
        content = ('{"ruling": "RECOVER", "action": {"operation": "SCROLL_TO", "element": 79}, '
                   '"reason": "元素79不可见，需先滚动到它才可点击。')
        monkeypatch.setattr(client_mod, "post_json", lambda *a, **k: reply(content))
        data, meta = chat_json(settings(), "sys", {"a": 1})
        assert data["ruling"] == "RECOVER" and data["action"]["element"] == 79
        assert data["reason"].endswith("可点击。")

    def test_closes_truncated_lists_and_objects(self):
        assert client_mod._try_repair_json('[{"goal": "打开登录页"}, {"goal": "输账号') == \
            [{"goal": "打开登录页"}, {"goal": "输账号"}]
        assert client_mod._try_repair_json('{"a": "b"') == {"a": "b"}
        assert client_mod._try_repair_json('{"a": 1,}') is None  # 尾部逗号不是截断形态

    def test_truncated_mid_number_is_not_repaired(self, monkeypatch):
        """截断点落在数字中间（7 可能是 79 被切）——拒绝修，宁可报错也不猜值。"""
        assert client_mod._try_repair_json('{"element": 79') is None
        monkeypatch.setattr(client_mod, "post_json", lambda *a, **k: reply('{"element": 79'))
        with pytest.raises(RuntimeError, match="未返回 JSON"):
            chat_json(settings(), "sys", {"a": 1})

    def test_drops_reasoning_field_when_gateway_rejects_it(self, monkeypatch):
        bodies: list[dict] = []

        def fake_post(url, key, body, **kwargs):
            bodies.append(dict(body))
            if len(bodies) == 1:
                raise RuntimeError("文本模型返回 HTTP 400；未执行任何动作。")
            return reply('{"text": "ok"}')

        monkeypatch.setattr(client_mod, "post_json", fake_post)
        data, meta = chat_json(settings(), "sys", {"a": 1})
        assert data == {"text": "ok"}
        assert "thinking" in bodies[0] and "thinking" not in bodies[1]
        assert "回退" in meta["reasoning"] or "去掉" in meta["reasoning"]

    def test_other_errors_are_not_retried(self, monkeypatch):
        calls = []

        def fake_post(*a, **k):
            calls.append(1)
            raise RuntimeError("文本模型返回 HTTP 500；未执行任何动作。")

        monkeypatch.setattr(client_mod, "post_json", fake_post)
        with pytest.raises(RuntimeError, match="500"):
            chat_json(settings(), "sys", {"a": 1})
        assert len(calls) == 1

    @pytest.mark.parametrize("content", ["not json", "[1,2]", "null"])
    def test_rejects_non_object_or_broken_json(self, monkeypatch, content):
        # 对象本身的合法性由 client 负责；{...} 里有没有 text 由 textfill 负责（见 TestTextFill）
        monkeypatch.setattr(client_mod, "post_json", lambda *a, **k: reply(content))
        with pytest.raises(RuntimeError):
            chat_json(settings(), "sys", {"a": 1})


def state() -> State:
    return State(page=PageInfo(url="http://x/", title="T", text="请输入帐户名"),
                 elements=[Element(index=1, role="textbox", label="账号", operations=["CLICK", "TYPE_TEXT"])])


class TestTextFill:
    def test_returns_value_and_meta(self, monkeypatch):
        monkeypatch.setattr("ui_agent.llm.textfill.chat_json",
                            lambda *a, **k: ({"text": "sysadmin"}, {"model": "m"}))
        value, meta = resolve_text(settings(), "登录", state().elements[0], state())
        assert value == "sysadmin"
        assert meta == {"source": "text_model", "model": "m"}

    def test_var_beats_text_model(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("指定了 --var 就不该调用文本模型")

        monkeypatch.setattr("ui_agent.llm.textfill.chat_json", boom)
        value, meta = resolve_text(settings(), "登录", state().elements[0], state(),
                                   vars={"账号": "sysadmin"})
        assert value == "sysadmin" and meta["source"] == "var" and meta["key"] == "账号"

    def test_no_matching_var_falls_back_to_model(self, monkeypatch):
        monkeypatch.setattr("ui_agent.llm.textfill.chat_json",
                            lambda *a, **k: ({"text": "from-model"}, {"model": "m"}))
        value, meta = resolve_text(settings(), "登录", state().elements[0], state(),
                                   vars={"验证码": "1234"})
        assert value == "from-model" and meta["source"] == "text_model"

    def test_unusable_payload_names_the_unmatched_vars(self, monkeypatch):
        """键名对不上控件标签时，报错要能把「哪个键、哪个标签」摆出来。"""
        monkeypatch.setattr("ui_agent.llm.textfill.chat_json", lambda *a, **k: ({"text": None}, {}))
        element = Element(index=3, role="textbox", label="请输入帐户名", operations=["CLICK", "TYPE_TEXT"])
        page_state = State(page=PageInfo(url="http://x/", title="T", text="登录"), elements=[element])
        with pytest.raises(RuntimeError, match="未给出可用的字段值") as excinfo:
            resolve_text(settings(), "登录", element, page_state, vars={"账号": "u", "密码": "p"})
        message = str(excinfo.value)
        assert "账号" in message and "请输入帐户名" in message

    @pytest.mark.parametrize("payload", [{"text": None}, {"text": ""}, {"text": "x" * 2001}, {"text": 5},
                                         {"text": "ok", "extra": 1}])
    def test_rejects_unusable_payloads(self, monkeypatch, payload):
        monkeypatch.setattr("ui_agent.llm.textfill.chat_json", lambda *a, **k: (payload, {}))
        with pytest.raises(RuntimeError, match="未给出可用的字段值"):
            resolve_text(settings(), "登录", state().elements[0], state())

    def test_context_has_no_geometry_and_is_json_serialisable(self):
        context = field_context("登录", state().elements[0], state())
        assert context["field"]["label"] == "账号"
        assert "rect" not in json.dumps(context)
        assert json.loads(json.dumps(context, ensure_ascii=False))["page"]["title"] == "T"
