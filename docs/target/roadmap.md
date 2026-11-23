---
title: 路线图
read_this: 决定做什么 / 判断进度 / 要知道依赖顺序时
owner_of: [重要] 进度、里程碑、任务编号、Phase 顺序的唯一归属地
---

# 路线图

---

## 1. [重要] 进度尺：五个里程碑

任务数和代码倍数都会给出**模糊的百分比**。这把尺子回答的是
「**现在能交付什么**」：

| 里程碑 | 判据 | 状态 |
|---|---|---|
| **M1** 能画出一个跑得起来的 agent | 拖节点 → 写描述 → AI 补齐 → 跑起来看每步 I/O | [x] **已达成** |
| **M2** 能画**复杂** agent | 有可复用单元（子流程），而不是只能摊成一张巨图 | [x] **已达成**：任务 30（图组合，编译期内联）+ 任务 28（定义版本化，每次保存留不可变快照）。**顺带修掉一个真实缺陷**：`definition_digest` 把节点坐标算进了哈希，所以**在画布上拖动一个节点**就会让所有引用它的父图报「子图已改」并拒绝运行 |
| **M3** 平台能**扛住批量** | 一个 agent 卡死/超支不拖垮其他 | [x] **已达成**：18 · 2.6 · **15** · **17** 全部完成 |
| **M4** agent 能**拿出去独立部署** | 导出后不靠本平台就能跑，且底座可查证未被改 | [~] **分层 + 封条已完成**（2026-10-08）：runtime 无一行平台引用（AST 门禁实测）、源码 wheel 构建成功、封条 wheel 全部带哈希且篡改被拒（wheel 计数见 [`../current/status.md`](../current/status.md)）；**31 已完**。**只剩任务 29 的收尾** —— 待决 1 已于 2026-10-08 定为「B · 导出即源码」，仍缺项见 §10 |
| **M5** agent 能**自己长时间干活** | 记忆 + 调度 + 预算护栏 | [x] **已达成**（2026-10-08）：记忆（19）· **自动写入（20，实测通过）** · 目标追踪（21）· 定时与事件触发（22）· 预算护栏（23）· Supervisor（24）→ [`../log/phase-3-2-auto-memory.md`](../log/phase-3-2-auto-memory.md) |

> **5 个里达成 4 个**（M1 · M2 · M3 · M5）。只剩 M4：
> M4 卡在运行时做成可校验的包（它曾**卡在环境上**，见 §10），
> 现在只差任务 29 的收尾 —— 见 §12 的下一步。
>
> 差异的原因：M1 是"铺管线 + 定形状"，M2–M5 才是主体。
> 而 M5 的核心问题（目标分解 / 何时该停 / 该记住什么）**业界都没有完全解决**。

### 其他口径（供参考，不如里程碑有用）

| 口径 | 数字 | 说明 |
|---|---|---|
| 任务数 | 已完 29 / 32 ≈ **91%** | 任务粒度不均。数法（**手工，逐 Phase 点**，子号并回父号：`14b`→`14`、`22a/b`→`22`、`27a/b/c`→`27`）：<br>Phase 0 `1–6` = 6 · Phase 1 `7–12` = 6 · Phase 2 `13~2.6` = 6 + `13` 半 · Phase 2.5 `30/28` = 2 · Phase 3 `19/20` = 2 · Phase 4 `21–24` = 4 · Phase 5 `25/26` = 1 · Phase 6 `27/29/31` = 2<br>→ **29 全完 + `13` 一个半**。<br>**注意：这个数字无法用脚本从表格推导** —— 状态标记散落在三处（行内、Phase 标题、正文），没有统一列。曾写过一个脚本，它据此误判（报 92%），已删除。**改完任务请照上表重点** |
| 差距清单（实测） | ~2 / 15 ≈ **13%** | 严格看真正关掉的 → [`gaps.md`](gaps.md) |
| 代码量 | **10–20 倍**（估计） | 基于「Phase 0 ≈ 2000 行」推算，**非实测** |

