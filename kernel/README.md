# ui_agent

自然语言驱动的 UI 测试执行器：**LLM 编排与裁决，TypeSafe Jev 决策，Playwright 执行。**

用户只给一句自然语言描述流程；LLM 把它拆成分步计划（每步一个小 goal + 独立断言），
常态循环里由 Jev 看 DOM 快照选"操作 + 目标元素"，只有 Jev 卡住时才由 LLM 介入裁决
（HINT / RECOVER / REPLAN / ABORT）。

## 快速开始

```powershell
cd D:\test_skills\ui_agent
uv sync

# 浏览器装到工作区内置目录（首次）
. .\scripts\env.ps1
uv run playwright install chromium

# 无网络冒烟：验证双模式启动与 DOM 读取
uv run python scripts\smoke_browser.py            # 无头
uv run python scripts\smoke_browser.py headed     # 可视化（会弹窗几秒）

# 离线全链路冒烟：本地夹具跑 快照 → 守卫 → 点击阶梯 → 断言（零模型调用）
uv run python scripts\smoke_snapshot.py
uv run python scripts\smoke_snapshot.py --headed  # 可视化看它怎么点

# 离线多步冒烟：脚本化决策跑一条两步计划（一个会话、零模型调用）
uv run python scripts\smoke_plan.py
uv run python scripts\smoke_plan.py --headed

# 离线监督冒烟：脚本化监督模型跑三条升级路径（HINT / RECOVER / ABORT，零模型调用）
uv run python scripts\smoke_supervisor.py
uv run python scripts\smoke_supervisor.py --headed

# 离线上传冒烟：A（隐藏 input 注入 U1）与 C（页内合成拖拽 U2）两条通道 + 一条页面拒收负例
uv run python scripts\smoke_upload.py
uv run python scripts\smoke_upload.py --headed          # 可视化看拖拽与提交
uv run python scripts\smoke_upload.py --files a.png b.pdf   # 指定上传文件（默认复用已有截图）

# 离线结构冒烟：iframe / shadow DOM / 遮挡 / 新标签 / 原生对话框（本地 HTTP 服务，零模型调用）
uv run python scripts\smoke_m3.py
uv run python scripts\smoke_m3.py --headed --only tab   # 只看"新标签跟随"那一段

# 离线断言冒烟：计数 / 属性 / 轮询重试（含一条计数负例，本地 HTTP 服务，零模型调用）
uv run python scripts\smoke_assert.py
uv run python scripts\smoke_assert.py --headed --only poll   # 只看"轮询重试"那一段

# 离线契约冒烟：case compile / snapshot --json / run --json / report --json 的子进程级验证（零模型调用）
uv run python scripts\smoke_cli.py

# 离线回放冒烟：录制 → 零模型回放（真 CLI --decider off，全命中免模型）→ 失配停机（零模型调用）
uv run python scripts\smoke_replay.py
```

## 跑一个目标

```powershell
# 只看不点：把即将喂给 Jev 的 state 打出来（零模型调用）
uv run ui-agent snapshot --url http://example.com

# 跑目标：debug 模式可视化；断言用 --check 独立指定（DONE 不算证据）
uv run ui-agent run `
  --goal-file examples\goals\baiwang_login.txt `
  --url <网址> `
  --task baiwang-login --mode debug `
  --var 账号=<账号> --var 密码=<密码> `
  --check element_exists=行政区域

# 回归（无头）：同一份代码、同一个 Chromium 二进制
uv run ui-agent run --goal-file examples\goals\baiwang_full.txt --url <网址> --mode ci `
  --check element_value=行政区域|2102 大连 `
  --check text_contains=票据类型
