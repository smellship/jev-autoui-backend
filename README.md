# ui_backend — UI 测试平台后端

用例 / 环境 / 运行台账 + 调 `ui_agent` 内核（REST）。按 `ui测试开发参考文档/UI测试平台_spec.md` 实现平台层：
**平台管状态（SQLite/MySQL + 磁盘），内核只做"文件进 → 文件/JSON 出"**；后端不 import 内核代码，
一律以 argv 数组调 `ui-agent` CLI（不过 shell）。

## 快速开始

```bash
uv sync                        # 装依赖（平台 + 内核副本，同一个 venv）
cp .env.example .env           # 本地默认 SQLite；内核默认指向 kernel/ 副本
uv run python scripts/init_db.py
uv run uvicorn app.main:app --reload --port 8770
```

- 接口文档：`http://127.0.0.1:8770/docs`；健康检查：`GET /api/v1/health`
- 默认管理员 `admin / admin123`（生产用 `UI_WEB_ADMIN_PASSWORD` 覆盖）
- 内核命令默认就是同一个 venv 里的 `ui-agent`（指向 `kernel/` 副本，P0 契约已就位）。
  只想跑平台、不碰内核/浏览器时，把 `UI_WEB_KERNEL_CMD` 改成桩 `scripts/fake_kernel.py`
  （`.env.example` 里有现成的注释行），单测与 `smoke_api.py` 默认走的就是桩。

## 整体部署（内核 + 平台 = 一份目录）

`kernel/` 是内核仓库 `ui_agent` 的**单向同步副本**（`kernel/KERNEL_SOURCE.md` 记录来源与 commit），
和平台装进**同一个 venv**、随目录整体上传服务器——但平台仍只以 argv 调它的 CLI，**不 import 内核代码**。

```
ui_backend/
├─ app/ tests/ scripts/      平台（FastAPI + SQLAlchemy）
├─ kernel/                   内核副本（勿在此改代码；源仓库改完用同步脚本刷新）
├─ .env                      平台密钥：DB / JWT / 管理员口令
└─ kernel/.env               内核密钥：决策模型与文本模型 API Key（服务器上单独填，永不入副本）
```

服务器上六步：

```bash
uv sync                                                  # ① 一次装全（平台 + 内核依赖，含 playwright）
cp .env.example .env                                     # ② 平台配置：DB / JWT / 管理员口令
cp kernel/.env.example kernel/.env                       # ③ 内核密钥：模型 API Key
PLAYWRIGHT_BROWSERS_PATH="$PWD/kernel/.browsers" uv run python -m playwright install chromium   # ④ 浏览器
uv run python scripts/init_db.py                         # ⑤ 建表 + 种子管理员
uv run uvicorn app.main:app --host 0.0.0.0 --port 8770   # ⑥ 起服务
```

- Windows 下 ④ 写成 `$env:PLAYWRIGHT_BROWSERS_PATH="$PWD/kernel/.browsers"; uv run python -m playwright install chromium`；
  Linux 首次装系统依赖可加 `--with-deps`（要 root）。装了全局 `PLAYWRIGHT_BROWSERS_PATH` 的话，要和 `kernel/.browsers` 一致。
- 装不动（内网/离线）：从已装好的机器把整个 `.browsers/` 拷到 `kernel/.browsers` 即可（版本号目录名要一致，如 `chromium-1243`）；
  报 `Executable doesn't exist at …kernel\.browsers\…` 就是这一步没做，或 playwright 升级后版本号对不上。
- 服务器没有桌面：起服务前设 `UI_AGENT_HEADLESS=true`（随内核子进程透传），`--mode debug` 也只留截图不弹窗；
  无桌面的 Linux 上不设它，有头浏览器起不来。
- 内核的浏览器/产物目录按"包所在工程根"推导，副本放进 `kernel/` 后 `.browsers/`、`.artifacts/` 自动落在
  `ui_backend/kernel/` 下，整体自洽，不需要改内核代码。`.browsers/` 与 `kernel/.env` 都在 gitignore，
  `sync_kernel.py` 同步时也不动它们。
- 部署前同步内核（开发在源仓库 `../ui_agent` 进行）：

```bash
uv run python scripts/sync_kernel.py            # 单向：源仓库 → kernel/，排除 .git/.venv/.browsers/.artifacts/.env/缓存
uv run python scripts/sync_kernel.py --dry-run  # 只看会动哪些文件
```

## 怎么自测