---

## 2. 任务索引

**改哪些文件 → [`../code/README.md`](../code/README.md)**（逐任务索引）

| Phase | 任务 | 状态 |
|---|---|---|
| **0** 安全地基 | 1–6 | [x] **全部完成**（2026-09-11） |
| **1** 能力层 | 7–12 | [x] **6/6** —— 任务 12（安全/认证文案 i18n）已完成 |
| **2** 持久化与执行 | 13–18 | [~] **7/7 前端** —— 13 的 alembic [x]、14 [x]、**14b [x]**、**15 [x]**、16 [x]、**17 [x]**、18 [x]、2.6 [x]；只剩 13 的 Postgres 后端 |
| **2.6** `RunStore` | 2.6 | [x] **已完成**（2026-09-14）→ [`../log/phase-2-4-run-store.md`](../log/phase-2-4-run-store.md) |
| **2.5** 图组合 | 30 + 28 | [x] **30 [x] · 28 [x]** |
| **3** 记忆 | 19–20 | [x] **19 [x] · 20 [x]**（自动写入 2026-10-08 实测通过，见 §7）→ [`../log/phase-3-1-five-cuts.md`](../log/phase-3-1-five-cuts.md) |
| **4** 自主与调度 | 21–24 | [x] **4/4** —— 21 [x] · **22 [x]**（22a 定时 / 22b 事件）· 23 [x] · 24 [x] |
| **5** 可观测 | 25–26 | [~] **25 [x]**；26（审计日志）未做 |
| **6** 打包交付 | 27a/b/c · 29 · 31（28 已提到 2.5） | [~] **31 [x] · 27a/b/c [x]**（2026-10-08）；29 仍开 |

---

## 3. Phase 0 · 安全地基 [x] 已完成（2026-09-11）

| # | 任务 | 状态 |
|---|---|---|
| 1 | 认证/授权（不透明 token + scope + 默认安全） | [x] |
| 2 | 子进程沙盒（真能 kill、真限内存） | [x] |
| 3 | 修 `gen_graph.py` 自动 trusted | [x] |
| 4 | 修凭据外泄（`base_url` 覆盖不继承 key） | [x] |
| 5 | `http_request` SSRF 防护 | [x] |
| 6 | 修导入 id 路径遍历 | [x] |

**反向验证的计数与「本次未重测」的 caveat 见 [`../current/status.md`](../current/status.md)**（数字归那里）。
详情 → [`../current/security.md`](../current/security.md) ·
[`../log/phase-0-security.md`](../log/phase-0-security.md)

---

## 4. Phase 1 · 能力层 [x] 已完成（架构心脏，改一次定十年）

| # | 任务 | 状态 | 说明 |
|---|---|---|---|
| 7 | 定义全部能力接口 + 本地实现 | [x] | Phase 1 范围内 5 个接口就位；第 6 个 `RunStore` 见 §5 任务 2.6 |
| 8 | **节点改为只依赖接口** | [x] | `code` / `tool` / `agent` / `llm` 已改完 |
| 9 | 补 `SandboxRunner` 接口 | [x] | [`../log/phase-1-1-sandbox.md`](../log/phase-1-1-sandbox.md) |
| 10 | `ModelGateway` 统一模型调用 | [x] | [`../log/phase-1-3-gateway.md`](../log/phase-1-3-gateway.md) |
| 11 | `SecretProvider` 抽象 | [x] | [`../log/phase-1-4-secrets.md`](../log/phase-1-4-secrets.md) |
| — | `ToolRegistry` | [x] | [`../log/phase-1-2-tools.md`](../log/phase-1-2-tools.md) |
| 12 | 安全/认证错误文案接入 i18n | [x] | **约 20 行** → [`../log/phase-1-5-security-i18n.md`](../log/phase-1-5-security-i18n.md) |

### 五个已完成的接口

