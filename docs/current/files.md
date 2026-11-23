---
title: 文件清单
read_this: 想知道"有哪些文件 / 加一个文件该放哪"时
owner_of: [重要] 文件清单与目录结构的唯一归属地
verified: 2026-10-21
---

# 文件清单（实测）

**后端 191 个 Python 文件**（app 116 + tests 57 + `backend/scripts/` 10 + `migrations/` 8；
另有仓库根 `scripts/` 的 23 个（含 `scripts/deploy/` 15 个部署/守护脚本与 `scripts/demo/` 2 个演示脚本）、以及
`backend/` 根下 20 个 `_*` 脚本不计。2026-11-03 实测）

---

## 后端

```
backend/app/
  ai_buildtime/ 11 __init__ copilot gen_graph gen_node metering parsing prompts repair self_test
                    platform_brief platform_tools
                    ↑ platform_brief：live 平台事实；platform_tools：副驾的计划/部署工具（2026-10-23）
                ↑ 构建时 AI（画图/补节点/修图/编辑副驾/生成后试跑）。**不进 runtime 包**（任务 27a）
                ↑ copilot：业务理解 → 方案 → 受控修改 → 试跑（2026-10-20）
  api/     11   __init__ access ai catalogue files hooks lab runs schedules settings workflows
                ↑ files：工作区上传/列表/下载/删除（2026-11-03）
  db/       6   __init__ migrate models repository run_store session
                ↑ migrate：ensure_schema 唯一入口（Phase 2 迁移）
                ↑ run_store：SqliteRunStore（任务 2.6；放在平台侧而不是 runtime/）
  graph/    7   __init__ compiler inspect schema state subgraphs validate
                ↑ inspect：生成期间给模型的流向/诊断渲染（2026-10-16）
  memory/   5   __init__ goals goals_node nodes store
    ↑ 三层记忆 + 目标追踪，建在 langgraph.store 上（任务 19 / 21）
  nodes/   16   __init__ agent base code condition control end human
    llm prompt registry retriever start subgraph supervisor tool
        runtime/ 25   __init__ attempts audit budget checkpoints egress events
      export export_templates expr http_api manager permissions projection
      queue runs runner sandbox sandbox_child serialize session template
      trace workflows workspace
    ↑ 2026-11-03 能力包底座：audit（审计 JSONL）· permissions（allow/deny/approval 策略）
      · workspace（受限文件根 + 原子写）· session（会话/消息 + SQLite 存储）· http_api（stdlib HTTP + 内置聊天页）
        packs/ 6  __init__ manifest registry  builtin/{__init__ core files}
          ↑ 能力包：manifest 声明 tools/permissions/deps/secrets；registry 负责工具归属、
            依赖检查与 doctor；core = 原有 12 个工具，files = 10 个文件/表格/文档工具
        ai/ 4  __init__ gateway messages provider
          ↑ 运行时调模型（原 app/ai/ 的一半）
                ↑ runner：代码执行能力（Phase 1）
                ↑ attempts：节点执行策略（超时 + 重试，任务 18）
                ↑ runs：RunStore 接口（任务 2.6）[重要] 不含 app.db 依赖
        ↑ workflows：定义加载注入点（27a）[重要] 不含 app.db 依赖
                ↑ checkpoints：CheckpointerProvider（任务 14）
      scheduling/ 4  __init__ rules service store
    ↑ 定时 / 事件唤醒（任务 22；复用 APScheduler）
  security/ 9   __init__ auth deps gate policy quotas ratelimit secrets web
                ↑ gate：Turnstile + PoW + 签名 Cookie（2026-10-20）
                ↑ quotas：匿名访客额度、SQLite 用量账本、模型 token 记账（2026-10-21）
                ↑ policy：公开演示的 admin-only 路径与图策略
                ↑ ratelimit / web：限流、体积上限、Origin/Host/安全头
      tools/    5   __init__ authoring builtin loading registry
  ·         4   __init__ main settings web
                ↑ web.py：公开模式同源托管 SPA（2026-10-20）
```

### [重要] `app/ai/` 已按依赖切开（任务 27a 完成）

判据是**什么时候调模型**，不是用哪个库：

