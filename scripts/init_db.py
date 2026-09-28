"""建表 + 种子管理员（幂等）。用法：uv run python scripts/init_db.py"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.db import init_db  # noqa: E402


def main() -> int:
    settings = get_settings()
    init_db()
    print(f"库已就绪：{settings.db_url}")
    print(f"产物目录：{settings.artifacts_dir}")
    print(f"密钥目录：{settings.secrets_dir}")
    print(f"管理员账号：{settings.admin_account}（口令来自 UI_WEB_ADMIN_PASSWORD，默认 dev 值）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
