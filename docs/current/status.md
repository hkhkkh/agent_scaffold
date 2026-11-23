---
title: 验证状态
read_this: 需要知道"测试几个通过 / 冒烟几项"时
owner_of: [重要] 所有测试数 / 冒烟数 / 验证结论的唯一归属地
verified: 2026-11-03
---

# 验证状态（实测）

> [重要] **这是所有数字的唯一归属地。** 别处引用时写「见 `current/status.md`」，不要抄。

---

## 汇总

| 项目 | 结果 |
|---|---|
| 后端单元测试 | [x] **1338 passed**（57 个测试模块，收集 1338；2026-11-03 09:5x 实测，`pytest --tb=short` 退出码 0。本机 DNS 未劫持 `example.com`，所以那 2 条 `skipif` 用例实际执行并通过；换一台会被劫持的机器会是 `1335 passed, 2 skipped`） |
| 前端单元测试 | [x] **78 passed**（14 个文件：store 9 + finalOutput 8 + templateCompletions 8 + auto 14 + autoLoop 6 + loopEdges 5 + selectionFocus 7 + versionDiff 4 + pow 4 + copilotStore 4 + CopilotSidebar 2 + RunPanel 1 + **UseView.inputs 4 + UseView.logs 2**）。`auto.test.ts` 的 localStorage 写回曾在全量单进程运行时偶发 flaky；**2026-11-03 页面 QA 修复后实测 78/78**。
| **平台 UI 视觉刷新（2026-11-03）** | [x] 浅色产品默认 + 深色可切（Coze/Semi 语义色）；`tokens.css` 重写 + `refresh.css` 覆盖层；节点卡/面板/弹窗/使用视图重排；主题切换持久化与 `?theme=light\|dark`。验证：`tsc --noEmit` 退出码 0、`vitest` **72 passed**、`vite build` 成功、Chrome headless + CDP 双主题多状态截图验收。见 [`../log/phase-11-platform-ui-2026-11-03.md`](../log/phase-11-platform-ui-2026-11-03.md)。**同日收敛 AI 入口**：顶栏移除「AI 生成」与「自动/按需」，场景卡改为直接发给右侧副驾，Ctrl+K 改为聚焦副驾输入框；CDP 实测通过 |
| **页面功能 QA（2026-11-03）** | [x] **真实浏览器操作**（CDP 工具 + 2 个并行 agent，217 张截图）：P0=0；**4 个 P1 全修**（使用视图输入契约、拖动不标脏/不可撤销、删节点不刷新校验、运行启动失败无提示）；另修 7 个 P2 + 3 个 P3（弹窗焦点与嵌套 Escape、脏数据确认、工作流搜索、日志过滤、防抖校验、取消中性化等）。验证：`tsc --noEmit` 0、`vitest` **78 passed**、`vite build` 通过、逐项 CDP 复测。见 [`../log/phase-12-qa-hardening-2026-11-03.md`](../log/phase-12-qa-hardening-2026-11-03.md) |
| **能力包底座（P0）** | [x] **真跑**：manifest 校验/往返、registry 归属与过滤、doctor 缺依赖报告、权限三策略（allow/deny/**approval 失败关闭**）、审计 JSONL 与写前记录、工作区穿越矩阵与原子写。见 [`../log/phase-9-capability-packs-2026-11-03.md`](../log/phase-9-capability-packs-2026-11-03.md) |
| **files 能力包** | [x] **真跑**：10 个工具（file_/csv_/excel_/pdf_/docx_ 读写真）；CSV(中文/BOM/GB18030)、Excel(多 sheet/header_row/布尔)、PDF(现场生成)、DOCX 往返；权限拒绝不落盘、审计失败关闭、惰性依赖 import；导出 manifest 的 `packs: [files]` 生效 |
| **会话层 + 导出 agent 聊天** | [x] **真机子进程实测**：导出包干净目录 `python main.py serve --http` → 上传 xlsx → 两轮对话（第一轮「10 笔订单 / 111,950 元」、第二轮「笔记本电脑 68,000 / 60.7%」，多轮 history 生效）→ 会话落 `data/sessions.db`（4 条消息）→ `?stream=1` SSE 帧 `run_started → run_finished → done`；`doctor`/`selfcheck` 均 PASS。**2026-11-03 聊天页重设计**（Coze/Semi 风格、浅色 + 深色、Markdown 表格、SSE 流式、拖拽上传、响应式）：Chrome headless 截图验收 + 真实对话渲染正确，见 [`../log/phase-10-chat-ui-2026-11-03.md`](../log/phase-10-chat-ui-2026-11-03.md) |
| 图组合（任务 30） | [x] 真后端实测：保存→钉哈希→运行→改子图被 **422 拒绝**→重存后可跑 |
| **并发上限（任务 15）** | [x] 上限不超、FIFO、等待者可取消、关机不留 `queued` |
| **配额（任务 17）** | [x] 四类各自触发；未上报用量不当 0；超限仍保留 steps |
| **导出 agent（M4 部分）** | [x] **真子进程实测**：`selfcheck` PASS、`run` 产出正确结果、改运行时/改定义都会被封条抓到；**`daemon` 连续运行**：探针包两周期、失败继续、日报每日一次、单实例锁；真实 IM-07 包在无平台进程下完成正常监控、`E-311` 自动调参+邮件、`E-999` 升级、日报+邮件。见 [`../log/phase-8-5-packaged-daemon-2026-10-23.md`](../log/phase-8-5-packaged-daemon-2026-10-23.md) |
| **agent 自带业务工具（31）** | [x] 真子进程实测：`selfcheck` PASS、只有自带的工具在注册表里；工具坏了则启动失败而不是瘦身 |
| **故障隔离（不在路线图）** | [x] 真图实测：`stop` 失败并指名节点 / `continue` 继续且步骤标 `failed` / `default` 下游拿到兜底值 |
| **长期记忆（19）** | [x] **跨两次 run** 实测：第二次问不同问题，检索回第一次写的事实 |
| **目标追踪（21）** | [x] **跨五次 run** 实测：建计划 → 三次推进 → 读回 `3/3 done` |
| **Supervisor 路由（24）** | [x] 真图实测：选中哪个 worker 就只有它跑（**未选中的不跑**）；未知判断走 fallback 或干净停止；`done` 不跑任何 worker |
| **预算熔断（23）** | [x] 真图实测：`warn` 在步骤日志里出警告且不干扰；`stop` 提前结束、状态是 **`stopped`**（既不 `succeeded` 也不 `failed`）、**保留产出**；无上限时行为不变 |
| **定时运行（22）** | [x] **真调度器实测**：强制到期的 job 真的起了一条 run，且该 run 出现在普通运行历史里；`enabled=false` 是**摘掉 job**而非标志位；工作流被删后 `reconcile` 把它标 `orphaned` 而**不删除**，重新指向后复活。规则算术/循环/迟到策略**全部复用 APScheduler**，见 [`../target/reuse.md`](../target/reuse.md) §1.33 |
| **定义版本化（28）** | [x] **真 HTTP + 真重启实测**：存 v1 → 改 v2 → **v1 仍取回 ORIGINAL**（不是当前版）；版本摘要不含 graph 载荷；拖节点**不改变**内容哈希、但保存仍记一个新版本号；**重启后端后三个版本与哈希逐字符不变**（证明版本活在 SQLite，不是内存） |
| **事件唤醒（22）** | [x] **真 HTTP 实测（非 TestClient）**：22/22（两条守卫已进 `_guardprobe_capability.py`）。`POST /api/hooks/<token>` 无 Authorization 头即可起 run 并跑到 `succeeded`；请求体覆盖存储的默认 inputs；未知 token 与已禁用的 token **答案完全相同**（都是 404，探不出来）；轮换后旧 URL 立刻失效；定时那半不受影响 |
| **顺带修的两个真缺陷** | [x] **节点日志全丢**（`StepRecord` 无 `logs`、记录器无 `log` 分支）与 **`log` 早于 `node_started` 被丢弃** —— 两条都反向验证过 |
| **反向验证（新增 2 条）** | [x] `RUNTIME_PATHS` 清单、节点图标 —— **两条都验证过「删掉会被抓到」** |
| **顺手缺陷** | [x] 5 个已修（端口默认值、`state_schema` 持久化、循环回边样式、AI 端点的 `lang`）→ [`../log/phase-2-11-small-defects.md`](../log/phase-2-11-small-defects.md) |
| i18n 校验 | [x] `RESULT=PASS (0 failures)` |
| 前端类型检查 | [x] `tsc --noEmit` 退出码 **0** |
| 护栏反向验证（能力层 / 迁移 / 回收 / 节点策略 / 运行存储 / checkpoint / 遮蔽 / 重启恢复 / **调度与事件触发** / **导出依赖 / SSE 线格式 / 级联删除**) | [x] **118/118**（`_guardprobe_capability.py`，`SOURCES_RESTORED=True`，2026-10-10）**注意：任务 30 / 31 / 12 / 19 / 21 / 故障隔离的守卫仍未进此探针** |
| 护栏反向验证（安全） | [x] **19/19**（`_guardprobe_security.py`）。**注意：这两个常量此前指向已不存在的 `app/ai/`，探针停摆；2026-10-10 修好，但 `19/19` 这个数本次未重测**（实测只到 18 OK / 1 FAIL，那条 FAIL 是[日志](../log/docs-truth-audit-2026-10-10.md) §3.3 记的一次污染） |
| 护栏反向验证（AI 修复） | [x] **7/7** |
| 端到端冒烟（不带 AI） | [x] **52/52**（2026-10-08 新增 3 条安全断言 + 1 条 traceparent） |
| 端到端冒烟（含真实 DeepSeek） | [x] **94/94** |
| **持续产线 agent（IM-07）** | [x] 27 节点、每分钟调度自动触发；短射故障自动调参并邮件、E-999 升级人工、同报警冷却；杀进程后守护重启且计划续跑；保留策略有归档测试；**导出包 `daemon` 在无平台进程下复验正常周期、自动调参、升级与日报**。见 [`../env/im07-line-agent.md`](../env/im07-line-agent.md) |

> 收集 **1037**，其中 **2 个 skip**：`test_security.py` 与 `test_security_i18n.py` 各一条，
> 断言「公网域名应被放行」，而本机 DNS 把 `example.com` 解析成 `127.0.0.1`（DNS 被劫持）。
> **策略拒绝它是对的**，所以断言的前提不成立 —— 用 `skipif` 显式跳过，报告里可见，不是静默通过。

> [2026-10-10] **上面那个护栏数字本来是不可信的，值得记一笔。**
> `_guardprobe_capability.py` 里有 3 个路径常量指向 `app/ai/`，而该目录已在
> `6ebe52e`（把 build-time AI 拆出运行时的那个提交）里搬到了 `app/runtime/ai/`
> 与 `app/ai_buildtime/`。常量没跟着搬，`main()` 又在检查文件是否存在**之前**
> 就读它 —— 于是探针**抛 FileNotFoundError 直接崩**，一条结论都产不出。
> 期间这里一直挂着 `114/114 SOURCES_RESTORED=True`。
> 修好常量后实测 **118/118（其中 4 条是这次新加的）**，另有一条 `manager.py`
> 的用例因该文件同期被重构成 `load_workflow()` / `WorkflowUnavailable` 而锚点失效，
> 已一并更新。
> **教训**：这个探针是手工在最后跑的，没有任何东西会注意到它崩了 —— 也就是说
> 「跑过探针」和「探针能跑」是两件事。**改目录结构后要把它跑一遍**，
> 而不只是改文档里的数字（那一次提交顺手改了 doc-count，没跑探针）。

**后端 1338 个测试的分布**（收集 1338；只列 >15 的）：

```
 138  test_files_pack.py
  72  test_session.py
  56  test_http_api.py
  55  test_goals.py
  55  test_auth.py
  49  test_copilot.py
  45  test_security.py
  35  test_tool_capability.py
  35  test_repair.py
  34  test_secret_provider.py
  34  test_capability_packs.py
  33  test_node_attempts.py
  33  test_model_gateway.py
  33  test_memory.py
  29  test_run_store.py
  29  test_capability_layer.py
  28  test_run_retention.py
  27  test_wind_down.py
  27  test_redaction.py
  26  test_tool_authoring.py
  26  test_scheduling.py
  23  test_supervisor.py
  23  test_runtime.py
  22  test_security_i18n.py
  21  test_fault_isolation.py
  21  test_compiler.py
  20  test_checkpoints.py
  19  test_platform_context.py
  17  test_web_search.py
  17  test_small_defects.py
  17  test_resume_after_restart.py
  17  test_export_agent.py
  16  test_registry.py
---
1338  total（55 个文件）
```

**规模**（文件数为 2026-11-03 实测；行数是 2026-10-19 的旧测量，本次未重测）：
`app/` 116 文件；`tests/` 57 文件（55 个测试模块 + `conftest.py` + `__init__.py`）；
`frontend/src/` 68 文件；`docs/` 84 文件（全是 `.md`）。


---

## 分项证据

### 国际化

| 项 | 结果 |
|---|---|
| 校验接口 | [x] `/workflows/validate?lang=` 返回中文，参数名也是中文（「提示词」而非「Prompt」） |
| **runtime 分层（27a）** | [x] **门禁实测**：`test_layer_boundary.py` 用 AST 解析导出包里的 runtime 源码，断言无 `app.db`/`app.api`/`fastapi`/`sqlmodel` 引用。**它抓出两处真实违规**：`manager.py` 的惰性 `from app.db.repository`、`runs.py` 的惰性 `from app.db.run_store` |
| **构建源码 wheel（27b）** | [x] **实测**：从 `H:/agent_1/langgraph/libs/langgraph` 用 `python -m build` 构建成功（`hatchling` 后端）。产物 `pregel/_utils.py` **221 行、用 `dis` 读字节码**；PyPI 同版本 wheel 是 **291 行、读源码文本** —— 与 [`../target/reuse.md`](../target/reuse.md) §1.36 的快照一致，故不能换成 PyPI wheel |
| **封条（27c）** | [x] **实测 + 反向验证**：`backend/scripts/build_vendor.py` 解析出 **49/49 wheel、0 sdist**；生成 49 条带 sha256 的 pin。`--no-index --find-links _vendor --require-hashes` 离线安装通过；**往一个轮子追加 1 字节后 pip 报 `THESE PACKAGES DO NOT MATCH THE HASHES`** —— 即「改过就装不上」（2026-10-10：46 → 49，补 `aiosqlite` / `langgraph-checkpoint-sqlite` / `sqlite-vec`） |
| 前端页面 | [x] 21 个节点 + 全部分类/参数/界面文案均为中文 |
| 语言双向切换 | [x] 中↔英 均立即重校验，`localStorage` 刷新后保持 |
| Vite 代理 | [x] `GET :5173/api/node-types` → **200** |

详情 → [`i18n.md`](i18n.md)

### 安全

| 项 | 结果 |
|---|---|
| Phase 0 五个 P0 | [x] 全修 |
| 沙盒死循环 | [x] **3 秒内真被杀** |
| SSRF / 凭据 / 路径遍历 | [x] 全部拒绝 |
| 认证 | [x] 非回环绑定**自动升级**为 token 模式、无 token **拒绝启动**、scope 拦截实测 |

详情 → [`security.md`](security.md)

### AI 功能

| 项 | 结果 |
|---|---|
| AI 填充（DeepSeek） | [x] 浏览器实测通过 |
| **AI 修复（DeepSeek）** | [x] 冒烟 25 项 + 浏览器实测：加节点/连线、保留用户文案与 id、早退不调模型 |
| **AI 生成 + 试跑（DeepSeek）** | [x] **真机实测**：4 节点、14 次工具调用、11 张 SSE 图快照、校验 0 纠错、**试跑 1.3s 通过并真实产出答案** |
| **生成过程实时可见** | [x] SSE 每步推完整快照；对话框内只读画布渲染并高亮新增/修改节点 |
| **连线语义** | [x] `connect` 工具强制 `label`（用户语言，写这条边传什么）；`inspect_canvas` 显示；画布默认隐藏、悬停/选中才显示，点击边后 Inspector 可编辑 |
| **回边可见性** | [x] 5 个前端用例：回边识别（目标是 Loop + 几何向后）、蛇形体边不误判、贴近端点的走廊 + 轮次标签、事件历史取最后一轮、`source_handle` 兼容 |
| **选择高亮** | [x] 4 个纯函数用例：点连线 → 两端节点 primary；点节点 → 入/出边 incident + 一跳邻居 secondary；自环不算邻居。**浏览器实测**（CDP 真点击）：10 节点图点节点 → 1 incident + 1 邻居 + 8 变暗，点边 → 2 端点 + 8 变暗；42 节点图点节点 → 2 incident + 2 邻居 + 39 变暗 |
| **编辑副驾（放手/确认）** | [x] **真实 DeepSeek + 浏览器实测**：确认模式「理解 → 方案 → 批准 → 改 2 个节点 → 校验 → 试跑通过」；放手模式「断开旧边 → 加节点 → 重连 → 校验 → 试跑通过」，新增节点落在两端点之间，已有坐标不变 |
| **AI 的平台知识（2026-10-23）** | [x] prompt 注入 live brief（运行上限、调度频率、Loop 上限、无 Wait、导出 daemon、public demo）；洞察上下文带当前 Triggers；副驾 4 个平台工具。**真实 DeepSeek 实测**：准确回答 IM-07 的两条计划与下次触发；按“设为每分钟、别改图”创建出 `enabled=False` 的计划并提示保存后启用。见 [`../log/phase-8-6-ai-platform-awareness-2026-10-23.md`](../log/phase-8-6-ai-platform-awareness-2026-10-23.md) |
| **副驾思考顺序 / 正文可读性** | [x] 思考块挂到对应 assistant 消息、渲染在正文上方（流式展开、结束后折叠）；正文 14px 正文色、1.75 行高、GFM Markdown；旧会话加载时把整轮思考迁到对应回答；等待期间显示阶段进度行。**真实 SSE 实测**：问题回答 thinking 2.5s、token 3.8s 起；编辑流程方案/工具阶段无文本 token，属正常停顿 |
| **原生推理（2026-10-23）** | [x] `LLM_THINKING=auto` 对 DeepSeek 发 `thinking.enabled` + `reasoning_effort`；`ReasoningChatOpenAI` 保留 `reasoning_content`（langchain-openai 1.6 会丢弃 delta 字段）。**原始 SSE 实测**：`reasoning_content` 2.46s 开始、`content` 2.78s；应用内首个 `thinking` 事件 3.01s。工具调用轮显式关闭 thinking（避免超时与回传约束），洞察/方案轮保留。见 [`../log/phase-8-7-native-reasoning-copilot-fixes-2026-10-23.md`](../log/phase-8-7-native-reasoning-copilot-fixes-2026-10-23.md) |
| **多轮执行显式可见（2026-10-23）** | [x] 侧栏「执行记录」逐条显示工具调用、校验、修正、试跑、产出比对；思考块按轮编号。真实放手模式实测：简单翻译 8 次工具、1 次校验、22.0s；Code 求和 8 次工具、1 次校验、22.9s。前端 72/72、typecheck 0 |
| **AI 能运行 / 查运行 / 查数据库（2026-10-23）** | [x] 副驾新增 `run_workflow`、`get_run_results`、`query_database`。**真实实测**：一次请求内 `run_workflow` 14.9ms 通过并返回逐步输出，`query_database` 列出 4 张表共 323 行（17.9s）；随后 `get_run_results` 读回持久化 run `565212b1871f` 的 succeeded、输出与逐步结果（21.7s）。工具结果只在本地模式执行；公开演示拒绝 AI 触发运行，保留 Run 按钮的队列与额度 |
| **AI 单节点测试 / 布局 / 版本 / 联网（2026-10-23）** | [x] 新增 `test_node`、`get_canvas_layout`、`list_versions`、`get_version`、`search_web`。单测覆盖单节点执行、布局坐标、图片块进入 user content；真实 DB 读 `im07-line-guardian` 5 个版本（版本 5 为 27 节点/26 边），真实 `web_search` 返回官方 Vision 文档。版本 5 的摘要 5,385 字符，可完整进入工具消息 |
| **思考阶段只读工具（2026-10-26）** | [x] 洞察 JSON 新增 `needs_investigation`；为 true 时在方案前跑最多 3 轮 / 6 次只读工具（运行记录、数据库、版本、布局、联网），工具事件进入执行记录，摘要写入方案提示词。单测覆盖：工具集严格只读、调查提示词、调查结果进入 plan、调查阶段事件。**真实 DeepSeek 实测 23.1s**：3 次查询后准确报告最近运行 `04b9798507e8` 的失败节点与 4 表 323 行，画布未改。见 [`../log/phase-8-8-thinking-tools-2026-10-26.md`](../log/phase-8-8-thinking-tools-2026-10-26.md) |
| **沙箱 Python 执行（2026-10-26）** | [x] 副驾新增 `run_python`：复用 Code 节点同一资源受限子进程沙箱（无 shell、无 `open`、无网络客户端、模块白名单，默认 10s、上限 60s）。公开演示不绑定该工具并在执行函数里再次拒绝。单测实测 `sum(range(5)) → 10`；**真实 DeepSeek 实测 12.5s**：`sum(range(1, 101)) → 5050`，未加节点、未写库 |
| **AI 数据库增删改查（2026-10-23）** | [x] `db_update`/`db_delete` 注册为危险工具：必须提供 where 条件，每次最多 500 行；副驾 `insert/update/delete_database_rows` 仅本地模式可用。**单测实测**：插入 2 行 → 更新 1 行 → 查回 → 删除 1 行 → 只剩 1 行；空 where 被拒绝 |
| **AI 识图（2026-10-23）** | [x] DeepSeek 官方文档确认 `deepseek-flash` 支持图片（仅 user 消息、PNG/JPEG/GIF/WebP）。副驾最多 4 张图，前端超大图缩到 1600px/JPEG，后端限制每张约 6MB。**真实实测**：上传红底白框 `HELLO 42` 图，6.3s 准确回答颜色与文字 |
| **AI 试跑在运行面板可见（2026-10-23）** | [x] `self_test` 结果带 `trace`（节点开始/完成/失败/日志、耗时、输入输出），前端存入 `selfTest` 并在 Run 标签显示逐节点步骤；标注「草稿试跑，不写入运行历史」。前端 RunPanel 用例 1 条，72/72 通过 |
| **放手模式不因 Code 信任门回滚（2026-10-23）** | [x] `untrusted_node` 从“回归”判定中排除；只有可修复错误才进入修正轮。真实实测：Code 求和 agent **applied=True、rollback=False、corrections=0**，画布从 2 节点变 3 节点 |
| **副驾快照不换文档 id（2026-10-23）** | [x] `GraphDraft.to_graph` 编辑时保留原 graph id（此前每个快照都 mint 新 id，前端误判为切换工作流并清空对话）。真实实测所有 4 张快照 id 均为 `blank-probe` |
| **AI 语气 / 温度（2026-10-23）** | [x] 全部构建时 prompt 追加工具语气：直接结论、不情绪化、不问候/道歉/emoji、不堆套话；补节点同样；`temperature=0.0` 有测试钉住。**真实 deepseek-flash 实测**：「你好」直接说明画布现状；「够不够用？」一句话给出“不够用”的结论 |
| **业务洞察必做** | [x] 每次请求先产出业务判断卡；信息充分时 `research skipped`，不足且 `AI_BUSINESS_RESEARCH=true` 时才联网（最多 2 条查询，来源进卡）；公开模式该开关为 false |
| **产出比对** | [x] 可选样例输入/期望产出；试跑后用一次 LLM 判定实际产出，报告给出 `matches / gaps`；不匹配最多再修一轮 |
| **AI 修改进版本** | [x] `workflow_versions.meta` + `PUT /workflows/{id}/ai`；**真 HTTP 实测**：版本 2 的 `meta.ai_change` 读回完整，版本 1 的 `meta={}` |
| **版本历史 UI** | [x] **浏览器实测**：v2 列表标 AI 修改；对比 v1 显示节点 +1 / ~1、连线 +2 / −1；预览高亮新增/修改节点；恢复 v1 后画布变 3 节点、进入未保存状态 |
| **多轮上下文** | [x] 前端只带最近 8 条、每条 ≤2000 字；后端再次裁剪到 6000 字；插入「Earlier in this conversation」 |
| **范围遮罩** | [x] 确认模式下选区外框半透明虚线框 + 「AI 写入范围」标签，跟随缩放/平移，`pointer-events:none` |
| **AI 记账与公网额度** | [x] 2026-10-21：构建时 `metering.py` + 运行时 usage sink 共用访客/IP/全局 token 账本；AI 请求先扣次数、失败也计入，单次 30k；`/api/runs/adhoc` 与 `/api/runs/node-test` 也按运行计数，`/resume` 续记 token；第 3 次运行实测 `429 quota_exceeded`。详见 [`security.md`](security.md) §7b 和 [`../log/phase-8-1-abuse-controls-2026-10-21.md`](../log/phase-8-1-abuse-controls-2026-10-21.md) |

详情 → [`ai-features.md`](ai-features.md)

### 能力层（Phase 1）

[x] **4/4 全部完成**：`SandboxRunner` · `ToolRegistry` · `ModelGateway` · `SecretProvider`

- Code / Tool / Agent / LLM 节点**全部经接口执行**
- **真实 DeepSeek 报告 token 用量**（流式路径也能统计）

详情 → [`../log/phase-1-*.md`](../log/)

### Phase 2

| 项 | 结果 |
|---|---|
| alembic 迁移 | [x] 真实库**收养**，13 工作流 / 30 运行 / 122 步骤**零丢失** |
| 运行回收与淘汰 | [x] 真实后端 `MAX_RETAINED_RUNS=1` 下 14/14 检查通过 |
| **通用节点超时 + 重试（任务 18）** | [x] 超时默认开、重试默认关；**人工节点在 `node_max_retries=3` 下仍只暂停一次** |
| **`RunStore` 接口（任务 2.6）** | [x] `import app.runtime.manager` **不再加载 sqlmodel / fastapi**；三个实现 sqlite / memory / null |
| **持久化 checkpoint（任务 14）** | [x] **默认已是 sqlite**：实测「暂停 → 重启后端 → 新 saver 读回状态」成功 |
| **重启后的 resume（任务 14b）** | [x] `RunManager.hydrate` 从 `Run` 行 + `Workflow` 定义 + checkpoint 重建 context。**端到端实测**（真 uvicorn + 真重启）：暂停 → 重启 → `GET /runs/{id}` 的 interrupt **逐字段与重启前相同** → `POST /resume` → `succeeded`，输出正确。顺手修掉一个真实 500（SQLite 读回的 datetime 丢时区） |
| **内容级凭据遮蔽** | [x] 用上游的 `apply_strategy` + 自建**有前缀**模式。**顺手修掉 transcript 的一个真实泄露** |

详情 → [`../log/phase-2-1-alembic.md`](../log/phase-2-1-alembic.md) ·
[`../log/phase-2-2-retention.md`](../log/phase-2-2-retention.md) ·
[`../log/phase-2-3-node-timeout.md`](../log/phase-2-3-node-timeout.md) ·
[`../log/phase-2-4-run-store.md`](../log/phase-2-4-run-store.md) ·
[`../log/phase-2-5-checkpoints.md`](../log/phase-2-5-checkpoints.md) ·
[`../log/phase-2-6-redaction.md`](../log/phase-2-6-redaction.md)

---

## 几个值得记录的实测细节

### AI 填充

输入描述「把用户的问题改写成一句简短的中文搜索关键词，不要解释，只输出关键词」，
DeepSeek 返回：

- 中文理由
- `system_prompt` 的 before/after 差异
- 两条实用警告（「上游未连接任何节点」「该节点输出未被下游使用」）

点 Accept 后提示词正确应用。**AI 全程只给建议，绝不擅自改画布。**

### AI 修复

故意造一个坏图（1 个 LLM 节点、必填 prompt 为空、无连线、无开始节点）：

| 模式 | 结果 |
|---|---|
| 默认（只修 error） | **1 步**加了一个「开始」节点，错误清零；缺「结束」是 warning，**没动**，如实报告「还剩 5 个警告」 |
| 勾选「同时修复警告」 | **3 步**加了「开始」+「结束」+ 2 条连线，**0 剩余** |
| 用户资产 | 节点、label、提示词文本**全部原样保留**；`id` 与工作流名也没变 |
| 对已合法的图再点一次 | `attempts=0`，**一次模型都没调**，`changed=false` |

### 沙盒（逐项跑出来的，不是推断）

| 用例 | 结果 |
|---|---|
| `result = sum(range(10))` | [x] `45` |
| `import json` + 使用 | [x] 可用（修掉了一个**先前就存在**的 bug） |
| `print('hello')` | [x] stdout 被捕获返回，**不污染子进程协议** |
| `inputs` / `vars` 注入 | [x] 可用 |
| `import os` | [x] 拒绝：`Module 'os' is not allowed` |
| `().__class__` / `__subclasses__` | [x] AST 守卫拦截并给出**行号** |
| `getattr({}, 'x')` | [x] `NameError`（原语已移除） |
| `while True: pass`（timeout=3） | [x] **3 秒内被真杀死** |
| `'x' * 900MB` | [x] 被 Job Object 拒绝，返回 `MemoryError` |

---

## 注意： 跑反向验证的注意

**跑完必须确认 `SOURCES_RESTORED=True`。**

一旦被中断，**第一步是恢复，不是跑测试**；看到 `NOT caught` 时先怀疑探针自己
（多半是变异根本没被执行，而不是守卫真的缺失）。完整故事与修法 ——
两段都归方法论的家：

→ [`../lessons/process.md`](../lessons/process.md) §探针被中断 · §缓存的伪证

---

## 相关

- 已知缺陷与未完成项 → [`limitations.md`](limitations.md)
- 怎么跑这些检查 → [`../env/run.md`](../env/run.md)
- 反向验证的方法论 → [`../lessons/process.md`](../lessons/process.md)