| 原文件 | 现在在哪 | 类别 |
|---|---|---|
| `gateway.py` · `provider.py` · `messages.py` | `runtime/ai/` | **运行时**：节点在跑的时候调模型 |
| `gen_graph.py` · `gen_node.py` · `repair.py` · `prompts.py` · `parsing.py` · `self_test.py` · `copilot.py` | `ai_buildtime/` | **构建时**：调模型去*写*或*改*定义；`self_test` 把结果真跑一次，`copilot` 先理解业务再受控修改 |

**没有 `app.ai.` 这个包了。** `messages.py` 跟着 runtime 走，因为 `runtime/egress.py`
和 `security/deps.py` 用它；它的旧位置在 `gen_node.py` 里，当初并为平台一伙是错的。

→ 详见 [`../target/vision.md`](../target/vision.md) §三层 ·
[`../log/phase-6-2-runtime-split-seal.md`](../log/phase-6-2-runtime-split-seal.md)

### 其他后端目录

```
backend/docker/      1   sandbox.Dockerfile    ← 注意： 从未构建过
backend/migrations/  8   env.py + versions/ 下 7 个迁移（script.py.mako 是模板，不计）
backend/scripts/    10   build_vendor.py  check_i18n.py  measure_reuse.py
                         create_flagship_workflow.py  fix_user_ecommerce_wf.py  run_flagship.py
                         simulate_integrations.py
                         build_im07_workflow.py  mock_production_line.py  im07_maintenance.py
                         ↑ build_vendor：构建 wheel + 生成封条锁（任务 27b/27c）
                         ↑ im07_*：IM-07 产线工作流生成、模拟网关、保留/归档维护
backend/tests/      57   （57 个文件；测试条数见 current/status.md）
                         ↑ test_layer_boundary：导出包无平台引用的 AST 门禁（27a）
                         ↑ test_capability_packs：能力包 manifest/registry、权限/审计/工作区、
                           导出 parity、平台 files API（2026-11-03）
                         ↑ test_files_pack：files 包 10 个工具、路径矩阵、CSV/Excel/PDF/DOCX 往返、
                           权限与审计（2026-11-03）
                         ↑ test_session / test_http_api：会话存储与服务、stdlib HTTP/SSE/上传/鉴权（2026-11-03）
                         ↑ test_template_refs / test_self_test / test_generate_stream：2026-10-19 新增
                         ↑ test_copilot：编辑副驾的范围限制、位置保留、断开连接与 SSE（2026-10-20）；文档 id 保持与信任门不回滚（2026-10-23）
                         ↑ test_native_reasoning：DeepSeek thinking 参数、reasoning_content 流式/非流式保留、工具轮回传（2026-10-23）
                         ↑ test_results_db_crud：db_update/db_delete 的条件必填、500 行上限与往返（2026-10-23）
                         ↑ test_public_access：门禁 Cookie/PoW、限流、体积、admin-only、公开演示策略（2026-10-20）
                         ↑ test_public_quotas：访客 Cookie、双主体账本、AI token 上限、429（2026-10-21）
```

---

## 前端（70 个源文件）

