"""守住"离线单测"的底线：.env 里的真实密钥不得进入用例（见 AGENTS.md 的密钥约束）。"""

from __future__ import annotations

from fakes import settings_for

from ui_agent.config import Settings


def test_default_settings_in_tests_carry_no_keys(tmp_path):
    assert Settings().text_model_api_key == ""
    assert Settings().typesafe_api_key == ""
    assert settings_for(tmp_path).text_model_api_key == ""


def test_real_env_file_is_still_read_for_the_other_knobs(tmp_path):
    # 隔离只针对密钥；base_url / model 这类非敏感项继续沿用 .env，保证与线上同构
    assert Settings().text_model_base_url.startswith("http")
    assert Settings().text_model
