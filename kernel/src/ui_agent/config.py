"""集中配置：.env → Settings；浏览器目录固定在工作区内。"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BROWSERS_DIR = PROJECT_ROOT / ".browsers"
ARTIFACTS_DIR = PROJECT_ROOT / ".artifacts"

# Playwright 在启动子进程时读这个变量；在导入任何 playwright 之前固定到工作区
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BROWSERS_DIR))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    typesafe_api_key: str = ""
    typesafe_model: str = "jev-latest"
    typesafe_base_url: str = "https://api.typesafe.ai/v1/systemone"
    typesafe_auth_mode: str = "bearer"  # bearer=TypeSafe/ShowAPI 原生；appcode=阿里云云市场

    text_model_api_key: str = ""
    text_model_base_url: str = "https://api.deepseek.com/v1"
    text_model: str = "deepseek-flash"
    text_model_reasoning: str = "none"

    ui_agent_mode: str = "debug"         # 产物策略：debug=逐步截图；ci=只留失败一张（与有无头解绑）
    ui_agent_headless: bool | None = None  # 不设=按 mode 推（debug 可视化、ci 无头）；服务器上固定 true
    ui_agent_viewport: str = "1920x1080"
    ui_agent_conf_min: float = 0.55
    ui_agent_decider: str = "jev"        # 决策层：jev=Jev 决策模型；llm=大模型代班；off=关闭（没缓存可回放就停）
    ui_agent_supervisor: bool = True     # 大模型总闸：关 = 监督缝停用（升级点直接停）+ TYPE_TEXT 只认 --var
    ui_agent_profile_dir: str | None = None  # 覆盖登录态 profile 目录（平台并发时按「人+路」逐路注入）
    ui_agent_cache: bool = True          # 回放缓存：on=命中免模型 + 断言确认后录制（默认）；off=本次运行不碰缓存
    ui_agent_cache_strict: bool = False  # 严判：命中的记录还须状态签名一致，否则按失配交还
    ui_agent_cache_dir: str = ""         # 回放缓存目录；默认 .artifacts/cache，跨运行持久
    ui_agent_upload_mode: str = "input"  # input=给隐藏 input 注入文件；drop=页内合成拖拽事件
    ui_agent_dialog_policy: str = "dismiss"  # dismiss=关掉不阻塞；accept=点确定（会改变页面状态，需显式指定）

    max_actions: int = 60
    max_decisions: int = 120
    supervisor_per_step: int = 2
    supervisor_per_run: int = 6
    max_elements: int = 250
    max_text: int = 6000
    slow_mo: int = 250
    settle_ms: int = 700
    ready_stable_ms: int = 2500    # 首帧就绪门：指纹安静这么久才算渲染完成（壳先渲染、内容后到）
    ready_timeout_ms: int = 20000  # 就绪门的最长等待；超时按当前快照继续，不阻塞运行
    ui_agent_assert_timeout_ms: int = 5000  # DONE 验收轮询窗口；单条断言可用 timeout_ms 加宽

    @property
    def headless(self) -> bool:
        if self.ui_agent_headless is not None:
            return self.ui_agent_headless
        return self.ui_agent_mode != "debug"

    @property
    def debug_artifacts(self) -> bool:
        """逐步截图只在 debug 模式留；ci 只留失败那一张。"""
        return self.ui_agent_mode == "debug"

    @property
    def viewport(self) -> tuple[int, int]:
        w, h = self.ui_agent_viewport.lower().split("x")
        return int(w), int(h)

    @property
    def profile_dir(self) -> Path:
        """登录态 profile：默认工作区固定目录（本机串行跑）；设了 UI_AGENT_PROFILE_DIR 就用它。

        平台并发的 Chromium 单实例锁靠它绕开：每路一个独立目录，互不抢锁。
        """
        if self.ui_agent_profile_dir:
            return Path(self.ui_agent_profile_dir)
        return ARTIFACTS_DIR / "chrome-profile"

    @property
    def runs_dir(self) -> Path:
        return ARTIFACTS_DIR / "runs"

    @property
    def cache_dir(self) -> Path:
        """回放缓存：默认工作区 .artifacts/cache（跨运行持久）；平台可用 UI_AGENT_CACHE_DIR 指走。"""
        if self.ui_agent_cache_dir:
            return Path(self.ui_agent_cache_dir)
        return ARTIFACTS_DIR / "cache"