```
frontend/src/
  ·          2   App.tsx  main.tsx
  types/     1   workflow.ts        ← 后端 schema 的 TS 镜像
  styles/   12   tokens base components header canvas copilot inspector run db versions gate
                 ↑ refresh.css：视觉刷新覆盖层（2026-11-03，最后 import，改视觉先看它）
  api/       1   client.ts          ← 手写 SSE 解析（run + generate + copilot 共用）
  access/    7   accessStore GateRoot GateScreen PublicDemoBanner pow[.test] turnstile
                ↑ 人机验证门禁、PoW、Turnstile 加载与公开演示能力（2026-10-20）
  store/     2   workflowStore.ts   ← 单一 zustand store
                workflowStore.test.ts
  ui/        6   Dialogs Icon JsonViewer SettingsDialog ThemeToggle WorkflowBrowser
                ↑ ThemeToggle：明暗主题切换 + localStorage 持久化 + ?theme 强制（2026-11-03）
  canvas/    8   Canvas NodePalette LoopBackEdge loopEdges[.test] selectionFocus[.test] nodes/BaseNode
  copilot/   3   CopilotSidebar CopilotStageBar copilotStore
  versions/  3   VersionHistoryDialog versionDiff[.test]
  inspector/ 5   Inspector SchemaForm TemplateTextarea templateCompletions[.test]
  run/       6   HumanInputDialog RunPanel RunPanel.test finalOutput[.test] runController
  use/       3   UseView.tsx  inputs[.test]   ← 使用视图（产品面）；inputs 把单框文本
                ↑ 映射到 Start 声明的入口字段（message/query/…），2026-11-03

  ai/        2   GeneratePreview RepairGraphDialog
                ↑ GenerateGraphDialog 已随「顶栏 AI 生成」一起退役（2026-11-03）：AI 只留右侧副驾
  i18n/      2   index.ts translations.ts
  assist/    4   auto[.test] autoLoop.test mode
                ↑ 自动填补引擎保留并有测试，但已不从 UI 挂载（「自动/按需」开关退役，2026-11-03）
  db/        1   DbPanel.tsx
frontend/public/
  landing.html   ← 公网 `/` 的产品介绍站，`/app` 才是编辑器（2026-10-21）
  landing.css    ← 落地页样式（浅色产品站，无外部字体/图片）
  landing.js     ← 滚动入场、进度条、首屏视差、提示词轮播（无依赖，2026-10-21）
```

**每个文件干什么** → [`../reference/frontend-files.md`](../reference/frontend-files.md)

> 注意： **实际运行在 `%TEMP%\agent_web`**，不是 `frontend/` →
> [`../env/frontend-temp.md`](../env/frontend-temp.md)

---

## 脚本（23 个）

| 脚本 | 作用 |
|---|---|
| `build_frontend.ps1` | 在 `%TEMP%` 装依赖 + 类型检查 |
| `sync_src.ps1` | 同步源码到 dev 目录，触发 Vite 热更新 |
| `dev_run.ps1` | 起前后端并探测（端口按 `-Port` → `$env:BACKEND_PORT` → 8001 解析；探测走 `$Port` / `$FrontendPort`） |
| `restart_backend.ps1` | 释放端口并重启后端（`-Port` 可配，默认 8001；启动后自检两处本地化） |
| `check_llm.py` | 单独验证 LLM 连通性 |
| `smoke.py` | 端到端冒烟（默认 `BASE = http://127.0.0.1:8001`；`--base` 只在换端口时才要带） |
| `demo/excel_chat_demo.py` | 生成样例表格 → 上传工作区 → 保存/运行演示工作流 → 导出独立 agent（2026-11-03） |
| `demo/excel_chat_agent.json` | 演示工作流定义（agent 节点绑定 files 包工具，输入 `message`/`history`） |
| `deploy/build_public.ps1` | 在临时目录构建生产前端并复制回 `frontend/dist` |
| `deploy/start_public_backend.ps1` | 用 `.env.public` 在 `127.0.0.1:8002` 启动单 worker（无 reload/proxy headers）；本机 8001 不受影响 |
| `deploy/start_cloudflared.ps1` | 后台运行命名隧道 |
| `deploy/install_cloudflared.ps1` | 安装 cloudflared、登录、创建 tunnel、写 ingress、路由 DNS |
| `deploy/quick_tunnel.ps1` | trycloudflare 临时隧道，默认指向 `127.0.0.1:8002`（`-Port` 可改） |
| `deploy/deploy.ps1` | 备份 → 构建 → seed → 启动 → 验证的编排 |
| `deploy/backup_data.py` | SQLite backup API + workflows 复制 + manifest + 保留 N 份 |
| `deploy/seed_workflows.py` | 只把选定工作流定义导入公开数据目录 |
| `deploy/verify_public.py` | 门禁/PoW/Cookie/额度/admin/落地页/SPA/安全头/体积/Origin 的端到端检查 |
| `deploy/run_im07_stack.ps1` | IM-07 守护进程：每 20 秒检查 8001/8101，异常重启，每天 03:00 后跑一次保留维护 |
| `deploy/run_im07_maintenance.ps1` | 单独执行保留/备份（`scripts/im07_maintenance`）并写日志 |
| `deploy/run_im07_supervisor.vbs` | 启动文件夹入口：隐藏窗口运行 supervisor，退出 10 秒后重拉 |
| `deploy/install_im07_tasks.ps1` | 可选的管理员级计划任务注册（本机普通用户被 Access denied） |
| `deploy/start_public_all.ps1` | 重启后一条命令起公网后端 8002 + 命名隧道 |
| `deploy/watch_public.ps1` | 公网健康看门狗：连续两次失败才重启隧道（防 TUN 闪断误判） |

