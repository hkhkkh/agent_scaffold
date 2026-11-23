---
title: 架构
read_this: 需要理解"各部分怎么配合"时
detail: reference/backend-layers.md（逐层实现）
---

# 架构

> 本文件是**概览** —— 各部分怎么配合。
> 逐层的实现细节 → [`../reference/backend-layers.md`](../reference/backend-layers.md)

---

## 核心主张

> **画布是编辑器，LangGraph 是执行引擎，两者解耦。**
> 节点写共享状态 `vars[node_id]`，边只表达依赖关系。
> 所以画布上的一条连线不会变成一句代码，而是变成一次状态写入。

这条决定了几乎所有设计。

---

## 1. 单一事实源

```
backend/app/graph/schema.py  (Pydantic)
        │
        ▼  GET /api/node-types
frontend/src/types/workflow.ts   (TS 镜像)
        │
        ▼
通用渲染：NodePalette / Inspector / SchemaForm
```

**关键性质**：前端**没有任何按节点类型定制的渲染代码**。
Inspector 的表单完全由后端的 `ParamSpec` 判别联合驱动
（唯一例外：运行对话框预填 Start 节点 `input_schema` 的 `runInputTemplate`，
`App.tsx` 按 `'start'` 类型查找入口节点）。

> **加一种节点类型 = 只改后端一个文件**（写 module + `@register_node`
> + 在 `nodes/__init__.py` 加一行 import），**前端零改动**。

**代价**：`workflow.ts` 是**手工镜像**，会静默漂移。
症状是「某个 Inspector 字段永远不显示」，极难回溯到原因。

→ 待做：`scripts/check-schema-drift.ps1`（见 [`limitations.md`](limitations.md) §3）

---

## 2. 节点系统

**21 种节点，全部实测可跑。** 注册表驱动。逐节点的参数与关键点 →
[`../reference/backend-layers.md`](../reference/backend-layers.md) §2

> 注意： **`retriever` 是 BM25，不是向量检索。**
> **不要在 UI 或文档里含糊成「语义检索」** —— 它无法匹配同义词（「车」vs「汽车」）。
> 向量检索需要新节点 + `Embeddings` 端口，属 Phase 3。

---

## 3. 编译器

三条设计决策（都写在 `compiler.py` 文件头 docstring）：

1. **节点写共享状态，边只表达依赖** ——
   边传值解决不了跨分支/非相邻/循环累积
2. **分支节点编译成条件边** —— Condition/Loop 会**真正执行**，
   所以决策会出现在时间线里（值得多花一个 super-step）
3. **多条边进同一个 input handle → 收成列表** ——
   这才让 Aggregator 能合并互斥分支

**节点级超时（2026-09-14 已实现）**：`_make_node_fn` 里的节点调用已包上
`runtime/attempts.py` 的执行策略 —— 每个节点有 deadline（超时/重试默认值见
[`../reference/backend-layers.md`](../reference/backend-layers.md) §5）。
→ [`../log/phase-2-3-node-timeout.md`](../log/phase-2-3-node-timeout.md)

---

## 4. 运行时

- `RunManager` 管理一次运行（编译、执行、持久化、回收）
- 运行历史经 `RunStore` 接口（[`../log/phase-2-4-run-store.md`](../log/phase-2-4-run-store.md)）——
  **`import app.runtime.manager` 不加载 sqlmodel / fastapi**
- 事件经 `EventBus` 广播 → `GET /api/runs/{id}/events` 走 **SSE**
- 前端 SSE 是**手写 fetch 流解析**（`api/client.ts` 的共用 `streamSse`）——
  原生 `EventSource` 不能设 header、不能读 body、不能发 POST、不能传 `after` 序号。
  三个消费者：`streamRun`（运行事件，可重放）、`streamGenerateGraph`
  （生成过程，每步一份完整图快照；最后一帧与非流式返回一致）与 `streamCopilot`
  （理解/方案/编辑/试跑的阶段事件，同样在变更后推完整快照）

