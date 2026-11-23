---
title: 已知限制与待办
read_this: 想知道"哪些是已知缺陷 / 哪些明确不做"时
next: ../code/README.md（改哪个文件）
---

# 已知限制与待办

---

## 1. 已知缺陷（小的、具体的）

> 编号是**历史编号，不重编** —— 下面的正文和别的文档会引用“第 10 条”“#1 #2”。
> 已修的行留在原位标 `[x]`，这样“这条什么时候修的”一眼可见。

| # | 问题 | 影响 | 位置 |
|---|---|---|---|
| 1 | [x] **已修**（2026-10-08）：`dev_run.ps1` 的健康检查 URL 写死 8000 | 四处 `8000` / `5173` 字面量改为 `$Port` / `$FrontendPort`（L88–92）。原记录：脚本能起来，但健康状况栏永远是空的 | `scripts/dev_run.ps1` |
| 2 | [x] **已修**：`smoke.py` 默认端口 | 已是 `8001`（`scripts/smoke.py:30`）。原记录：默认指向 8000，不带 `--base` 就只能连僵尸端口 | `scripts/smoke.py` |
| 3 | [x] **已修**：`state_schema` 编辑不持久 | `graphFields()` 现在投影 store 的 `stateSchema`、`toGraph()` 读它，新增 `setStateSchema()` 标脏。原记录：`toGraph()` 写死 `emptyGraph().state_schema`，载入的共享状态通道会被静默丢掉 | `workflowStore.ts` |
| 4 | [x] **已修**（2026-10-08）：运行历史接口未接前端 | `RunPanel` 空状态加「加载历史」，走 `GET /runs/history`。原记录：接口存在但没人用 | — |
| 5 | 子流程嵌套未实现 | React Flow `parentId` 与后端 `is_container` **已建模**，UI 没做 | — |
| 6 | 内置工具 **10** 个（6 个网络/计算类 + 4 个数据库类，2026-10-16 加后者） | 网络类：`http_request` / `calculator` / `current_time` / `json_query` / `text_stats` / `web_search`（`tools/builtin.py`）；数据库类：`db_write` / `db_query` / `db_tables` / `db_reset`（`tools/results_db.py`，写 `<data_dir>/results.db`）。**为什么是工具而不是节点**：Code 节点的沙箱删了 `open` 且模块白名单没有 `sqlite3`，所以「写库」在 Code 节点里做不到；见 [`../log/phase-7-2-ai-authoring-2026-10-16.md`](../log/phase-7-2-ai-authoring-2026-10-16.md) | `tools/builtin.py`、`tools/results_db.py` |
| 6b | **`web_search` 靠抓搜索引擎的 HTML，没有 API key** | 会撞限流（DuckDuckGo 回 `202`）和反爬。**按查询语言选引擎**（2026-10-22）：含 CJK 时走 百度 → 搜狗 → 360 → DDG → Bing，纯拉丁走 DDG → Bing。中文实测已由百度返回相关结果（此前 Bing 会把「消保法第二十四条」搜成「消费（经济学名词）」）。降级仍返回 `degraded: true`，英文查询目前常落在 Bing。要稳定得接搜索 API（Tavily / 博查），见 [`../log/phase-7-1-web-search.md`](../log/phase-7-1-web-search.md) | `tools/builtin.py` |
| 6c | **业务连接器仍有边界** | 已加 API/FHIR/SQL/邮件/PDF 五个节点（[`../log/phase-8-3-business-connectors-2026-10-22.md`](../log/phase-8-3-business-connectors-2026-10-22.md)）。仍然没有：MQTT/OPC-UA/Modbus、浏览器自动化/登录/Cookie/验证码；PDF 只取内嵌文字（无 OCR）；SQL 只读且 PostgreSQL 需在部署里装 `psycopg2`；邮件只发不收。公开演示禁用 FHIR/SQL/邮件/文档四个私有连接器 | `nodes/integrations.py` |
| 6d | **医院场景缺合规层** | 没有 PHI 字段级脱敏、按科室 RBAC、审批工作台（`human_input` 只是暂停一次 run）、审计留存和设备凭据保管。私有部署 + 医院集成平台是前提，不能把患者数据发到公网演示或未签数据处理协议的模型 | `security/`、`run/` |
| 7 | `repair-graph` 只针对静态校验 | 生成（`generate-graph`）现在会在校验后试跑并回馈运行时失败；**修复**仍只看校验器，节点跑失败的 traceback 还修不了 | `app/ai_buildtime/repair.py` |
| 7b | [x] **已修**（2026-10-19）：生成出的图，AI 自己不能试跑一遍 | 生成在校验通过后用 Start 的默认值真跑一次，失败回馈最多 2 轮；外部错误不纠错、Code 节点仍需人工信任。→ [`../log/phase-7-3-live-authoring-2026-10-19.md`](../log/phase-7-3-live-authoring-2026-10-19.md) §1.3 | `app/ai_buildtime/self_test.py` |
| 8 | [x] **已修**（2026-09-16，任务 12）：安全/认证的错误文案只有英文 | 现在 `deps.py` 有 `_MESSAGES`、`egress.py` 有 `_DENY_MESSAGES` + `reason_localized`，按 `lang` 渲染 | `app/security/`、`app/runtime/egress.py` |
| 9 | 容器 profile 的**镜像未构建过** | flags 有测试，镜像本身没有。**本机 Docker 未运行，无法验证** | `backend/docker/sandbox.Dockerfile` |
| 10 | **节点配置里的 `api_key` 明文落盘** | 见 [`../code/known-defects.md`](../code/known-defects.md) §2。**仍然是设计待决，不是缺陷** —— `export_workflow()` 实测逐字写 `model_dump()`，不遮蔽 | `db/repository.py` |
| 11 | **对运行中的导出包重导出会留下半成品目录** | `write_export()` 先 `rmtree(target)`；旧 agent 进程占着 `<pkg>/data/sessions.db` 时删到一半抛 `PermissionError`（WinError 32），目录已缺 `vendor/`。实测 2026-11-03 撞到。**当前约定：先停旧进程再导出**；修法是写临时目录再切换（P0 收尾） | `runtime/export.py::write_export` |

