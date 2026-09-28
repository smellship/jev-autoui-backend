"""midscene 导入器：前瞻断言、同页多条断言、变量/secret 引用、末端兜底与报告。"""

from __future__ import annotations

import pytest
import yaml

from app.importers import midscene
from app.services import case_service

SAMPLE = """\
import { test, expect } from '@midscene/web';

test('发票查验-数电二手车', async ({ page }) => {
  await page.goto('http://fixture.local/invoice/check');
  await aiInput('25152000000000000001', '发票号码输入框');
  await aiTap('查验按钮');
  await expect(page.getByText('发票类型：数电票（二手车销售统一发票）')).toBeVisible();
  await expect(page.getByText('发票状态：正常')).toBeVisible();
});
"""

NO_EXPECT = """\
test('纯动作脚本', async ({ page }) => {
  await page.goto('http://fixture.local/list');
  await aiTap('查询按钮');
});
"""

CREDENTIAL = """\
test('登录', async ({ page }) => {
  await page.goto('http://fixture.local/login');
  await aiInput('Passw0rd!23', '密码输入框');
});
"""


def test_sample_steps_lookahead_and_multi_checks():
    result = midscene.convert(SAMPLE, code="sc-0001-01", owner="林赟曦", env_name="示例环境")
    doc = yaml.safe_load(result.yaml_text)
    assert doc["id"] == "sc-0001-01"
    assert doc["title"] == "发票查验-数电二手车"          # 标题取自脚本里的 test('…')
    steps = doc["steps"]
    assert len(steps) == 3
    assert steps[0]["checks"] == [{"element_exists": "发票号码"}]   # 前瞻：借下一步的输入框
    assert steps[1]["checks"] == [{"element_exists": "查验"}]
    # 同一页可以挂多条断言：两条都留下，各自独立判定
    assert steps[2]["checks"] == [{"text_contains": "发票类型：数电票（二手车销售统一发票）"},
                                  {"text_contains": "发票状态：正常"}]
    assert doc["vars"] == {"发票号码": "25152000000000000001"}
    report = result.report
    assert (report.steps, report.checks) == (3, 4)
    assert (report.explicit, report.lookahead, report.fallback) == (2, 2, 0)
    assert report.issues == []
    assert any("含冒号" in item for item in report.notes)
    assert any("脚本入口" in item for item in report.notes)


def test_sample_compiles_clean():
    result = midscene.convert(SAMPLE)
    lint = case_service.compile_yaml(result.yaml_text)
    assert lint["ok"] is True, lint["errors"]
    assert [item["code"] for item in lint["warnings"]] == []
    assert lint["plan"]["steps"][2]["checks"][0]["kind"] == "text_contains"


def test_terminal_fallback_is_flagged():
    result = midscene.convert(NO_EXPECT)
    doc = yaml.safe_load(result.yaml_text)
    assert doc["steps"][0]["checks"] == [{"element_exists": "查询"}]
    assert doc["steps"][1]["checks"] == [{"element_exists": "查询"}]   # 兜底：只证明控件还在
    assert result.report.fallback == 1
    assert any("兜底" in item and "结果断言" in item for item in result.report.notes)
    lint = case_service.compile_yaml(result.yaml_text)
    assert lint["ok"] is True, lint["errors"]
    assert "weak_final_check" in [item["code"] for item in lint["warnings"]]


def test_credentials_become_secret_refs():
    result = midscene.convert(CREDENTIAL)
    assert "Passw0rd!23" not in result.yaml_text
    assert yaml.safe_load(result.yaml_text)["vars"] == {"密码": "${{secret.密码}}"}
    assert any("secret.密码" in item for item in result.report.notes)
    assert case_service.compile_yaml(result.yaml_text)["ok"] is True


