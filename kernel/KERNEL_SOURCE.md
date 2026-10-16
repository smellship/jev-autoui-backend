# 内核副本来源

本目录是内核仓库的**单向同步副本**，不要在这里改代码——改动会被下一次同步覆盖。
开发在源仓库进行，部署前跑一次：`uv run python scripts/sync_kernel.py`。

- 源仓库：`D:\test_skills\ui_agent`
- 远端：https://github.com/smellship/Jev-browseruse-autoui.git
- commit：`822f0fd47b2b711c6910a5dcb840924d64738d14`
- 源仓库有未提交改动：有（副本不是源仓库某个干净提交的快照）
- 同步时间：2026-10-15T16:45:47
- 同步文件数：95

- 排除（不会进副本）：.DS_Store、.artifacts、.browsers、.env、.git、.idea、.mypy_cache、.pytest_cache、.ruff_cache、.venv、.vscode、__pycache__、build、dist；副本内同名项也不会被同步删除