| 接口 | 默认实现 | 生产实现 | 调用点 |
|---|---|---|---|
| `EgressPolicy` | `NetworkEgressPolicy` | 白名单 + 代理 | `tools/builtin.py` |
| `SandboxRunner` | `LocalSandboxRunner` | `ContainerSandboxRunner` | `code.py` |
| `ToolRegistry` | `BuiltinToolRegistry` | `ScopedToolRegistry` | `tool.py`、`agent.py` |
| `ModelGateway` | `LangChainModelGateway` | 配额/成本/路由 | `llm.py`、`agent.py` |
| `SecretProvider` | `SettingsSecretProvider` | `MappingSecretProvider` | `provider.py` |

**统一形状**（新增接口照这个做，**别另发明**）：

```
ABC + 默认实现 + 进程级 get_*/set_* 注入点 + 从 Settings 构造的 build_*
```

**判定不抽**：纯函数（无 I/O、无资源、无外部状态）——
`runtime/template.render`（6 节点）、`runtime/expr.evaluate`（2 节点）。

> **为什么这一步必须在 Phase 2 之前**：Phase 2 要换存储后端、加队列。
> 如果那时节点还在直接 import 具体实现，就要在改存储的同时改所有节点。

---

## 5. Phase 2 · 持久化与执行 ← **进行中**

**依赖**：Phase 1（[x] 已满足）

| # | 任务 | 状态 | 文件 |
|---|---|---|---|
| 13 | Postgres + **alembic 迁移** | [~] alembic [x] / Postgres [ ] | [`../code/task-13-postgres.md`](../code/task-13-postgres.md) |
| 14 | 持久化 checkpointer → **重启可恢复** | [x] | [`../log/phase-2-5-checkpoints.md`](../log/phase-2-5-checkpoints.md) · [`../log/phase-2-7-durable-default.md`](../log/phase-2-7-durable-default.md) |
| **14b** | **重启后的 resume**（把持久化接到端点上） | [x] | [`../log/phase-2-8-resume-after-restart.md`](../log/phase-2-8-resume-after-restart.md) |
| 15 | 任务队列 + worker pool + **并发上限** | [x] | [`../log/phase-2-10-queue-budget-export.md`](../log/phase-2-10-queue-budget-export.md) |
| 16 | 修 `_runs` 无限泄漏 | [x] | [`../log/phase-2-2-retention.md`](../log/phase-2-2-retention.md) |
| 17 | 配额（token / 花费 / 墙钟 / 步数） | [x] | [`../log/phase-2-10-queue-budget-export.md`](../log/phase-2-10-queue-budget-export.md) |
| 18 | 通用节点超时 + 重试策略 | [x] | [`../log/phase-2-3-node-timeout.md`](../log/phase-2-3-node-timeout.md) |
| **2.6** | [x] **`RunStore` 接口** → [`../log/phase-2-4-run-store.md`](../log/phase-2-4-run-store.md) |

**已完成的五项**：

- **alembic**：`ensure_schema()` 是唯一入口 —— 已管理的库 `upgrade head`，
  其它 `create_all()` + `stamp head`（**收养**）。
  **双路径一致性守卫**保证两条路建出的 schema 相同。
  已有部署**平滑升级且数据零丢失**（实测 13 工作流 / 30 运行 / 122 步骤保留）。
- **运行回收**：结束即释放 checkpointer（主犯：每个 super-step 一份 state）；
  整个 context 按**年龄 + 数量**双上限淘汰；判据集中在**一个字段** `retired_at`。
- **节点执行策略**：`runtime/attempts.py`。超时**默认开**（限流，不改任何能在预算内跑完的图的结果）；
  重试**默认关**（重复副作用，必须显式要求）。Executor 比节点自己的 deadline 多等 5s，
  让更具体的错误先报。
- **运行存储接口**：`runtime/runs.py` + `db/run_store.py`。**`import app.runtime.manager`
  不再加载 sqlmodel / fastapi** —— 这是任务 27a 的硬前置。三个实现：
  `sqlite`（默认，平台）/ `memory`（测试）/ `null`（导出的 agent）。