```bash
uv run ruff check .            # 静态检查
uv run pytest -q               # 单测：TestClient + 内核桩 + 保留策略（独立 SQLite，互不干扰）
uv run pytest kernel/tests -q  # 内核自带的离线单测（零模型、零浏览器）
uv run python scripts/smoke_api.py         # 接口冒烟：真起 uvicorn + 真打 HTTP（内核桩），数据落 .smoke/
uv run python scripts/smoke_kernel_real.py # 真内核联调：UI_WEB_KERNEL_CMD 指向 kernel/ 副本，数据落 .smoke-real/

# 单测/冒烟也可以跑在外部库（如本机 MySQL）：测试库每次开跑前会被清空重建，口令只在本机用
UI_WEB_TEST_DB_URL="mysql+pymysql://root:<本地口令>@127.0.0.1:3306/ui_web_test?charset=utf8mb4" uv run pytest -q
SMOKE_DB_URL="mysql+pymysql://root:<本地口令>@127.0.0.1:3306/ui_web_smoke?charset=utf8mb4" uv run python scripts/smoke_api.py
```

冒烟覆盖主流程 24 项：健康检查 / 登录鉴权 / 用户管理（自助改密 + 管理员围墙）/ 分类树 /
环境与密钥（只写不读）/ 模型 API Key（只写不读 + 注入内核子进程）/ 探针与页清单 /
用例校验（错误码+行号）/ 保存与乐观锁 409 / TestPlan 预览 /
自然语言草稿 / 运行（debug·ci·失败·停止）/ SSE 实时事件 / 产物与报告（含路径穿越）/ 运行台账筛选 /
版本回滚 / 软删除 / 场景管理（套件 + 批次串跑，失败继续）/ 批次停止（aborted / 再停 409）/
审计落库 + 全目录密钥明文扫描。

`smoke_kernel_real.py` 是**不调模型**的真内核联调（没有 API key 也能跑）：起真 uvicorn 指向 `kernel/` 副本，
打本地夹具页做探针、跑一条入口连不上的用例（内核退出码 1 也要被平台正确回读成 `error`）、
再用真 `report` 重出报告，最后扫一遍密钥明文。
它需要浏览器目录：脚本默认认 `PLAYWRIGHT_BROWSERS_PATH`，没有就按 README 第 ④ 步装到 `kernel/.browsers`。

## 老脚本导入（midscene → 用例 YAML）

`app/importers/midscene.py` + `scripts/import_midscene.py`：把老平台那种 midscene 脚本静态转成用例
（不调模型、不起浏览器），产物直接粘进平台用例编辑器保存即可。三个入口共用同一份转换逻辑：

- 前端：用例详情页「导入老脚本」→ `POST /api/v1/cases/{id}/imports`（`{script_text, keep_url}`，返回 YAML + 导入报告，不落库；前端把 YAML 灌进编辑器、报告显示在编辑器上方）；
- 接口直接调：请求体同上，用例自身元信息（code/title/优先级/负责人/环境）自动带进产物；
- CLI（离线/批处理）：

```bash
uv run python scripts/import_midscene.py case.js                   # YAML 打 stdout，报告走 stderr
uv run python scripts/import_midscene.py case.js --out case.yaml   # 落文件
uv run python scripts/import_midscene.py case.js --check           # 顺带用内核 lint（case compile --json）
```

转换规则（"老脚本没有断言也能跑"的落地）：

| 脚本里 | 用例里 |
|---|---|
| `page.goto(url)` · `aiInput(值, 目标)` · `aiTap(目标)` · `aiHover(目标)` · `aiKeyboardPress(键, 目标)` | 一步一个动作，goal 是目标描述 |
| `expect(page.getByText('X')).toBeVisible()` | `text_contains: X`，挂到它前面那一步（同一步可多条） |
| `expect(...).not.toBeVisible()` | `element_absent: X` |
| 输入的值 | 进 `vars`（键取目标核心词：「发票号码输入框」→ 发票号码）；像凭据的改写成 `${{secret.键}}`，字面量不写进用例 |
| 步骤没有断言 | **前瞻**：用下一步要用的元素补 `element_exists`；借不到就用自身控件兜底，并在报告里点明 |
| 最后一步只有 `element_exists` | 报告提示「只证明控件还在，结果对不对没验证」（内核也会报 `weak_final_check`） |

认不出的行（`aiAssert` / `aiScroll` / 页面上原生的 `page.click('#id')` 等）**不静默丢**：进报告「待人工」，
按提示补成目标描述。两条已知坑也写进报告：

- 断言含「：」（如 `发票类型：数电票…`）：页面文本按**文本节点换行**拼接，标签与值分属两个节点时整串会匹配不到；运行失败就把断言改成冒号后那段；
- `page.goto` 的地址默认**不写进用例**（入口由环境地址决定）；要固定就加 `--keep-url`。

## 与内核的契约（P0 已就位）

| 平台动作 | 内核命令 |
|---|---|
| 校验 + 编译预览 | `case compile --in <yaml> --json [--secret-keys a,b]` |
| 自然语言 → YAML 草稿 | `case draft --desc … --url … --json` |
| 运行 | `run --plan-file … --run-dir <dir> --mode debug\|ci --url … --json [--var k=v]` |
| 探针 | `snapshot --url … --json`（`summary.{final_url,title,element_count,needs_login,has_password_field}`） |
| 重出报告 | `report --run-dir … --json` |

