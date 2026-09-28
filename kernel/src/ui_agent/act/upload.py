"""上传文件的取值与执行前校验：路径只来自测试数据（--var / 计划变量），任何模型都看不到本地磁盘。

与 TYPE_TEXT 同一套取值纪律：没有匹配的值就拒填，绝不猜一个文件出来；
能在执行前判定的错（不存在、扩展名不符、超出单/多选）在这里拒绝，不留给服务端报错。
"""

from __future__ import annotations

from pathlib import Path

from ui_agent.driver.playwright_ import FileRef, file_ref
from ui_agent.llm.textfill import match_var
from ui_agent.schema.state import Element


def resolve_files(element: Element, vars: dict[str, str] | None) -> tuple[list[FileRef], dict]:
    """命中规则与 TYPE_TEXT 一致（键名出现在控件标签里，取最长键）；多个路径用 ; 分隔。"""
    hit = match_var(vars, element.label)
    if hit is None:
        raise RuntimeError(f"没有匹配的文件路径变量（键名要出现在控件标签里：{element.label[:40]}）；未执行上传。")
    key, value = hit
    paths = [Path(part.strip()) for part in value.split(";") if part.strip()]
    if not paths:
        raise RuntimeError(f"变量 {key} 没有给出文件路径；未执行上传。")
    if len(paths) > 1 and not element.multiple:
        raise RuntimeError(f"控件只接受一个文件，变量 {key} 给了 {len(paths)} 个；未执行上传。")
    files = [file_ref(path) for path in paths]
    for item in files:
        if not item.path.is_file():
            raise RuntimeError(f"文件不存在：{item.path}；未执行上传。")
        if not accepted(item, element.accept):
            raise RuntimeError(f"文件类型不在控件 accept 声明里：{item.name}（accept={element.accept}）；未执行上传。")
    meta = {"source": "var", "key": key,
            "files": [{"name": f.name, "size": f.size} for f in files]}
    return files, meta


def accepted(item: FileRef, accept: str) -> bool:
    """accept 是逗号分隔的扩展名或 MIME（含 image/* 通配）；页面没声明就交由页面自己校验。"""
    rules = [rule.strip().lower() for rule in accept.split(",") if rule.strip()]
    if not rules:
        return True
    suffix = item.path.suffix.lower()
    for rule in rules:
        if rule == suffix or rule == item.mime:
            return True
        if rule.endswith("/*") and item.mime.startswith(rule[:-1]):
            return True
    return False