```

示例目标文件里**不含任何真实凭据**：账号、密码、上传文件路径一律在运行时用 `--var` 注入
（值不落盘、不进任何模型调用），所以这些文件可以安全地进版本库。

断言写法：`kind=值`、`element_value=标签|期望值`、`element_count=标签|个数`（或只给个数），
或直接给 JSON（可带 `attr` / `timeout_ms`）。可用 kind：
`text_contains / url_contains / title_contains / element_exists / element_absent / element_value /
element_count / element_attribute`。

`element_count` 数「标签包含该值的元素个数」；`element_attribute` 读元素属性
（`{"kind": "element_attribute", "value": 标签, "attr": "href", "expected": "..."}`）。
DONE 后的验收**自动轮询**：默认窗口 `UI_AGENT_ASSERT_TIMEOUT_MS`（5000ms），单条可用
`timeout_ms` 加宽；页面慢半拍不再白跑一次模型往返，trace 的 `assert` 行记第几轮才成立。

## 跑一条计划（M1）

```powershell
# 1) 一段自然语言流程 → 分步计划（这一步会调用文本模型）
uv run ui-agent plan --desc "打开系统，用账号登录，行政区域选 2102 大连，点查询，看到票据列表" `
  --url <网址> --out plans\baiwang.json

# 2) 按计划执行：一个浏览器会话跑完所有步骤，每步独立断言，失败即停
uv run ui-agent run --plan-file plans\baiwang.json --url <网址> --mode debug

# 3) 重出报告（不需要重跑）
uv run ui-agent report --run-dir .artifacts\runs\<某次运行>
```

版本库里的计划文件**不写入口地址**（免把生产域名带进公开仓库），运行时用 `--url` 给；
忘了给会直接报「计划里没有 url，请用 --url 指定入口」，不会拿空地址去跑。

`--var 键=值` 可以给 `TYPE_TEXT` 字段直接供值（优先于文本模型，值不落盘、不进模型），
也可以给 `UPLOAD` 供**上传文件路径**（键名要出现在控件标签里，多文件用 `;` 分隔）；
适合口令等不便写进目标的场景；缺值且没有文本模型时，字段会被拒填并记为失败而不是猜一个值。

## 卡住了怎么办（M2）

常态循环不调 LLM；只有下面五种情况会停下来问一次监督模型（HINT / RECOVER / REPLAN / ABORT）：

| 触发点 | 什么时候 |
|---|---|
| `blocked` | Jev 说没有任何支持的操作能推进 |
| `repeat_no_progress` | 同一个决策连做 3 次且页面没有变化 |
| `low_confidence` | 连续 3 次决策置信度低于 `UI_AGENT_CONF_MIN` |
| `budget_actions` / `budget_decisions` | 动作或决策预算耗尽 |
| `check_failed` | 第一次 DONE 没通过断言（第二次直接判失败） |

裁决由代码裁决，不由模型自己保证：元素只能用当次快照里的 index，恢复动作只允许
`CLICK / SELECT / HOVER / SCROLL_TO / PRESS_KEY / SCROLL_UP / SCROLL_DOWN / WAIT`（不含 `CLICK_FORCE`
与 `TYPE_TEXT`），SELECT 的选项必须在该元素当次选项里且未禁用，REPLAN 的步骤要过与 planner 相同的断言检查。
RECOVER 的动作走和 Jev 决策完全相同的执行路径（守卫、降级阶梯、缺陷通报都照旧）。

预算：`SUPERVISOR_PER_STEP`（默认 2，每步重置）与 `SUPERVISOR_PER_RUN`（默认 6，整轮共享），
在调用前扣除，所以升级次数有限、循环必然终止。监督模型不可用（没 key、预算用完、回答不合规）
时行为与 M0 完全一致：按 M0 语义收尾。`--supervisor off` 或 `UI_AGENT_SUPERVISOR=false` 关掉大模型总闸：
升级点直接停（断言未过不再给第二次 DONE 机会），TYPE_TEXT 字段值也只认 `--var`。
决策层另有 `--decider`（或 `UI_AGENT_DECIDER`）：`jev`=Jev 决策（默认）、`llm`=大模型代班 Jev（快路径不开思考）、
`off`=关闭决策（只认回放缓存，没有可回放的记录就按 `cache_missed` 停）。

计划模式下 REPLAN 会把**剩余步骤整体换成新序列**（原计划留在 `plan.json`，新计划写 `plan.final.json`，
重试的那一步另开 `-r2` 目录，`summary.json` 里有 `replans` 记录）；单目标模式下 REPLAN 只会停在
`replanned` 状态并提示改用计划模式——不会假装成功。