约定（内核侧 README「给平台/CI 用的命令行契约」有完整版）：带 `--json` 时 stdout 只留一份 JSON、
人类话术走 stderr；退出码 `0`=成功、`1`=用例失败（JSON 仍有效）、其他=内核错误，平台按 `(0, 1)` 回读。
运行目录根部的聚合 `trace.jsonl`（每行带 `step/url/title`，另加 `plan_step`）与 `actions.json` 是平台 SSE、
页清单与「决策时间线」的数据源；debug/ci 只管"逐步截图 / 只留失败一张"，与有无头解绑
（服务器侧用 `UI_AGENT_HEADLESS=true`，`--mode debug` 也不会弹窗）。

内核侧 lint 错误码（平台逐行标红用的）：`steps_empty / goal_empty / checks_empty / ref_in_goal / bad_kind /
bad_check / empty_check / bad_name / unknown_secret / duplicate_key / bad_vars / bad_step / bad_root /
yaml_error / empty_file`，警告码：`weak_final_check / too_many_checks / too_many_steps / literal_secret / hardcoded_url`。
`scripts/fake_kernel.py` 与它保持一致，改内核的码名要同步改桩与单测。

`UI_WEB_KERNEL_CMD` 默认 `ui-agent`，就是同一个 venv 里、指向 `kernel/` 副本的控制台脚本；
要临时改指别处内核（比如源仓库自己的 venv），只改这一项即可，例如
`UI_WEB_KERNEL_CMD="D:/test_skills/ui_agent/.venv/Scripts/ui-agent.exe"`。

## 目录结构

```
app/
├─ main.py                 FastAPI 装配 + /api/v1/health + 静态托管（frontend/dist 存在时）
├─ db.py                   引擎 / 会话 / 建表 / 编号序列 / 种子管理员
├─ models/tables.py        数据表（spec §5.1）
├─ schemas.py              请求体模型
├─ core/                   config（UI_WEB_*）· errors（统一错误体）· security（PBKDF2+JWT）· paths（白名单）
├─ services/
│   ├─ kernel_client.py    内核子进程：argv 数组、JSON 解析、日志落盘、进程树终止
│   ├─ secret_store.py     secrets/<env>.local.yaml（只写不读、${{secret.X}}/${{env.X}} 解析）
│   ├─ model_keys.py       模型 API Key：secrets/_model_keys.local.yaml（只写不读、注入内核子进程）
│   ├─ case_service.py     YAML ↔ TestPlan：校验 / 保存版本 / 回滚 / 草稿
│   ├─ suite_service.py    串跑套件（场景管理）：CRUD / 有序清单 / 引用校验 / 存清单
│   ├─ run_service.py      串行队列 + 状态映射 + 页清单登记 + 停止 / 重出报告 + 批次（同 batch_code 按序入队 / 停止批次）
│   ├─ retention.py        保留策略（§13）
│   └─ audit.py            审计
├─ importers/
│   └─ midscene.py         老脚本（midscene）→ 用例 YAML：前瞻断言 / vars / 报告（离线）
└─ api/v1/                 auth · nodes · scenarios · cases · suites · batches · envs · runs（含 SSE）· users · settings
kernel/                    内核副本（源仓库 ui_agent 的单向同步，见 KERNEL_SOURCE.md）
scripts/                   sync_kernel.py（同步内核）· fake_kernel.py（内核桩）· init_db.py · import_midscene.py（脚本导入）· smoke_api.py · smoke_kernel_real.py
tests/                     conftest（临时库+桩）· kernel_client · core/secrets · case_service · midscene 导入 · api_flow · model_keys · suite/batch · retention
```

