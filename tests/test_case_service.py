"""用例服务：元信息提取、骨架、校验、草稿。"""

from __future__ import annotations

import pytest

from app.core.errors import ApiError
from app.services import case_service

GOOD = """id: sc-0001-01
title: 83票
priority: P1
tags: [冒烟]
owner: 林赟曦
env: 192.0.2.10
vars:
  账号: ${{secret.账号}}
steps:
  - goal: 登录系统
    checks:
      - element_exists: 查询
      - element_value: 行政区域|2102 大连
"""


def test_brief_extracts_meta():
    meta = case_service.brief(GOOD)
    assert meta["title"] == "83票" and meta["priority"] == "P1"
    assert meta["tags"] == ["冒烟"] and meta["steps"] == 1


def test_brief_tolerates_broken_yaml():
    assert case_service.brief("title: [未闭合") == {}


def test_skeleton_is_valid_yaml_and_passes_lint():
    text = case_service.skeleton("sc-0001-01", "冒烟用例 #1 全流程", "P1", "林赟曦", "192.0.2.10")
    meta = case_service.brief(text)
    assert meta["title"] == "冒烟用例 #1 全流程"
    result = case_service.compile_yaml(text)
    assert result["ok"] is True and len(result["plan"]["steps"]) == 1


def test_compile_maps_checks_to_plan():
    result = case_service.compile_yaml(GOOD, secret_keys=["账号"])
    assert result["ok"] and result["plan"]["name"] == "83票"
    checks = result["plan"]["steps"][0]["checks"]
    assert checks[0]["kind"] == "element_exists"
    assert checks[1] == {"kind": "element_value", "value": "行政区域", "expected": "2102 大连",
                         "role": "", "attr": "", "timeout_ms": 0}


def test_compile_reports_errors_with_line_numbers():
    bad = "id: a\ntitle: 坏\nsteps:\n  - goal: 带引用的 ${{secret.密码}}\n  - goal: 没有断言\n"
    result = case_service.compile_yaml(bad, secret_keys=[])
    assert result["ok"] is False
    codes = {err["code"] for err in result["errors"]}
    assert {"ref_in_goal", "checks_empty"} <= codes
    assert all(err["line"] >= 1 for err in result["errors"])


def test_guard_raises_422_with_detail():
    with pytest.raises(ApiError) as exc:
        case_service.guard("steps: []")
    assert exc.value.status == 422 and exc.value.code == "lint_failed"
    assert exc.value.detail["errors"]


def test_draft_returns_yaml_with_secret_refs():
    data = case_service.draft("登录后查发票并上传 PDF")
    assert data["yaml_text"].count("${{secret.") >= 2
    assert case_service.brief(data["yaml_text"])["title"]