### 已修掉的（别再重复报）

- **没有节点级超时**（原第 11 条）→ 任务 18：`compiler.py` 的节点调用已包上执行策略，
  超时默认开、重试默认关 → [`../log/phase-2-3-node-timeout.md`](../log/phase-2-3-node-timeout.md)
- `restart_backend.ps1` 硬编码 8000 → 现在按 `-Port` → `$env:BACKEND_PORT` → `8001` 解析
- **`smoke.py` 默认端口**（原第 2 条）→ 已是 `8001`
- **安全/认证错误文案只有英文**（原第 8 条）→ 任务 12，`_MESSAGES` / `_DENY_MESSAGES`
- `replaceGraph` 不可撤销且不置 dirty → [`../lessons/frontend.md`](../lessons/frontend.md)
- `MemoryError` 错误消息是空的（`str(MemoryError())` 是空字符串）→ [`../log/phase-1-1-sandbox.md`](../log/phase-1-1-sandbox.md)
- `sandbox.py` 的 `memory_limit` 只能靠环境变量，runner 传不进去 → 同上
- 容器入口缺 `sys.path` 注入（`-I` 隐含 `-P`）→ 同上
- **`Tool` 节点的下拉框 / AI 提示词的工具清单在 import 时被冻结** → [`../log/phase-1-2-tools.md`](../log/phase-1-2-tools.md)
- **`fail_on_error` 对「工具未启用」这个失败无效** → 同上
- **`unknown_tool_names` 自己是装饰品** → 同上

---

## 2. [重要] 第 10 条的说明：`api_key` 明文落盘