## 回放缓存（零模型回归）

同一条用例在同一环境跑过后，决策与收尾结论会按「用例哈希 + 环境地址」建键录下来：下次整步命中就回放，
**零模型调用**；命中不了才交还模型。录像按起点上下文（URL 路径 + 标题）分成变体：回放自动挑与当前
页面相符的那条（后录优先），选不中整步交还；同一用例的不同初始状态（如登录页 / 已登录页）各录一条变体。
回放不是另走一套：元素按语义重定位（role+label 精确 → 唯一标签兜底），页面上下文守卫不符即交还；
回放出的收尾结论仍要过独立断言。回放出的记录没能成立（动作执行失败 / 断言没过）时，本步剩余录像
整体作废交还模型（后面的记录都建立在前面成立的前提上）；整步走完且结论成立才把新录像并入对应变体
（自愈回填 + 防投毒：失败只淘汰回放过的那条变体，且失败那步绝不落盘）。

开关：`--cache off`（`UI_AGENT_CACHE=false`）本次运行不碰缓存；`--cache-strict`
（`UI_AGENT_CACHE_STRICT=true`）严判——命中的记录还须状态签名一致，页面悄悄变了也能看出来。
配合 `--decider off` 与 `--supervisor off` 整轮零模型：全命中直接过，失配即 `cache_missed` 停机，
并报出原因（no-entry / no-variant / wrong-context / invalid-entry / truncated / retry）。`trace.jsonl` 多一行
运行级 `kind="cache"` 事实：模式（self-finalized / agent-concluded / missed）、写入结果
（saved / kept / no-change / unconfirmed / evicted / not-written）、命中/失配与逐类步骤计数。

## 上传文件（拖拽 / 点选）

`UPLOAD` 操作的目标只能是**文件控件**（role `file`，快照里带 `accept` / `multiple` 事实）；
被 `display:none` 藏起来也算，因为真实用户入口就是那个按钮或拖拽框。走哪条通道由**配置**决定，
不由模型选，也不做跨通道静默回退：

| 通道 | 开关 | 阶梯级别 | 机制 |
|---|---|---|---|
| A：隐藏 input 注入 | `--upload-mode input`（默认）/ `UI_AGENT_UPLOAD_MODE=input` | `U1` | `set_input_files`，协议层设置 FileList |
| C：页内合成拖拽 | `--upload-mode drop` | `U2` | 页内造 `DataTransfer` + `dragenter/dragover/drop`（`isTrusted=false`，属已知限制） |

文件路径只来自 `--var`（键名要出现在控件标签里，多文件用 `;` 分隔），**没有模型兜底**：匹配不上就干净地拒绝
（"未执行上传"）而不是猜一个路径。执行前只做确定性预检（文件存在、扩展名/MIME 对得上 `accept`、单文件控件不给多个），
大小与数量上限属于**页面自己的策略**，用断言判（本地夹具与百望页面都把它们写在页面上）。

`UPLOAD` 常是两段式：先进入待提交列表，再点提交才算数——所以验收要看提交后的文案（如 `text_contains=提交成功`），
以及待提交列表里的**文件名**。产物里只落**文件名 + 大小 + 变量键名**（trace 的 `upload` 字段），本地绝对路径不落盘、不进模型。

本地夹具 `examples/local_fixture.html` 复刻了百望上传页的规则（pdf/jpg/jpeg/png、图片 ≤ 20M、PDF ≤ 10M、≤ 50 个），
`scripts/smoke_upload.py` 用真实 Chromium 跑 A、C 两条通道各一次，外加一条负例（往未声明 `accept` 的控件传 `.txt`，
页面必须自己拒收并给出文案），全程零模型调用。

## 页面结构支持（M3）：iframe / shadow DOM / 新标签 / 原生对话框

