"""共享 HTTP 工具的离线单测：传输层错误重试、HTTP 错误语义（不联网）。"""

from __future__ import annotations

import httpx
import pytest

import ui_agent.net as net_mod
from ui_agent.net import post_json


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers: dict = {}

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    def json(self) -> dict:
        return self._payload


class StubClient:
    def __init__(self, outcomes: list):
        self.outcomes = list(outcomes)
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        item = self.outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(net_mod.time, "sleep", lambda _s: None)


def client_of(monkeypatch, outcomes: list) -> StubClient:
    stub = StubClient(outcomes)
    monkeypatch.setattr(net_mod, "CLIENT", stub)
    return stub


def test_transport_error_is_retried_then_succeeds(monkeypatch):
    stub = client_of(monkeypatch, [httpx.ConnectError("boom"), httpx.ReadTimeout("slow"),
                                   FakeResponse(200, {"ok": 1})])
    assert post_json("http://x/", "k", {}, label="决策模型") == {"ok": 1}
    assert stub.calls == 3


def test_transport_error_gives_up_after_retries(monkeypatch):
    stub = client_of(monkeypatch, [httpx.ConnectError("boom")] * 3)
    with pytest.raises(RuntimeError, match="决策模型连接失败（ConnectError）"):
        post_json("http://x/", "k", {}, label="决策模型")
    assert stub.calls == 3


def test_http_500_is_not_retried(monkeypatch):
    stub = client_of(monkeypatch, [FakeResponse(500), FakeResponse(200, {"ok": 1})])
    with pytest.raises(RuntimeError, match="HTTP 500"):
        post_json("http://x/", "k", {}, label="文本模型")
    assert stub.calls == 1


def test_rate_limit_is_retried(monkeypatch):
    stub = client_of(monkeypatch, [FakeResponse(429), FakeResponse(200, {"ok": 1})])
    assert post_json("http://x/", "k", {}, label="文本模型") == {"ok": 1}
    assert stub.calls == 2