**它在不在，取决于导出物的定位 —— 而这是个待决问题。**
`export_workflow()` 逐字写 `model_dump()`、`redact()` 只遮界面与事件不遮磁盘，
以及「灾备副本 vs 交付产物」两行决策表，
全部归 [`../code/known-defects.md`](../code/known-defects.md) §2（这条缺陷的唯一之家）。

→ 先定 [`../target/decisions.md`](../target/decisions.md) 的待决 #1。

---

## 3. 仍然有效、只在这里记的顺手事项

| # | 事项 | 为什么值得做 |
|---|---|---|
| 1 | `scripts/check-schema-drift.ps1` | TS 镜像是**手工维护**的，会静默漂移；症状（某个 Inspector 字段永远不显示）**极难回溯** |
| 2 | 根目录 `README.md`（项目门面，含 SECURITY 章节） | 现在**有内容可写了**：可直接引用 [`security.md`](security.md)。注意： **这和 `docs/README.md` 不是一回事** —— 那个是文档索引 |
| 3 | 修复**运行期**错误（节点跑失败时的 traceback） | 现在 `repair-graph` 只修静态校验错误 |

> **「下一步做什么」不在这里** —— 去
> [`../target/roadmap.md`](../target/roadmap.md)（按里程碑选）与
> [`../code/README.md`](../code/README.md)（改哪些文件）。
> 这里只记「散落的、够不上一个任务的小事」。

---

## 4. 明确不在范围内

用户没提，**不要主动加**：

- 多用户协作 / CRDT
- 模板市场
- 暴露成 MCP server
- DSL 版本迁移

> **「导出可独立运行的 agent 包」在范围内** —— 属 Phase 6。
> 见 [`../target/delivery.md`](../target/delivery.md)。

---

## 5. 能力边界（不是缺陷，是刻意的限制）