| 结构 | 行为 | 边界 |
|---|---|---|
| iframe | 快照逐帧收集（元素带 `frame` 事实）；索引 → locator 在元素所属帧内解析，动作（含 drop 上传）也在那一帧里执行 | 逐帧求值不受同源限制，跨域帧同样收 |
| shadow DOM | 穿透各级 **open** shadow root：元素进快照并标 `shadow` 事实，影子里的文本同样收集；命中测试按"文档 → 宿主 shadowRoot"逐层下钻 | `closed` 的 shadow root 从页面 JS 进不去，里面的元素模型看不见 |
| 新标签 | `context.on("page")` 登记，动作后的 settle 里跟随最新标签；当前标签被关掉就切回剩下的一个 | 只跟随，不自动关闭任何标签 |
| 原生对话框 | 挂 `page.on("dialog")`：默认 `dismiss`（不改变页面状态）；`--dialog-policy accept` 才点确定（prompt 保留页面输入的默认值）；`confirm` / `prompt` / `beforeunload` 一律记缺陷候选 | `alert` 只记事实、不算缺陷 |

不挂处理器时 Playwright 会**静默自动关掉**对话框——页面代码以为用户点了取消/确定，测试却什么都没记下来。所以这里一律显式应答并把事实落盘：
trace 每行的 `page_facts`（`{"fact": "dialog", "kind": "confirm", "message": …, "handled": "dismiss"}` 与 `{"fact": "tab", "note": …}`），
`actions.json` 里对应一行 `action="PAGE"`——它不是动作，所以不计入重复判定与动作预算，只作为模型与报告能看到的事实。

离线冒烟 `scripts/smoke_m3.py` 起一个本地 HTTP 服务跑 `examples/m3_fixture.html`（子页 `m3_child.html`），
八个用例：帧内点击、帧内 drop 上传、影子点击、被 light DOM 盖住的影子按钮判**不可达**（不许假成功）、新标签跟随、
alert（dismiss）/ confirm（dismiss 与 accept）/ prompt（accept 保留默认值）。全程零模型调用。

## 给平台/CI 用的命令行契约（P0）

外层平台只按 **argv 调 CLI + 读文件/JSON**，不 import 内核代码。带 `--json` 的子命令：
stdout 只留最后一份 JSON（人类话术走 stderr），退出码 **0=成功、1=用例失败（JSON 仍有效）、其他=内核错误**。

```powershell
# 用例 YAML → 计划：校验 + 编译（--secret-keys 给出环境已登记的密钥名，用于查未知引用）
uv run ui-agent case compile --in cases\baiwang.yaml --json --secret-keys 账号,密码
#   → {ok, errors[{line,code,message}], warnings[…], plan}；errors 非空时 plan 为 null
#   → 编译出的 plan 可直接落盘当 run --plan-file 的输入

# 一句话 → 用例草稿（这一步调用文本模型）
uv run ui-agent case draft --desc "登录后查询票据" --url <网址> --json
#   → {ok, yaml, plan, meta}；出来的 YAML 先过一遍 compile 自检，不合规就报错

# 跑（--run-dir 指定产物目录；--mode 只管产物策略，服务器用 UI_AGENT_HEADLESS=true 照样无头）
uv run ui-agent run --plan-file plan.json --run-dir .artifacts\runs\case-123 --mode ci --url <网址> --json --var 账号=<账号>

# 探针：只看不点，给平台登记环境用
uv run ui-agent snapshot --url <网址> --json
#   → {ok, summary:{final_url,title,element_count,needs_login,has_password_field}, state}
#   needs_login 取「页面上有密码输入框」这个确定性信号，不做业务推断

# 重出报告
uv run ui-agent report --run-dir .artifacts\runs\case-123 --json   # → {ok, report}
```

用例 YAML 的校验规则（错误：空 steps / 空 goal / 空 checks / 把 `${{secret.*}}` 写进 goal /
未知断言类型 / 名称含 `/\:*?"<>|` / 未登记的密钥名 / 重复键；警告：末步断言偏弱、
步骤 > 12、单步断言 > 3、变量写了字面量敏感值、硬编码 url）由 `ui_agent/case/yaml_case.py` 实现，
错误带**真实行号**（按 YAML 节点定位），未知密钥通过 `--secret-keys` 与环境的登记表比对。