- **重启后的 resume**：`RunManager.hydrate` 从 `Run` 行 + `Workflow` 定义 +
  checkpoint 重建 context，`resume` 找不到就退回它。顺手挖出并修掉一个**真实 500**：
  SQLite 读回的 datetime 丢时区，混进 live 集合后 `GET /runs` 排序抛 `TypeError`。

---

## 6. [重要] Phase 2.5 · 图组合（子流程）← **原路线图的遗漏，必须补**

**依赖**：Phase 1（[x]）。**与任务 28 强耦合。**

> **这一节是 2026-09-14 新增的。原路线图 29 个任务里没有任何一条覆盖它。**
>
> 搜「子流程 / 子工作流 / 复用 / 组合 / 嵌套 / subgraph」在原文档里**只有 1 处命中**，
> 而且那是一句形容 `events.py` 的注释。
> **这个洞是用户追问「能不能画非常复杂的 agent」时才暴露的 —— 而它正是那个问题的答案。**

| # | 任务 | 说明 |
|---|---|---|
| 30 | [x] **图组合**（见 `../log/phase-2-9-subgraphs.md`）：把一段图作为可复用单元嵌入另一张图 | 编译期嵌套，不是运行时调用 |
| 28 | [x] **定义版本化**（2026-09-18）—— 每次保存写一行不可变快照 `workflow_versions`；`GET /workflows/{id}/versions[/{n}]`；哈希与子图钉住共用同一个函数。 | （见上） **从 Phase 6 提前到这里** —— 它是 30 的前提 → [`../log/phase-2-5-definition-versioning.md`](../log/phase-2-5-definition-versioning.md) |

### 为什么它是「复杂 agent」的天花板

今天一座图**无法被拆开**。后果很具体 —— 假设要做「合同审阅」：
抽取条款 → 检索法条 → 风险评级 → 生成意见书 → 人工复核。

其中「检索法条」在另外三个 agent 里也要用。今天只有两个选择：

| 选择 | 代价 |
|---|---|
| 每个 agent **重新画一遍** | 改一处要改 N 处，且会逐渐不一致 |
| 画成一张 200 节点的巨图 | 无法维护、无法单独测试、无法单独改 |

> **没有组合能力，「快速产出复杂 agent」就退化成「快速复制粘贴」。**
>
> 这一条比打包更根本：打包只决定能不能拿出去，**组合决定能不能画得出来**。

### 它**不是**任务 24（这两个经常被混为一谈）

| | 任务 30 图组合 | 任务 24 子 agent 委派 |
|---|---|---|
| 发生时机 | **编译期** | 运行时 |
| 子单元是 | 同一张图里的一段 | **独立部署的服务** |
| 形态 | 子图被**内联**进父图 | A 通过协议调用 B |
| 解决 | **复杂度与复用** | 分工与并行 |

**两者都要做，且互不替代。** Phase 3/4 的自主 agent 需要 24；
用户「画复杂 agent」需要 30。

### [重要] 设计选择：引用 + 版本锁定（**不是**内联展开）

| | 引用 + 版本锁定 | 内联展开（复制一份进来） |
|---|---|---|
| 改子图 | 引用者**不**受静默影响 | 引用者不受影响 |
| 复用维护点 | **一处** | 各自一份 |
| 需要什么 | **版本化**（任务 28） | 什么都不需要 |
| 风险 | 版本不固定时会静默改掉所有引用者 | 每个父图各持一份拷贝 |

**选前者。** 内联展开看着更简单，但它只是把「改一处要改 N 处」
**换个地方保留下来**：子图本身是单一维护点了，可**每个父图仍各持一份拷贝** ——
于是改子图依旧要手工同步到 N 个父图，而且这次连「它们本该一致」这件事都是不可见的。

> **这意味着任务 30 和 28 不拆开做。** 引用关系一旦存在，
> 「改子图会静默改变所有引用它的 agent」就是最大的坑，
> 而版本锁定是唯一的解法。