| 限制 | 性质 |
|---|---|
| `retriever` 是 **BM25 关键词检索**（另有 `substring` 策略），不能匹配同义词 | 已文档化。**注意：任务 19 已给“长期记忆”接上了向量检索（`memory/store.py` + GLM embeddings），但 `retriever` 节点本身仍未接** —— `retriever.py` 的 `RETRIEVER` / `EMBEDDINGS` 配置键已预留，接上即可 |
| **凭据遮蔽的两层边界** | ① **按键名**（`api_key` 这类）—— 准确，但对**嵌在字符串里的**凭据无效。② **内容扫描**（2026-09-14 新增）—— 用**有前缀的**模式（`sk-` / `AKIA` / `ghp_` / `xoxb-` / JWT）捕获字符串里的凭据。**代价：没有前缀的凭据仍遮不住** —— 为了不误伤 UUID / 哈希 / 正文，故意不做「长随机串」规则 |
| Code 节点沙盒**不隔离文件系统/网络** | 已文档化（`sandbox.py` docstring + 有测试断言它被文档化） |
| 容器 profile 的**镜像从未构建** | 已文档化（文件头写明） |
| **公开演示禁用 Code**：含 Code 的工作流只能看不能跑/存 | 这是安全演示模式的刻意限制；`radar-intel-01` 因此是只读样板。要运行它必须用本机 local 模式或构建容器沙盒 |
| **公开数据共享**：访客可以互相覆盖工作流 | 无账号的必然结果；删除/导入/导出已关闭，版本历史和每日备份降低损失。需要隔离时应加匿名会话或账号 |
| **画布没有计划管理页面** | 计划可通过 API 或让副驾创建（副驾创建的是 **paused**，需保存后显式启用）。列表/暂停可以问副驾或调 `/api/schedules`；这不是“AI 能改，人有界面看”的状态 |
| **副驾不会自动启用计划** | 创建计划强制关闭，避免在未保存的草稿上运行旧定义；跨工作流改计划会被拒绝。要启用必须再明确说一次 |
| **平台知识是注入的 brief，不是模型天生知道** | brief 从当前 settings/节点 spec 生成；新加的平台能力如果没写进 brief/工具，AI 仍然不知道。改动平台后要同步 `platform_brief.py` 和测试 |
| **原生推理的边界** | `deepseek-flash` **有**原生 thinking（官方文档与原始 SSE 均已核实）；应用显式发 `thinking.enabled`、`reasoning_effort`，并保留 `reasoning_content`。但**工具调用轮默认关闭 thinking**：DeepSeek 要求把上一轮 `reasoning_content` 回传，且开启后 30 次工具调用的编辑循环明显变慢（实测一次 212s 后重试超时）。因此画布 AI 的原生推理主要在洞察/方案轮；修改轮显示的是工具与校验轨迹。深度推理开关仍会让模型先比较方案，但没有「无限思考预算」 |
| **AI 试跑不写入运行历史** | 副驾的 `run_workflow` 直接执行草稿，不经过 RunManager、不写 `runs` 表；结果只显示在 Run 标签的「AI 试跑」卡片，刷新后消失。要持久化历史请用 Run 按钮 |
| **公开演示不允许 AI 触发运行** | `run_workflow` 会绕过 Run 队列和启动额度，因此在 PUBLIC_DEMO 下拒绝；读取运行记录/数据库仍可用。本地模式不受限 |
| **AI 运行不是「任意系统都能连」** | 它能运行画布上的节点、读运行历史和 `results.db`，但没有 OPC UA/MQTT/Modbus/PLC 连接器，也不能登录任意网站；没有对应工具/凭据时只能直说 |
| **AI 不执行主机 shell 命令** | 它能运行节点/工作流、单独测试节点，并在 Code 节点同一沙箱里跑短 Python（`run_python`：无 shell、无 `open`、无网络、模块白名单，公开演示不绑定）。沙箱不是文件系统/网络隔离；Code 节点写入图后仍要用户勾「受信任」。没有 `shell`/`exec` 工具，这是刻意保留的人工闸门 |
| **思考阶段的工具偏只读** | 洞察后、方案前可以调 `get_run_results` / `query_database` / `list_versions` / `get_version` / `get_canvas_layout` / `search_web`，最多 3 轮 6 次；`run_workflow`、`test_node`、写库和 `run_python` 仍在编辑阶段。这样思考不会在一次工具调用里就产生不可撤销的数据变更 |
| **图片只在当轮有效** | 图片以 data URL 随请求发送，不写入对话历史或磁盘；刷新后旧对话只保留文字。公开模式请求体上限 1MB，前端会压缩，超大截图仍可能被 413 拒绝 |
| **版本查看是只读** | `list_versions`/`get_version` 能读历史定义，但没有「用 AI 恢复旧版本」的工具；恢复要走版本历史 UI。 |
| **编辑流程的方案/工具阶段不流式** | 方案是 JSON、工具调用是结构化参数，没有可读 token；侧栏用阶段进度行代替。问题/闲聊的答案仍然是逐 token 流式 |
| **PoW 不是强 bot 防护** | 只是 Turnstile 的国内兜底；主要防线是 Turnstile、Cloudflare 边缘、限流和每日 AI 配额 |
| **限流/nonce 是单进程内存状态** | 公开部署固定 1 worker；重启会清空窗口；匿名额度账本已持久化到 `quotas.db`，不随重启清零 |
| **cloudflared 下载需要 GitHub 可达** | 这台机器到 GitHub 的下载可能失败；需要用代理/VPN 手动下载官方二进制到 `tools/cloudflared.exe`，不要用来路不明的镜像 |
| AI 辅助功能**没有**走 `ModelGateway` | 刻意：它们是平台功能，花费属平台预算；2026-10-21 起由 `ai_buildtime/metering.py` 单独上报 token，纳入公开访客额度 |
| **公开额度不是按人** | 访客 Cookie + IP 双账本只能挡普通/脚本滥用；清 Cookie 换 IP 仍可重置。真正的按人额度需要账号/OAuth |
| **公网仍用主 LLM key** | 当前 `.env.public` 没有独立 `LLM_API_KEY`，会回退到根 `.env`；上线前必须换独立 key 并在供应商侧设消费上限 |
| 一个进程 = **一个能力档位** | 这是批量产出独立 agent 的**主要障碍** → [`../target/vision.md`](../target/vision.md)。[2026-11-03] 部分缓解：能力包（`packs:` manifest）让**工具面**按 agent 收窄，但进程级 provider/设置仍是一份 |
| **files 包只有一个工作区** | 工作区根 = `AGENT_WORKSPACE_DIR` / `<DATA_DIR>/workspaces/default`；平台上传与 agent 工具共享同一目录，**没有 per-session 隔离**（`workspace_id` 在 P5）。导出包默认 `<包>/workspace` |
| **导出包 HTTP 是最小实现** | stdlib `ThreadingHTTPServer` + 可选 Bearer token；**无 TLS、无限流、单进程**；对外部署必须放在反向代理后面（P1 之后补） |
| **平台内没有会话 UI** | 会话能力在 runtime（`app/runtime/session.py`）且导出包自带聊天页，但平台的 `/api/sessions` 与前端会话面板未做（P1-d）；画布仍是「运行面板」而非对话 |
| **审批工作台未做** | `PolicyDecision.requires_approval` 是一等答案，但没有收集人工同意的界面；目前要求审批的权限**失败关闭**（拒绝并说明原因，不假装批准过） |
| **对运行中的导出包重导出会留下半成品** | `write_export` 先删目标目录；旧进程占着 `data/sessions.db` 时 rmtree 中途失败（WinError 32）。**重导出前先停旧进程**；临时目录原子切换列入 P0 收尾 |
| **`_vendor/*.whl` 未复制进导出包** | 导出包 `requirements.txt` 写着 `--find-links vendor/`，但 builder 只把 wheel 放在 `backend/_vendor/`。**真实交付前必须复制/做成基础镜像**（P0 收尾） |
| **导出包的 `config/.env.example` 与运行时的 secrets 读取** | 已修：`main.py prepare()` 现在把 `config/secrets.env`（KEY=VALUE）读入环境；JSON `SECRETS_FILE` 仍支持。注：这不改变「值绝不进包」的约束 |
| **未保存的工作流跑出的运行，重启后不可恢复** | `hydrate` 从 `Workflow` 表读定义，而画布上的试跑（`/runs/adhoc`）**那张图从未保存** → 明确报错，而不是用一个空图跑。
理由：把图存进 `Run` 行会让**行成为一份同时还在画布上被编辑的定义的权威**，两者不一致时无从判断哪份真的跑过。见 [`../log/phase-2-8-resume-after-restart.md`](../log/phase-2-8-resume-after-restart.md) §9.5 |
| **暂停的运行（`CHECKPOINT_STORE=sqlite`）会在 `data/checkpoints.db` 长期留存** | 它们被淘汰策略**豁免**（人在等），所以不会被清。语义正确，但要知情 |
| **[重要] DeepSeek 会把内容审核标记 `<ds_safety>…</ds_safety>` 嵌进返回文本** | **上游服务端行为，不是本项目的缺陷。** 2026-10-08 浏览器实测：一句无害的英文问候，连续两次运行都在 `content` 里带回该标签（含「用户未成年」「分类」「判定」「规则」等字段），我们**原样透传** —— 所以它会出现在节点的输出和 agent 的最终结果里。上游有同类 issue（deepseek-ai/DeepSeek-V3 #1678「emits internal markup in content」）。**未做清洗**：清洗意味着在 runtime 里加一条只对某家供应商成立的文本规则，而这是模型的输出，不是我们的数据。要改应先定「哪些标签算噪声」|

---

## 相关

- 下一步做什么 → [`../target/roadmap.md`](../target/roadmap.md)
- 改哪些文件 → [`../code/README.md`](../code/README.md)
- 待决 → [`../target/decisions.md`](../target/decisions.md)
- 当前验证状态 → [`status.md`](status.md)