`case draft` 与 `plan` 生成的 YAML **不写 url**：入口留给环境决定，同一份用例才能跑在不同环境。

## 产物

单目标运行在 `.artifacts/runs/<任务>-<时间>/` 留下：`result.json`、`trace.jsonl`、`actions.json`
（动作 + 是否改变页面）、`snapshots/*.json`（喂给模型的 state）、`screenshots/`、`report.html`。

`trace.jsonl` 是一份**分层时间线**：每行有 `seq`（文件内连续编号，行序即事件序）与 `at`（毫秒），
按 `kind` 分四层——`run_start`/`run_end`（运行边界：身份、配置、最终状态与耗时）、
`step_start`/`step_end`（计划模式的步边界，只在根 trace 有）、`decision`（一次决策往返 + 落地的动作，
保留秒级 `time` 兼容老消费方）、`assert`（DONE 验收：轮询次数 `attempts`、耗时、逐条结果）、
`model`（没产出决策的模型调用，比如响应不合规）。回放缓存另有一行运行级 `kind="cache"`：
模式、写入结果、命中/失配与逐类步骤计数（词表见 `run/trace.py` 头部注释）。

计划的运行目录多出：`plan.json`、`summary.json`（逐步结果 + `replans`）、`steps/<NN-目标>/`（每步自成一套产物）、
根 `trace.jsonl`（各步的**聚合**：决策/断言/模型行原样汇入并补记 `plan_step`，生命周期行留在各自文件）、
根 `actions.json`（同理，带 `plan_step`）、`report.html`（汇总报告，截图按相对路径引用）；发生重排时还有 `plan.final.json`。

平台的回读口径就锚在这套布局上：`summary.json` 给状态与逐步结果，根 `trace.jsonl` 给逐决策时间线
（决策行含 `url`/`title`），根 `actions.json` 给动作序列。截图只进报告、永不进模型输入；
`mode=debug` 逐步留图，`mode=ci` 只留失败那一张。
`mode` 与有无头已经解绑（`UI_AGENT_HEADLESS` 单独控制），服务器上 `--mode debug` 也不会弹窗。

## 目录

| 路径 | 内容 |
|---|---|
| `src/ui_agent/cli.py` | 命令行：`case` / `plan` / `run` / `snapshot` / `report`（`--decider`、`--supervisor`、`--cache`、`--cache-strict`、`--upload-mode`、`--dialog-policy` 可覆盖配置） |
| `src/ui_agent/case/` | 用例 YAML：按行号校验 → 编译成计划；计划 → 用例 YAML 草稿 |
| `src/ui_agent/llm/` | 三个 LLM 缝：planner 编排、textfill 字段取值、supervisor 卡住裁决 |
| `src/ui_agent/observe/` | state 组装；`js/snapshot.js` 页面内取数与索引标记；`js/guard.js` 执行前守卫 |
| `src/ui_agent/decide/` | Jev 客户端、questions 模板、提示词 |
| `src/ui_agent/driver/` | Playwright 驱动：双模式启动、点击降级阶梯、跨 frame 快照、新标签跟随、原生对话框事实 |
| `src/ui_agent/act/` | 白名单动作执行 + 守卫预检 + 降级通报；上传取值校验（`upload.py`） |
| `src/ui_agent/verify/` | 独立断言（文本/URL/元素/控件值/计数/属性 + DONE 后轮询验收），不看模型自述 |
| `src/ui_agent/run/` | 单目标主循环与多步计划执行、卡住检测、监督升级、预算、分层时间线（`trace.py`）与产物落盘 |
| `src/ui_agent/replay/` | 回放缓存：键与单键文件（`cache.py`）、语义重定位与上下文守卫（`match.py`）、录制/回放会话（`session.py`） |
| `src/ui_agent/schema/` | 契约：state / decision / check / plan / supervisor / status |
| `src/ui_agent/report/` | HTML 报告（计划模式读 `summary.json`，单目标读 `result.json`） |
| `tests/fakes.py` | 离线单测的假驱动、假决策引擎与脚本化监督模型（不起浏览器、不调模型） |
| `.browsers/` | 工作区内置浏览器（gitignore，`scripts/env.ps1` 导出路径） |
| `.artifacts/` | 持久化 profile、运行产物（gitignore） |
| `examples/goals/` | 现成的自然语言目标（百望登录、百望全流程） |
| `examples/` | 本地夹具：`local_fixture.html`（上传规则）、`m3_fixture.html` + `m3_child.html`（iframe / shadow / 新标签 / 对话框） |