## 主要接口

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/auth/login` · GET `/auth/me` | 登录换 token / 当前用户 |
| PATCH | `/auth/me` · PUT `/auth/password` | 改自己的昵称 / 改自己的密码（要核对原密码，401 `bad_password`） |
| GET/POST | `/users` · PATCH `/users/{id}` · POST `/users/{id}/password` | 用户管理（**仅管理员**，否则 403）：列表 / 新建 / 改名·停用·设管理员（不能动自己，409 `self_guard`）/ 重置密码 |
| GET/PUT | `/settings/model-keys` | 模型 API Key（**仅管理员**，否则 403）：状态（键名+已设置+来源） / 写入（`None`=不改、空串=清除；值只写不读，落 `secrets/_model_keys.local.yaml`） |
| GET/POST | `/nodes` · PATCH/DELETE `/nodes/{id}` | 分类树 |
| GET/POST | `/scenarios` · GET/PATCH/DELETE `/scenarios/{id}` | 场景 |
| GET/POST | `/scenarios/{id}/cases` | 场景下用例列表 / 新建 |
| GET | `/cases` | 全局用例池（场景管理选择器）：`node_id` / `scenario_id` / `q` 过滤，跨场景 |
| GET/PUT | `/cases/{id}` | 详情 / 保存（乐观锁 409、校验 422、成功留版本） |
| POST | `/cases/{id}/validate` · `/drafts` · `/imports` | 校验+TestPlan / 自然语言草稿 / 老脚本（midscene.js）导入（都不落库） |
| GET/POST | `/cases/{id}/versions…` · `/rollback` · `/copy` | 版本 / 回滚（前滚）/ 复制 |
| GET/POST/PATCH/DELETE | `/envs…` | 环境；`PUT /envs/{id}/secrets` 只写不读、`DELETE /envs/{id}/secrets/{key}` 删键；`POST /envs/{id}/probe` 探针；`GET /envs/{id}/pages` 页清单 |
| GET/POST | `/suites` · GET/PATCH/DELETE `/suites/{id}` | 串跑套件（场景管理）：列表（附最近批次）/ 新建 / 详情（含已删用例引用 `missing`）/ 改名 / 删除 |
| POST | `/suites/{id}/copy` · PUT `/suites/{id}/cases` · POST `/suites/{id}/run` | 复制 / 存有序清单（含已删引用则 409）/ 起批次（202，复用顶栏 env/mode） |
| POST/GET | `/runs` | 发起（202 入队）/ 台账（case_id、env_id、status、mode、batch_code、时间筛选） |
| GET | `/runs/{id}` · `/runs/{id}/events`(SSE) · `/runs/{id}/artifacts/{path}` · `/runs/{id}/report` | 详情 / 实时事件 / 产物（白名单）/ 报告 |
| POST | `/runs/{id}/stop` · `/runs/{id}/report` | 停止 / 重出报告 |
| POST | `/batches` | 临时串跑（直接传有序 `case_ids`，202），等价于一次性套件 |
| GET/POST | `/batches/{code}` · `/batches/{code}/stop` | 批次聚合进度（items + counts + done）/ 停止批次（停当前 + 未跑标记中止，终态再停 409） |

错误体统一为 `{"code", "message", "detail"}`；列表分页 `?page=&size=`。

## 安全边界（与内核一致）

- 密钥只存 `secrets/<env>.local.yaml`（gitignore、写时 chmod 600）；接口**只写不读**，响应只回键名与"已设置"；
- 模型 API Key（`TEXT_MODEL_API_KEY` / `TYPESAFE_API_KEY`）同一不变式：只存 `secrets/_model_keys.local.yaml`，
  设置页只写不读；每次起内核子进程时注入环境变量（存储值优先于服务器进程 env，内核 pydantic-settings 直接读），
  即改即生效、不用重启；
- 启动日志里的库地址带脱敏（`mysql+pymysql://user:***@host/db`）；冒烟的明文扫描除了用例/环境密钥、模型 Key，
  还会把外部库口令一起盯上；
- 真值只在运行前解析成内核 `--var` 参数，**不进模型、不进产物、不进日志**（冒烟会全目录扫描明文）；
- 模型输出永不变成 selector / 坐标 / 可执行 JS；产物访问一律经规范化前缀校验（路径穿越 400）；
- YAML 用 `safe_load`；按钮 = 固定命令 + 白名单参数，无"执行任意命令"接口。

## 待办 / 与 spec 的偏差

- **前端在 `../ui_frontend`**（Vue3 + Element Plus，dev 时 `npm run dev` 走 `/api` 代理；`frontend/dist` 一旦存在会被自动托管）；
  内核改动在源仓库 `../ui_agent` 做完后，用 `scripts/sync_kernel.py` 刷到 `kernel/`（副本勿手改）；
- **Alembic 迁移暂缓**：本地 SQLite 直接用 `create_all`；接 MySQL 前补迁移脚本（`user.is_admin` 这类新列由
  `db.py` 里的 `_patch_user_table()` 在启动时 `ALTER TABLE` 补齐；`run.batch_code` / `run.suite_id` 由
  `_patch_run_table()` 同法补列 + 建索引，批次聚合直接按 `Run.batch_code` 派生，无独立批次表）；
- 运行记录导出 CSV 暂未实现（spec §8 的按钮已在用例/运行两级打通；场景级"整场景串跑"已由
  「场景管理」套件 + 批次取代，旧入口已移除）；
- 内核的真实模型路径（`case draft`、带决策模型的运行）要有 API Key 才能实跑：管理员在设置页
  「模型 API Key」里填两把 Key 即可（写入后对之后的内核调用生效）；或者沿用内核自己的 `.env` / `kernel/.env`。
  平台冒烟仍刻意避开付费调用。
