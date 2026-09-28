# ui_agent

读 README.md 再动手。三层分工：**LLM 编排/裁决，Jev 决策，Playwright 执行**。常态循环里没有 LLM。

## 硬约束

- 模型输出永不变成 selector / 坐标 / 可执行 JS；元素只能用**当次快照**分配的 index 引用。
- 快照标记 `data-uiagent-idx` 由本仓库代码生成；选择器永远只存在于 executor 内部。
- 执行前校验节点新鲜度与遮挡；浏览器 mutation 永不重试。
- 断言独立于模型自述：`DONE` 不是成功证据；没配断言只能算"未验收"。
- `TYPE_TEXT` 的值只来自文本模型或 CLI `--var`（`--var` 优先，值不落盘、不进模型）；模型输出必须解析为严格 JSON，解析失败则不输入。
- `UPLOAD` 的目标只能是文件控件（快照 role `file`，允许隐藏），**文件路径只来自 `--var`**，没有模型兜底：
  匹配不上就干净地拒绝。走 input（U1）还是 drop（U2）由设置决定，不做跨通道静默回退；
  执行器只做确定性预检（存在、扩展名/MIME、单文件控件不给多个），大小/数量上限属于页面策略，交给断言。
  产物只落文件名与大小，本地路径不落盘、不进模型。
- 计划执行失败即停：某步没通过，后续步骤记为 `skipped`，不把"前一步没成立"当成可继续的状态。
- 页面结构（M3）：快照与守卫要**穿透各级 open shadow root**，命中测试按"文档 → 宿主 shadowRoot"逐层下钻
  （直接问元素自己的 root 会看不见盖在宿主上面的 light DOM，把被遮挡判成可达）；`closed` shadow root 拿不到就放弃，不许猜。
- iframe：索引 → frame 的映射只来自当次快照（`_frames`），任何帧内求值（含 drop 上传）都必须回到元素所属的帧里做。
- 新标签：`context.on("page")` 只登记，切换放在动作后的 `settle()` 里（事件处理器内不做 Playwright 往返）；
  当前标签被关掉要切回剩下的一个。任何标签都不自动关闭。
- 原生对话框：必须显式挂 `page.on("dialog")` 并按 `UI_AGENT_DIALOG_POLICY` 应答——不挂处理器时 Playwright 会静默自动关掉，
  页面以为用户点了取消/确定而测试什么都没记录（假成功）。`confirm` / `prompt` / `beforeunload` 一律记缺陷候选。
- 页面事实（新标签、对话框）写成 `action="PAGE"` 的行为与 trace 的 `page_facts`；它不是动作，不计入重复判定与动作预算。
- 每步的动作历史（`actions.json`）只含本步动作；上一步带来的历史只在内存里喂给模型，不混进产物计数。
- 监督模型只能说 HINT / RECOVER / REPLAN / ABORT，落点由代码校验：元素必须是当次快照里的 index，
  恢复动作不许 `CLICK_FORCE`（缺陷路径）、`TYPE_TEXT` 与 `UPLOAD`（值/文件不能现编），REPLAN 的步骤过与 planner 相同的断言检查。
  监督模型不可用（没 key / 预算用完 / 回答不合规）时，行为必须与"没有监督模型"时完全一致：按 M0 语义停机。
- `RECOVER` 的动作走普通执行路径（守卫、降级阶梯、缺陷通报、trace 一个都不能少），不许旁路执行。
- 监督预算在调用前扣除（每步 `SUPERVISOR_PER_STEP`，整轮 `SUPERVISOR_PER_RUN`）；升级次数有限 ⇒ 循环必然终止。
- 单目标模式收到 REPLAN 只停在 `replanned` 状态，不假装跑完了新序列；计划模式才做整体替换。
- 密码等敏感字段：快照里只给掩码 + `secret` 标记，typed value 不进 trace / 历史。
- 禁用控件仍作为事实出现在元素表（供 WAIT 判断），但 `operations` 为空，永远不能被选中。
- 只读事实进 state：`rect`、`dom_index`、选择器一律不进模型输入（`to_jev_state` 显式构造，不做 model_dump 透传）。
- 可视化与无头必须用**同一个 Chromium 二进制**（`channel="chromium"`，绕开 headless shell），viewport 两边显式一致。
- 截图只进报告，永不进模型输入（Jev 不支持图像）。
- 点击降级阶梯只有 L0–L3 是正常路径；L4（force）与 L5（合成事件）必须写入 trace 并在报告标 ⚠（视作可用性缺陷候选）。
  真遮挡（命中测试失败）时 L4 直接跳过——force 只是跳过 Playwright 检查，事件仍被上层元素接收，假成功比失败更糟。
- 密钥只放 `.env`（已 gitignore）；测试不得调用付费 API（含 TypeSafe 与文本模型）。
- 平台契约（P0）：平台只按 **argv 调 CLI + 读文件/JSON**，永不 import 内核代码。
  带 `--json` 的子命令 stdout 只留最后一份 JSON（人类话术走 stderr），退出码 0=成功、1=用例失败（JSON 仍有效）、其他=内核错误。
  `case compile` 出错时 `plan` 必须为 `null`；`run --run-dir` 的产物布局（根 `trace.jsonl` 每行有 `step/url/title`、
  根 `actions.json`、`snapshots/`、`screenshots/`、`summary.json`/`plan.json`）是对外接口，改名即破坏平台。
  产物策略由 `UI_AGENT_MODE` 定，有无头由 `UI_AGENT_HEADLESS` 定（服务器 `--mode debug` 也要无头）。
- 中文 Windows 下一律 UTF-8 读写（`encoding="utf-8"`），否则 GBK 解码会崩。
- 未经用户要求不 commit / push。

## 检查

```bash
uv run ruff check . && uv run pytest -q          # 离线单测（不起浏览器、不联网）
uv run python scripts/smoke_cli.py               # P0 命令行契约：case compile / snapshot / run / report 的子进程级冒烟（零模型调用）
uv run python scripts/smoke_snapshot.py          # 本地夹具全链路冒烟（真实 Chromium，零模型调用）
uv run python scripts/smoke_plan.py              # 两步计划的真实浏览器冒烟（脚本化决策，零模型调用）
uv run python scripts/smoke_supervisor.py        # 监督升级的真实浏览器冒烟（脚本化监督，零模型调用）
uv run python scripts/smoke_upload.py            # 上传 A/C 两条通道 + 页面拒收负例（零模型调用）
uv run python scripts/smoke_m3.py                # iframe / shadow / 遮挡 / 新标签 / 对话框（本地 HTTP 服务，零模型调用）
```
