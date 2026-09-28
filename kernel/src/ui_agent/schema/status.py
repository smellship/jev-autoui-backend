"""运行状态的统一措辞：CLI、runner 与报告共用一份。"""

STATUS_LABELS = {
    "ok": "成功：全部断言通过",
    "done_unverified": "未验收：模型报告 DONE，但没有配置断言",
    "check_failed": "失败：断言未通过",
    "blocked": "阻塞：决策模型认为无法推进",
    "stuck": "卡住：触发升级信号且无法恢复",
    "budget": "预算耗尽",
    "aborted": "中止：监督模型判定无法继续",
    "replanned": "待重排：监督模型给出了新的步骤序列，本次运行停在原处",
    "skipped": "未执行：前序步骤未通过",
    "error": "运行错误",
}