---

## 7. Phase 3 · 记忆 [x] 完成（19 [x] · 20 [x]）

**依赖**：Phase 2（需要持久化）

| # | 任务 |
|---|---|
| 19 | 三层记忆：工作（运行内）/ 情景（发生过的）/ 语义（知识），带检索 | [x] |
| 20 | [x] 记忆节点 + **自动写入** —— `RunManager._remember_if_asked`，`MEMORY_AUTO_WRITE` 开关（默认关）。**2026-10-08 实测通过**。**未接 `langmem`**（依赖膨胀 + 0.0.x 一年未更新），记为升级路径 | [x] |

> 注意： **「什么该记住」是研究问题，不是配置问题。**
> 建议先做**显式**记忆节点（用户/AI 主动写），自动写入策略留到有真实使用数据之后。

**为什么 Phase 2 必须先做**：记忆**无处存放**（重启即丢）。

---

## 8. Phase 4 · 自主与调度 [x] 完成（21 [x] · 22 [x] · 23 [x] · 24 [x]）

**依赖**：Phase 3

| # | 任务 |
|---|---|
| 21 | 持久化目标 / 任务追踪（自主 agent 的「我在干什么、干到哪了」） | [x] |
| 22 | `Scheduler`：定时唤醒 + 事件唤醒 | [x] |
| 22a | 定时：APScheduler 在 lifespan 里起停，到点起一条普通 run；工作流被删则 `orphaned` 不删除 | [x] |
| 22b | 事件：`POST /api/hooks/<token>` 起同一条 run 路径；token 即凭证，禁用的 hook 与不存在的 hook 答案相同 | [x] |
| 23 | **预算护栏与熔断** ← 没有这个，自主 agent 就是烧钱机器 | [x] |
| 24 | Supervisor 循环节点 + 子 agent 委派 | [x] |

**为什么 Phase 3 必须先做**：自主 agent 没有上下文，退化成「**重复劳动机器**」。

---

## 9. Phase 5 · 可观测与运维

**可与 Phase 4 并行**

| # | 任务 |
|---|---|
| 25 | [x] **四件齐了**（2026-10-08）：Trace ID 贯穿（W3C `traceparent`）· token/成本统计（Phase 1 已有）· 结构化日志（加了 trace 列；JSON 输出未做）· **`/metrics` 已加**（JSON 格式，非 Prometheus 文本 —— 避免新依赖）→ [`../log/phase-5-1-trace-ids.md`](../log/phase-5-1-trace-ids.md) |
| 26 | 审计日志（谁改了什么、谁跑了什么） |

---

## 10. Phase 6 · 打包交付

**依赖**：Phase 1（[x]）+ **Phase 2.5**（任务 28 已提前到那里）

> [重要] **2026-09-14 重写**：原表只有 3 行，其中任务 27 只写对了一半。
> 「runtime 抽成独立 pip 包」**没有说清怎么进 agent 包、怎么保证没被改**
> —— 而那正是「所有 agent 同一个底层」能否成立的关键。现在拆成 27a/27b/27c。