---

## 验证辅助脚本（不属于产品代码，可随时删）

| 脚本 | 作用 |
|---|---|
| `backend/_guardprobe_security.py` | 破坏安全护栏，确认对应测试会报 FAIL（数量见 [`status.md`](status.md)） |
| `backend/_guardprobe_capability.py` | 破坏能力层护栏（迁移 / 回收 / 节点策略 / 运行存储 / checkpoint / 遮蔽 / 重启恢复 / 调度与事件触发 / **导出依赖 / SSE 线格式 / 级联删除**），同上 |
| `backend/_check_docs.py` | `docs/` 的四项一致性：相对链接有效、**每份文档从某个索引可达**、锚点有定义（`<a id>` 与标题锚都认）、`current/status.md` 的数字与实测一致。**已是维护流程的一环**，见 [`../README.md`](../README.md) §11 |
| `backend/_guardprobe_docs.py` | 破坏 `_check_docs.py` 的每一项，确认它真的会报。**改的是 `docs/` 的副本，不碰真源码** —— 所以没有同族脚本那个"被中断需先恢复"的问题 |
| `backend/_restore_guardprobe.py` | **中断恢复**：对比 sidecar 备份与真实源码，不同就恢复 |

> 注意： 前两个会**临时修改源码**。它们先写 sidecar 备份，正常结束才清理；
> **被强杀时不会自动恢复**。
>
> **被中断后第一步是恢复，不是跑测试** →
> [`../lessons/process.md`](../lessons/process.md) §探针被中断
>
> `_guardprobe_capability.py` 支持按测试名过滤，迭代时用：
> `python _guardprobe_capability.py test_run_retention.py`

---

## 数据目录（`data/workflows/` 进仓库，其余运行时产生、不在仓库里）

| 路径 | 内容 |
|---|---|
| `data/scaffold.db` | SQLite（WAL）：`workflows` / `workflow_versions` / `runs` / `run_steps` / `schedules` / `alembic_version` |
| `data/checkpoints.db` | **只在 `CHECKPOINT_STORE=sqlite` 时存在**。LangGraph 自己的 schema，归它管 |
| `data/workflows/<id>.json` | 工作流定义（git 友好，灾备副本） |
| `data/runs/<id>.json` | 每次运行的完整事件 transcript |

> `DATA_DIR` 可覆盖根目录。注意： **用完记得清掉环境变量** →
> [`../env/tooling.md`](../env/tooling.md) §环境变量泄漏

---

## 加一个新文件该放哪

| 你要加 | 放哪 | 别忘了 |
|---|---|---|
| 一种新节点类型 | `app/nodes/<name>.py` | 在 `nodes/__init__.py` 加一行 import（否则不注册） |
| 一个新内置工具 | `app/tools/builtin.py` | 它是注册表驱动的，加 `register_tool` 即可 |
| 一条新校验规则 | `app/graph/validate.py` | **必须双语**：`issues.error(code, msg, msg_zh)` |
| 一个新能力接口 | `app/runtime/<name>.py` | 照能力层统一形状：ABC + 默认实现 + `get/set` + `build` |
| 一个新 API 路由 | `app/api/<area>.py` | 默认需要认证（中间件），公开路径要加进 `PUBLIC_PATHS`（`app/security/auth.py`） |
| 一个新前端组件 | `frontend/src/<area>/` | 文案要进 `i18n/translations.ts`（漏了编译不过） |
| 一个新测试 | `backend/tests/test_<area>.py` | 新守卫要**同时**加反向验证用例 |

---

## 相关

- 每一层的实现细节 → [`../reference/backend-layers.md`](../reference/backend-layers.md)
- 架构与机制 → [`architecture.md`](architecture.md)
- 启动/测试命令 → [`../env/run.md`](../env/run.md)