## 状态

M0 完成：单目标循环可跑（state → Jev → 执行 → 断言），零模型调用即可验证快照/守卫/降级阶梯。

M1 完成：planner 拆解 + 多步计划执行（一个会话、失败即停、历史交接）+ 独立断言 + HTML 报告 + `--var` 供值。

M2 完成：卡住/阻塞/预算/断言未过时交给监督模型（HINT / RECOVER / REPLAN / ABORT），
裁决与落点由代码校验，预算有限、不可用即退回 M0 停机语义；计划模式支持整体重排。

M3 完成（本地夹具验证）：**上传**（A/C 两条通道 + 页面级验收）与**页面结构**（iframe / open shadow DOM /
新标签跟随 / 原生对话框事实化 + `--dialog-policy`）。这些能力都在 `scripts/smoke_m3.py` 与 `smoke_upload.py`
的真实 Chromium 上跑通，但**尚未在真实业务页面上实跑校准**——拿到合适的页面后再验证一遍（尤其是遮挡判定与跨域帧）。
M4（批量回归）未开始。P0（平台契约）已完成：`case compile/draft`、`run --run-dir/--json`、`snapshot --json`、
`report --json` 与产物布局（根聚合 `trace.jsonl`/`actions.json`、`screenshots/`），命令行契约有子进程级冒烟 `scripts/smoke_cli.py` 把守。
回放缓存（plan.md 第 4 项）内核侧已落地：按「用例哈希 + 环境地址」建键的录制 / 回放（语义重定位、上下文守卫、
失配交还与自愈回填）、独立严判开关、零模型停机语义，`scripts/smoke_replay.py` 在真实 Chromium 上把守。

离线单测 317 项与八个冒烟脚本（`smoke_cli` / `smoke_snapshot` / `smoke_plan` / `smoke_supervisor` / `smoke_upload` / `smoke_m3` / `smoke_assert` / `smoke_replay`）全绿。

实跑校准（2026-09-23，百望非税查询页）：`goto` 之后**不能立刻信快照**——页面 `domcontentloaded`
后先渲染壳（约 20 个导航元素），内容区 0.8–3 秒后才到，中间那段窗口"看起来稳定"却没有任何
业务控件。所以：

- 首次渲染过一道就绪门（`ready_stable_ms`，默认 2.5s 指纹不动才算渲染完），导航后先等它；
- 快照里一个元素都没有时（SPA 偶发全空白）刷新一次再看，只刷一次，之后按事实交给决策层；
- 模型的传输层错误（DNS / 连接重置 / 超时）重试，HTTP 4xx/5xx 不重试。

`SCROLL_TO` 的可选性一度与目标 head 不一致（目标 head 从来没有被组装出来，模型一选就抛错），
现在两者都由 `build_action_space` 派生，不会再分叉。

实跑记录：登录 → 行政区域"2102 大连" → 查询 的完整流程与单独的查询流程都已可视化跑通
（Jev 逐步决策，脚本断言验收；凭证只经 `--var` 注入，产物里没有明文密码）。

模型密钥：文本模型接 **DeepSeek 官方**（`https://api.deepseek.com/v1`，`TEXT_MODEL=deepseek-flash`
即 V4.1-Flash）；`.env` 里已有 key。`TEXT_MODEL_REASONING=none` 关掉思考（实测掉出
`reasoning_content`，同题 5 tokens vs 40）；想让它思考就改成别的值。
密钥只进 `.env`（已 gitignore）：单测由 `tests/conftest.py` 钉成空串，冒烟脚本显式关监督缝，
两者都不会联网计费；脚本化的 `--var` 路径也不需要 key。