| # | 任务 | 说明 | 状态 |
|---|---|---|---|
| **27a** | **runtime 抽成独立包** | [x] **已完成**（2026-10-08）。`app/ai/` 已劈开：`gateway`/`provider`/`messages` → `runtime/ai/`，其余 5 个 → `ai_buildtime/`。两处 `from app.db` 改为注入（`set_workflow_loader` / `set_database_run_store`，照 `subgraphs.py` 形状），并加了 AST 门禁。详见 [`../log/phase-6-2-runtime-split-seal.md`](../log/phase-6-2-runtime-split-seal.md) | [x] |
| **27b** | **构建时发布** | [x] **源码 wheel 构建已实测**（`backend/scripts/build_vendor.py`）。发布到私有源 / 基础镜像仍未做 —— 本机可离线封条，故不作为 29 的前置 | [~] |
| **27c** | [重要] **导出器生成带封条的依赖** | [x] **实测**：全部 wheel（0 sdist）每条 pin 带 sha256，篡改 1 字节即被 pip 拒绝（wheel 计数见 [`../current/status.md`](../current/status.md)）。导出器在锁文件存在时写 sealed `requirements.txt` | [x] |
| 29 | 导出独立部署包 | [~] **主要形态齐了**（2026-10-08）：**`agent.yaml` + `!ref`**（原设计的 YAML 形态）、逻辑外置（`prompts/*.md` · `code/*.py`）、`manifest.yml`。**待决 1 已定为 B**。仍开：`config/capabilities.yaml`、`models.yaml`、`prompts/partials/`、lock 的镜像 digest | [~] |
| **31** | [重要] **agent 自带业务工具** | 工具随包走，而不是进程级 `register_tool()` | [x] |

**2026-09-18 实测发现的一个前置**：

| 发现 | 影响 |
|---|---|
| `langgraph` 在本机是**源码 / 可编辑安装**（`H:\agent_1\langgraph`） | **27c 不能直接封装 editable 安装**。先从源码树构建 wheel；PyPI 同版本 wheel 缺少本机的子图检测修复。依据与快照 → [`reuse.md`](reuse.md) §1.36 |

**三条必须写进代码或 CI 的约定**：

| # | 约定 | 为什么 |
|---|---|---|
| 1 | **升级 runtime = 重新导出**，不允许手改 runtime 文件 | 否则哈希校验形同虚设。改 runtime 是**改模子**（属于我们），不是适配 |
| 2 | **CI 必须有一个任务：导出 agent，在空 venv / 干净镜像里跑起来** | 缺这一步时 runtime 会静默反向依赖平台（`manager.py` 的 `from app.db`）—— 出处与修法见 [`../log/phase-2-4-run-store.md`](../log/phase-2-4-run-store.md) |
| 3 | 导出时**强制哈希 / 固定 digest**，缺了就失败 | 让「相同底层」可验证，而不是靠人记得检查 |

> **更准确的定位**：任务 27 **不是"打包"，是"把模具和试模机分开"**。
> 它排在最后，就意味着在这个边界长满平台假设之后才去拔它们。

**为什么它仍然不能先做**（技术可行、意义上不行）：

一个不能持久化上下文、一个节点卡死就永久卡死、一个业务方的 key
所有 agent 共用的东西，**导出来也不是业务级 agent**。

---

## 11. [重要] 依赖链（为什么不能跳步）

```
Phase 1 ──► Phase 2 ──► Phase 2.5 ──► Phase 6
   │            │            │
   │            ▼            │
   │        Phase 3 ──► Phase 4 ──► Phase 5
   │                        │
   └────────────────────────┘（Phase 6 只依赖 1，但"意义上"依赖 2/2.5）
```

| 跳过 | 后果 |
|---|---|
| Phase 1 | Phase 2 换存储时要**同时改所有节点** |
| Phase 2 | Phase 3 的记忆**无处存放**（重启即丢） |
| **Phase 2.5** | 只能画出扁平巨图，「复杂 agent」**停在复制粘贴** |
| Phase 3 | Phase 4 的自主 agent 没有上下文，退化成**重复劳动机器** |
| Phase 4.23（预算护栏） | 自主 agent 会**烧钱** |
| **把 Phase 6 提前** | 技术可行，但把一个**跑不住、画不复杂**的核心导出去 |
| **任务 2.6（`RunStore`）** | 任务 27 做不成，因为 `manager.py` 会把平台拖进 runtime 包 |

---

## 12. 建议的执行顺序

**按里程碑选，不要只看任务编号。**

