"""midscene 脚本 → 用例 YAML（离线 CLI：不调模型、不起浏览器）。

用法：
  uv run python scripts/import_midscene.py case.js                    # YAML 打到 stdout，报告走 stderr
  uv run python scripts/import_midscene.py case.js --out case.yaml    # 落文件
  uv run python scripts/import_midscene.py case.js --check            # 顺带真内核 lint（case compile --json）
  uv run python scripts/import_midscene.py case.js --json             # 机器可读（含报告与 lint）

退出码：0 正常；1 只有 --check 发现 lint 错误时；2 脚本文件不存在或内核不可用。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.errors import ApiError  # noqa: E402
from app.importers import midscene  # noqa: E402


def report_lines(report: midscene.Report) -> list[str]:
    lines = [f"步骤 {report.steps} · 断言 {report.checks} 条"
             f"（显式 {report.explicit} / 前瞻 {report.lookahead} / 兜底 {report.fallback}）"]
    if report.vars:
        lines.append("变量：" + "、".join(report.vars))
    for item in report.issues:
        lines.append("待人工：" + item)
    for item in report.notes:
        lines.append("提示：" + item)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="import-midscene",
                                     description="midscene 脚本 → 平台用例 YAML（离线）")
    parser.add_argument("script", help="midscene 脚本文件")
    parser.add_argument("--out", default="", help="写到哪里；不写就打 stdout")
    parser.add_argument("--code", default="", help="用例编号（写进 id:）")
    parser.add_argument("--title", default="", help="用例标题；默认取脚本里的 test('…')")
    parser.add_argument("--priority", default="P2")
    parser.add_argument("--owner", default="")
    parser.add_argument("--env", dest="env_name", default="")
    parser.add_argument("--tag", action="append", default=[], help="可多次：打标签")
    parser.add_argument("--keep-url", action="store_true",
                        help="把脚本的 page.goto 固定成步骤 url（默认不写，入口交给环境地址）")
    parser.add_argument("--check", action="store_true", help="用内核 lint 校验产物（case compile --json）")
    parser.add_argument("--json", action="store_true", help="stdout 只打一份 JSON")
    args = parser.parse_args(argv)

    path = Path(args.script)
    if not path.exists():
        print(f"找不到脚本文件：{path}", file=sys.stderr)
        return 2
    result = midscene.convert(path.read_text(encoding="utf-8"), code=args.code, title=args.title,
                              priority=args.priority, owner=args.owner, env_name=args.env_name,
                              tags=args.tag, keep_url=args.keep_url)
    payload = result.as_dict()
    code = 0
    if args.check:
        try:
            from app.services import case_service  # 只在这里碰平台配置：内核命令来自 UI_WEB_KERNEL_CMD

            lint = case_service.compile_yaml(result.yaml_text)
        except ApiError as exc:
            print(f"内核不可用，跳过 lint：{exc.message}（{exc.detail}）", file=sys.stderr)
            return 2
        payload["lint"] = lint
        code = 0 if lint["ok"] else 1
    if args.out:
        Path(args.out).write_text(result.yaml_text, encoding="utf-8")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return code
    if not args.out:
        print(result.yaml_text, end="" if result.yaml_text.endswith("\n") else "\n")
    else:
        print(f"已写入 {args.out}", file=sys.stderr)
    for line in report_lines(result.report):
        print(line, file=sys.stderr)
    if args.check:
        lint = payload["lint"]
        mark = "✓" if lint["ok"] else "✗"
        print(f"{mark} 内核 lint：错误 {len(lint['errors'])} · 警告 {len(lint['warnings'])}", file=sys.stderr)
        for item in lint["errors"]:
            print(f"  第 {item['line']} 行 [{item['code']}] {item['message']}", file=sys.stderr)
        for item in lint["warnings"]:
            print(f"  第 {item['line']} 行 [{item['code']}] {item['message']}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
