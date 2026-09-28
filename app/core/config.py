"""平台配置：全部可用 UI_WEB_* 环境变量覆盖（见 .env / README）。"""

from __future__ import annotations

import os
import re
import shlex
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def split_cmd(text: str) -> list[str]:
    """把命令行拆成 argv 数组。Windows 下 shlex 会把反斜杠当转义符，改用引号感知的正则切分。"""
    if os.name != "nt":
        return shlex.split(text)
    return [part.strip('"') for part in re.findall(r'"[^"]*"|\S+', text)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="UI_WEB_", env_file=".env", extra="ignore")

    # 存储
    db_url: str = "sqlite:///./ui_web.db"
    artifacts_dir: Path = Path("artifacts")
    secrets_dir: Path = Path("secrets")

    # 内核
    kernel_cmd: str = "ui-agent"
    kernel_cwd: str = ""
    kernel_timeout_s: int = 300
    kernel_run_timeout_s: int = 1800

    # 登录
    jwt_secret: str = "dev-only-change-me"
    jwt_ttl_min: int = 720
    admin_account: str = "admin"
    admin_password: str = "admin123"
    admin_name: str = "管理员"

    # 队列
    run_concurrency: int = 1

    # 保留策略（§13，全部可配）
    retention_enabled: bool = True
    retention_interval_h: int = 24
    retention_days_snapshots: int = 30
    retention_keep_snapshot_runs: int = 200
    retention_days_screenshots: int = 14
    retention_keep_debug_runs: int = 20
    retention_days_report: int = 30
    retention_total_gb: float = 10.0

    cors_origins: str = "*"

    @property
    def kernel_argv(self) -> list[str]:
        return split_cmd(self.kernel_cmd)

    @property
    def runs_dir(self) -> Path:
        return self.artifacts_dir / "runs"

    @property
    def cors_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.artifacts_dir = settings.artifacts_dir.expanduser().resolve()
    settings.secrets_dir = settings.secrets_dir.expanduser().resolve()
    return settings
