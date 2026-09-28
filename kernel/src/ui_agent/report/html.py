"""HTML 报告：把运行目录渲染成一个自包含单文件（截图用相对链接）。

计划模式读 `summary.json` + `steps/*/`；单目标模式读 `result.json`。
两级报告都只呈现产物里的既有事实：断言结果、trace、降级警告、缺陷候选。
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from ui_agent.schema.status import STATUS_LABELS

CSS = """
:root { color-scheme: light dark; }
body { font: 14px/1.6 "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
       margin: 24px auto; max-width: 1180px; padding: 0 16px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 28px 0 6px; padding-bottom: 4px; border-bottom: 1px solid #8884; }
h3 { font-size: 14px; margin: 16px 0 4px; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; vertical-align: 2px; }
.ok { background: #1a7f3722; color: #1a7f37; }
.warn { background: #9a670022; color: #9a6700; }
.bad { background: #cf222e22; color: #cf222e; }
.meta { color: #6e7781; font-size: 12px; }
.meta b { color: inherit; font-weight: 600; }
table { border-collapse: collapse; width: 100%; margin: 6px 0 10px; font-size: 13px; }
th, td { border: 1px solid #8883; padding: 4px 8px; text-align: left; vertical-align: top; }
th { background: #8881; font-weight: 600; white-space: nowrap; }
td.num { text-align: right; white-space: nowrap; }
td.mark { text-align: center; white-space: nowrap; }
.box { border-left: 3px solid #cf222e; background: #cf222e12; padding: 8px 12px;
       margin: 10px 0; border-radius: 0 4px 4px 0; }
.box.info { border-color: #9a6700; background: #9a670012; }
.shots { display: flex; flex-wrap: wrap; gap: 10px; }
.shots figure { margin: 0; }
.shots img { max-width: 340px; max-height: 220px; border: 1px solid #8884; border-radius: 4px; display: block; }
.shots figcaption { font-size: 11px; color: #6e7781; }
details { margin: 4px 0 12px; }
summary { cursor: pointer; font-size: 13px; color: #6e7781; }
code { font-family: Consolas, "Courier New", monospace; }
"""


def build_report(run_dir: Path, out: Path | None = None) -> Path:
    run_dir = Path(run_dir)
    out = Path(out) if out else run_dir / "report.html"
    plan = _load(run_dir / "summary.json")
    if isinstance(plan, dict) and plan.get("kind") == "plan":
        title = f"ui_agent 报告 · {plan.get('name')}"
        blocks = [_plan_header(plan)]
        for step in plan.get("steps", []):
            rel = (Path("steps") / str(step.get("dir", ""))).as_posix()
            sub = run_dir / rel
            blocks.append(_sub_run(rel, sub, f"第 {step.get('index')} 步：{step.get('goal', '')}", step))
        blocks.append(_closing(plan))
    else:
        result = _load(run_dir / "result.json")
        if not isinstance(result, dict):
            raise RuntimeError(f"{run_dir} 下既没有 summary.json（计划）也没有 result.json（单目标）")
        title = f"ui_agent 报告 · {run_dir.name}"
        blocks = [_run_header(result), _checks(result), _trace_block(run_dir),
                  _gallery("", run_dir), _closing(result)]
    out.write_text(_page(title, blocks), encoding="utf-8")
    return out


# --- 页面片段 ---

def _plan_header(plan: dict) -> str:
    rows = "".join(
        f"<tr><td class='num'>{esc(step.get('index'))}{_attempt(step)}</td><td>{esc(step.get('goal'))}</td>"
        f"<td class='mark'>{_badge(step.get('status'))}</td>"
        f"<td class='num'>{esc(step.get('decisions'))}</td><td>{esc(step.get('detail'))}</td></tr>"
        for step in plan.get("steps", [])
    )
    table = ("<h3>步骤</h3><table><tr><th>#</th><th>目标</th><th>结果</th><th>决策次数</th><th>详情</th></tr>"
             f"{rows}</table>")
    return (f"<h1>计划「{esc(plan.get('name'))}」 {_badge(plan.get('status'))}</h1>"
            f"<p>{esc(plan.get('detail'))}</p>"
            f"<p class='meta'>{_meta(plan)}</p>{_replans(plan)}{table}")


def _run_header(result: dict) -> str:
    return (f"<h1>单目标运行 {_badge(result.get('status'))}</h1>"
            f"<p>{esc(result.get('detail'))}</p>"
            f"<p class='meta'>{_meta(result)}</p>")


def _sub_run(rel: str, sub: Path, heading: str, step: dict) -> str:
    result = _load(sub / "result.json")
    result = result if isinstance(result, dict) else {}
    line = (f"<h2>{esc(heading)}{_attempt(step)} {_badge(step.get('status'))}</h2>"
            f"<p>{esc(result.get('detail', step.get('detail', '')))}</p>"
            f"<p class='meta'>{_meta(result)}</p>")
    return line + _checks(result) + _trace_block(sub) + _gallery(rel, sub)


def _attempt(step: dict) -> str:
    attempt = step.get("attempt") or 1
    return f"<span class='meta'>（第 {esc(attempt)} 次）</span>" if attempt > 1 else ""


def _replans(plan: dict) -> str:
    replans = plan.get("replans") or []
    if not replans:
        return ""
    items = "".join(
        f"<li>第 {esc(item.get('index'))} 步第 {esc(item.get('attempt'))} 次尝试后重排为 "
        f"{len(item.get('steps') or [])} 步：{'；'.join(esc(s.get('goal')) for s in item.get('steps') or [])}</li>"
        for item in replans
    )
    return f"<h3>监督重排</h3><div class='box info'><ul>{items}</ul></div>"


def _checks(result: dict) -> str:
    checks = result.get("checks") or []
    if not checks:
        return ""
    rows = "".join(
        f"<tr><td class='mark'>{'✓' if c.get('ok') else '✗'}</td><td>{esc(c.get('kind'))}</td>"
        f"<td>{esc(c.get('expected'))}</td><td>{esc(c.get('evidence'))}</td></tr>"
        for c in checks
    )
    return ("<h3>断言</h3><table><tr><th></th><th>类型</th><th>期望</th><th>证据</th></tr>"
            f"{rows}</table>")


def _trace_block(sub: Path) -> str:
    trace = _lines(sub / "trace.jsonl")
    if not trace:
        return "<p class='meta'>没有 trace.jsonl</p>"
    actions = {a.get("step"): a for a in _load(sub / "actions.json") or [] if isinstance(a, dict)}
    rows = "".join(_trace_row(row, actions.get(row.get("step"))) for row in trace)
    return ("<details open><summary>决策与动作</summary><table>"
            "<tr><th>#</th><th>操作</th><th>目标</th><th>置信</th><th>层级</th><th>结果</th><th>说明</th><th>变化</th></tr>"
            f"{rows}</table></details>")


def _trace_row(row: dict, record: dict | None) -> str:
    act = row.get("action") or {}
    op = str(row.get("operation", ""))
    target = "—"
    if row.get("target_element") is not None or row.get("target"):
        text = (record or {}).get("text") or row.get("target") or ""
        target = f"<code>{esc(row.get('target_element'))}</code> {esc(text)}"
    conf = row.get("confidence")
    conf = f"{conf:.2f}" if isinstance(conf, (int, float)) else "—"
    if act:
        mark = "✓" if act.get("ok") else "✗"
        outcome, level = f"{mark} {esc(act.get('detail'))}", esc(act.get("level"))
        changed = "↻" if (record or {}).get("page_changed") else "·"
    else:
        outcome, level, changed = "—", "", ""
    return (f"<tr><td class='num'>{esc(row.get('step'))}</td><td>{esc(op)}</td><td>{target}</td>"
            f"<td class='num'>{conf}</td><td>{level}</td><td>{outcome}</td><td>{_note(row, act)}</td>"
            f"<td class='mark'>{changed}</td></tr>")


def _note(row: dict, act: dict) -> str:
    parts = []
    signal = row.get("signal") or {}
    if signal:
        parts.append(f"<span class='bad'>信号</span> {esc(signal.get('kind'))}：{esc(signal.get('detail'))}")
    sup = row.get("supervisor") or {}
    if sup:
        ruling = sup.get("ruling")
        if ruling:
            cls = "ok" if ruling in ("HINT", "RECOVER") else "bad"
            parts.append(f"<span class='{cls}'>监督 {esc(ruling)}</span> {esc(sup.get('label'))}")
        else:
            parts.append(f"<span class='warn'>监督不可用</span> {esc(sup.get('error'))}")
    if act.get("warning"):
        parts.append(f"<span class='bad'>⚠</span> {esc(act.get('warning'))}")
    meta = row.get("text_model")
    if meta:
        parts.append(f"文本值来源：{esc(meta.get('source'))} {esc(meta.get('key') or meta.get('model') or '')}")
    return "<br>".join(parts)


def _gallery(rel: str, sub: Path) -> str:
    shots = sorted((sub / "screenshots").glob("*.png")) if (sub / "screenshots").is_dir() else []
    if not shots:
        return ""
    figs = "".join(
        f"<figure><img src=\"{esc((Path(rel) / 'screenshots' / p.name).as_posix())}\" alt=\"{esc(p.name)}\">"
        f"<figcaption>{esc(p.name)}</figcaption></figure>" for p in shots
    )
    return f"<h3>截图</h3><div class='shots'>{figs}</div>"


def _defects(data: dict) -> str:
    items = data.get("defect_candidates") or []
    if not items:
        return ""
    lis = "".join(f"<li>{esc(item)}</li>" for item in items)
    return f"<h3>可用性缺陷候选</h3><div class='box'><ul>{lis}</ul></div>"


def _closing(data: dict) -> str:
    blocks = [_defects(data)]
    note = data.get("zoom_note")
    if note:
        blocks.append(f"<div class='box info'>缩放提示：{esc(note)}</div>")
    if data.get("notes"):
        blocks.append(f"<p class='meta'>计划备注：{esc(data.get('notes'))}</p>")
    return "".join(blocks)


# --- 小工具 ---

def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _badge(status) -> str:
    status = str(status or "")
    return f"<span class='badge {_status_class(status)}'>{esc(STATUS_LABELS.get(status, status))}</span>"


def _status_class(status: str) -> str:
    if status == "ok":
        return "ok"
    if status in ("done_unverified", "skipped", "budget", "replanned"):
        return "warn"
    return "bad"


def _meta(data: dict) -> str:
    parts = []
    if data.get("url"):
        parts.append(f"<b>入口</b> {esc(data.get('url'))}")
    if data.get("mode"):
        policy = "逐步截图" if data.get("mode") == "debug" else "只留失败截图"
        parts.append(f"<b>模式</b> {esc(data.get('mode'))}（{policy}）")
    if data.get("headless") is not None:
        parts.append(f"<b>浏览器</b> {'无头' if data.get('headless') else '可视化'}")
    if data.get("viewport"):
        parts.append(f"<b>视口</b> {esc(data.get('viewport'))}")
    if data.get("duration_s") is not None:
        parts.append(f"<b>耗时</b> {esc(data.get('duration_s'))}s")
    if data.get("steps") is not None:
        parts.append(f"<b>决策</b> {esc(data.get('steps'))}")
    if data.get("decision_model"):
        parts.append(f"<b>决策模型</b> {esc(data.get('decision_model'))}")
    if data.get("text_model"):
        parts.append(f"<b>文本模型</b> {esc(data.get('text_model'))}")
    if data.get("vars"):
        parts.append(f"<b>变量键</b> {esc('、'.join(data.get('vars')))}")
    if data.get("started"):
        parts.append(f"<b>开始</b> {esc(data.get('started'))}")
    return " · ".join(parts)


def _name(run_dir: Path) -> str:
    return run_dir.name


def _load(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _lines(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _page(title: str, blocks: list[str]) -> str:
    return ('<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>{esc(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n"
            + "\n".join(b for b in blocks if b) + "\n</body>\n</html>\n")