def test_unrecognized_lines_are_reported_not_swallowed():
    script = """\
test('混合', async ({ page }) => {
  await page.goto('http://fixture.local/list');
  await aiAssert('出现查询结果');
  await aiTap('查询按钮');
  await page.click('#export');
});
"""
    result = midscene.convert(script)
    joined = "\n".join(result.report.issues)
    assert "aiAssert" in joined and "page.click" in joined
    doc = yaml.safe_load(result.yaml_text)
    assert len(doc["steps"]) == 2                      # 认不出的调用没有被硬塞成步骤
    assert [item["goal"] for item in doc["steps"]] == ["打开入口页面", "点击「查询按钮」"]


def test_leading_expect_becomes_confirm_step():
    script = """\
await expect(page.getByText('欢迎回来')).toBeVisible();
await aiTap('退出');
"""
    doc = yaml.safe_load(midscene.convert(script).yaml_text)
    assert [item["goal"] for item in doc["steps"]] == ["确认页面内容", "点击「退出」"]
    assert doc["steps"][0]["checks"] == [{"text_contains": "欢迎回来"}]


def test_negative_expect_becomes_element_absent():
    script = """\
await page.goto('http://fixture.local/list');
await expect(page.getByText('加载中')).not.toBeVisible();
"""
    doc = yaml.safe_load(midscene.convert(script).yaml_text)
    assert doc["steps"][0]["checks"] == [{"element_absent": "加载中"}]


def test_keypress_and_value_guard():
    script = """\
await page.goto('http://fixture.local/search');
await aiKeyboardPress('Enter', '搜索框');
"""
    doc = yaml.safe_load(midscene.convert(script).yaml_text)
    assert doc["steps"][1]["goal"] == "按「Enter」键（焦点在「搜索框」）"
    assert doc["steps"][0]["checks"] == [{"element_exists": "搜索"}]


def test_target_token_strips_control_words():
    assert midscene.target_token("查验按钮") == "查验"
    assert midscene.target_token("发票号码输入框") == "发票号码"
    assert midscene.target_token("「提交」按钮") == "提交"
    assert midscene.target_token("查询按钮，位于页面顶部") == "查询"
    assert midscene.target_token("${label}") == ""
    assert midscene.target_token("") == ""


# --- 接口：POST /cases/{id}/imports ---

@pytest.fixture
def fresh_case(client, auth, workspace) -> str:
    """共享 workspace 里的用例会被别的测试软删除，这里每次现建一个。"""
    resp = client.post(f"/api/v1/scenarios/{workspace['scenario']['id']}/cases",
                       json={"name": "导入用例"}, headers=auth)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_api_import_returns_yaml_and_report(client, auth, fresh_case):
    resp = client.post(f"/api/v1/cases/{fresh_case}/imports",
                       json={"script_text": SAMPLE}, headers=auth)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == fresh_case
    doc = yaml.safe_load(data["yaml_text"])
    assert len(doc["steps"]) == 3
    assert doc["id"] == fresh_case
    assert doc["title"] == "导入用例"                     # 用例名不被脚本标题改掉
    assert doc["priority"] == "P1"                        # 用例的元信息原样带进 YAML
    report = data["report"]
    assert (report["steps"], report["checks"]) == (3, 4)
    assert (report["explicit"], report["lookahead"], report["fallback"]) == (2, 2, 0)
    assert report["vars"] == ["发票号码"]
    lint = client.post(f"/api/v1/cases/{fresh_case}/validate",
                       json={"yaml_text": data["yaml_text"]}, headers=auth)
    assert lint.status_code == 200 and lint.json()["ok"] is True, lint.text


def test_api_import_rejects_empty_script(client, auth, fresh_case):
    resp = client.post(f"/api/v1/cases/{fresh_case}/imports",
                       json={"script_text": ""}, headers=auth)
    assert resp.status_code == 422


def test_api_import_unknown_case_404(client, auth):
    resp = client.post("/api/v1/cases/sc-9999-99/imports",
                       json={"script_text": "aiTap('查询按钮')"}, headers=auth)
    assert resp.status_code == 404