**运行的生命周期**：`release()` 在终点放掉重资源，
`prune()` 按年龄+数量淘汰整个 context →
[`../log/phase-2-2-retention.md`](../log/phase-2-2-retention.md)

---

## 5. 能力层（[重要] 架构心脏）

**「一个底层」的物理形态。** 统一形状：

```
ABC + 默认实现 + 进程级 get_*/set_* 注入点 + 从 Settings 构造的 build_*
```

各接口的默认实现 / 生产实现 / 调用点（`EgressPolicy` · `SandboxRunner` ·
`ToolRegistry` · `ModelGateway` · `SecretProvider` · `RunStore`）→
[`../reference/backend-layers.md`](../reference/backend-layers.md) §9

**27a 新增的两个注入点**（同一形状，用于切断 runtime 对平台的引用）：
`runtime/workflows.py` 的 `set_workflow_loader` / `has_workflow_loader` / `load_workflow`
（`manager.py` 的 `hydrate` 用它取定义），以及 `runtime/runs.py` 的
`set_database_run_store`（`main.py` 用它装入 `SqliteRunStore`）。

**判定不抽**（已复核两次）：`runtime/template.render`（6 节点）、
`runtime/expr.evaluate`（2 节点）—— **纯函数、无 I/O、无资源、无外部状态**，
抽了只是多一层间接。
`runtime/attempts.py` 也**不抽** —— **只有一个实现**，注入点是仪式。

> **为什么必须是接口而不是「在每个节点里写 if」**：
> 加一个新节点类型就要重新想一遍所有横切关注点 → **必然遗漏**；
> 想从本地切到生产 → 要改 N 个地方，且没有编译器帮你检查。

实现记录（含真 bug 与教训）→ [`../log/phase-1-*.md`](../log/)

---

## 6. 一次运行的数据流

```
POST /api/runs (或 /adhoc)
      │
      ▼
RunManager.create()  ← 编译图（失败 → HTTP 422，用户看得到）
      │                （编译期失败不给背景任务，避免静默失败）
      ▼
RunManager.start()   ← 背景 asyncio task
      │
      ▼
compiler 的 _make_node_fn（每个节点一个闭包）
      │  ├─ 收集 inputs（读前驱的 vars）
      │  ├─ merge tweaks（per-run 配置覆盖）
      │  ├─ emit("node_started")
      │  ├─ await run_with_policy(...)    ← 超时/重试策略见 backend-layers §5
      │  ├─ 结果写进 vars[node.id]
      │  └─ emit("node_finished")
      ▼
EventBus.emit() ──► SSE 订阅者（重放 + 增量）
      │                └─ 前端 applyRunEvent → 投影到节点卡片
      ▼
_run 结束 → _persist_finish()（经 RunStore 写 Run/RunStep 行 + transcript 文件）
         → _retire()（release 重资源 + prune 淘汰）
```

**注意顺序**：`_persist_finish` **必须**在 `_retire` **之前** ——
前者要读 `bus.history()`，后者会裁掉它。
调换这两行，每次长运行的开头都会从 transcript 里消失。

---

## 7. 认证

**核心规则（单向）**：认证关闭，当且仅当可证明它不必要 ——「不必要」只有一个含义，
即服务器绑定在回环地址上。`AUTH_MODE=local` + 绑定 `0.0.0.0` 会自动升级为 token
模式；无 token 则拒绝启动。实现用**中间件而非逐路由依赖**（默认关闭 `/api/*`）。

详情（含第二道防御与 scope 拦截）→ [`security.md`](security.md) §2

---

## 相关

- 逐层实现细节 → [`../reference/backend-layers.md`](../reference/backend-layers.md)
- 设计决策的理由 → [`../reference/design-decisions.md`](../reference/design-decisions.md)
- 目标架构（三层） → [`../target/vision.md`](../target/vision.md)
- 文件清单 → [`files.md`](files.md)