| 顺序 | 做什么 | 状态 | 为什么是这个顺序 |
|---|---|---|---|
| **1** | **M3**：18 · 2.6 → 15 → 17 | [x] **已完成** | 一个 agent 卡死或超支不得拖垮其他；这是「批量」的**定义** |
| **2** | **M2**：任务 30（图组合）+ 28（版本化） | [x] **已完成** | 用户「画复杂 agent」的直接答案；两者必须一起做 |
| **3** | **M4**：任务 27a/b/c → 31 → 29 | [~] **31 [x] · 27a/b/c [x]**；29 主要形态已完成（仍缺项见 §10） | 1+2 之后才有意义，否则导出的是空壳 |
| **4** | **M5**：任务 19/20 → 21–24 | [x] **已完成**（20 于 2026-10-08 实测通过，见 §7） | 研究问题，且是最大的量 |

**下一步**：**M4 的收尾（任务 29）**；27a/b/c 与环境前置**都已解决**：

> [2026-11-03] 能力面的工作另有路线：**P0 能力包底座 + files 包 + 导出侧会话/HTTP 聊天
> 已完成**（Excel → 对话 → 导出 → 独立 `serve --http` 实测跑通）。能力路线、边界与
> 下一步以 [`capability-roadmap.md`](capability-roadmap.md) 为准；
> 本文件仍负责 M1–M5 里程碑与 Phase 任务编号。

- 27a（分层）[x]；27b（源码 wheel 构建）[x]；27c（封条）[x]。
- `langgraph` 源码安装的环境问题**已绕过**：从源码树 `python -m build --wheel` 成功，
  产物是字节码版子图检测（221 行）而非 PyPI 的源码文本版（291 行），故不换 PyPI wheel。
  详见 [`reuse.md`](reuse.md) §1.36b。
- 29 仍开的收尾项见 §10；另有一条**哈希未覆盖外置文件** ——
  客户改 `prompts/*.md` 不会被 selfcheck 发现。

（本行之前写过三个过期的“下一步”：任务 14/15、M2 收尾（任务 28）、27a 卡环境。
三者都已完成或已绕过。**改完进度请顺手改这行** —— 它已经错过三次。）

---

## 13. 代码量与难度

### 现在有多少

**文件数与行数不在本文件维护** —— 它们的唯一归属是
[`../current/files.md`](../current/files.md)（文件清单）与
[`../current/status.md`](../current/status.md)（实测数字）。
本文件只保留结构说明，**不要再抄数字**（抄了就会与那两处漂移 —— 本节曾因此过期）。

**结构性事实**（不随版本漂）：`app/` 分 **10** 个目录（`ai_buildtime` · `api` · `db` ·
`graph` · `memory` · `nodes` · `runtime` · `scheduling` · `security` · `tools`）+
顶层若干模块；`frontend/src/` 分 **11** 个目录（`ai` · `api` · `canvas` · `i18n` ·
`inspector` · `run` · `store` · `styles` · `types` · `ui` · `use`）；`docs/` 分 **7** 个目录
（`code` · `current` · `env` · `lessons` · `log` · `reference` · `target`）。
docstring 占比很高 —— 很多文件开头是整段"为什么这么设计"，
所以「代码行 / 注释行」拆开看会比总数更有信息量。

### 最难的不是基础设施，是「自主」本身

持久化、队列、沙盒、认证都是**成熟工程**，照做就行。

但「长时间琢磨」真正的难点是：

- **目标分解**（把「帮我调研 X」拆成可执行步骤）
- **什么时候该停**（判断「做完了」vs「该换方法」vs「该问人」）
- **什么该记住**（记忆写入策略）

**这三样是研究问题，不是配置问题。** 市面上所有产品在这一点上都不完全可靠。

> **本路线图会实现业界标准做法，但不应期待超越业界。**

---

## 相关

- 差距清单（快照） → [`gaps.md`](gaps.md)
- 交付物与交付机制 → [`delivery.md`](delivery.md)
- **改哪些文件** → [`../code/README.md`](../code/README.md)
- 待决 → [`decisions.md`](decisions.md)
- 当前验证状态 → [`../current/status.md`](../current/status.md)
