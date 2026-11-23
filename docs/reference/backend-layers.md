---
title: 后端逐层实现
read_this: 读代码 / 改代码时查细节
scope: 逐层实现细节（不是概览）
---

# 后端逐层实现

> 概览（各部分怎么配合）→ [`../current/architecture.md`](../current/architecture.md)
> 本文件是**逐层细节**。

---

## 目录

| § | 内容 |
|---|---|
| [1](#l1) | 契约层 `graph/schema.py` |
| [2](#l2) | 节点系统（16 种） |
| [3](#l3) | 编译器 `graph/compiler.py` |
| [4](#l4) | 校验 `graph/validate.py` |
| [5](#l5) | 运行时 `runtime/` |
| [6](#l6) | 数据库 |
| [7](#l7) | AI 子系统 |
| [8](#l8) | API 路由（58 条） |
| [9](#l9) | 能力层（统一形状） |

---

<a id="l1"></a>

## 1. 契约层 `graph/schema.py`

**这是整个项目的单一事实源。** 改了这里，一切都跟着变。

- `PortType` 枚举 **13 种** + `PORT_COLORS` 颜色表 + `ports_compatible()` 兼容矩阵
- `ParamSpec` 判别联合 **7 种**：`StringParam` / `NumberParam` / `BooleanParam` /
  `SelectParam` / `JsonParam` / `CodeParam` / `ModelParam`
  - 每个都带 `label` / `description` / `required` / `advanced` / **`show_if`（条件显示）**
  - 每个也带 `label_zh` / `description_zh`（i18n 用）
- `NodeTypeSpec`：ports + params + `dynamic_outputs` + `requires_trust` 等
- `WorkflowGraph` / `NodeSpec` / `EdgeSpec` / `NodeUI` / `StateField`
- `NodeSpec.description` = **用户写的「这里是什么功能」**，
  既是画布标注**也是 AI 输入**
- `ValidationReport`（含 `.errors` / `.warnings` 属性）
- `RunEvent` / `RunStatus` / `RunEventType` 等
- i18n 机制：`SUPPORTED_LANGUAGES` · `normalize_language()` · `localize()` ·
  `LOCALIZABLE_KEYS`

**容器节点已建模**（`is_container` / `NodeUI.parent_id`），但**前端没做**。

---

<a id="l2"></a>

## 2. 节点系统（16 种，全部实测可跑）

| type | 说明 | 关键点 |
|---|---|---|
| `start` | 入口 | 声明 `input_schema`，合并 run inputs |
| `end` | 出口 | 收集结果标 `__output__` |
| `prompt` | 纯模板 | 不调模型；`missing` 可选报错 |
| `llm` | 调模型 | 逐 token 流式；json 模式容错解析 |
| `condition` | 确定性分支 | **动态端口**：每个 branch 一个出口；`__branch__` 记决策 |
| `code` | Python 逃生口 | `requires_trust` + 全局开关；沙箱 |
| `agent` | ReAct 循环 | **手写循环**，每步进时间线；工具调用失败可回灌给模型 |
| `tool` | 确定性工具调用 | 与 agent 的区别：**不由模型选工具** |
| `retriever` | **BM25 关键词检索** | 注意： **不是向量检索** |
| `human_input` | 人工审批 | `interrupt()`；**幂等约束写在文件头** |
| `loop` | 有界循环 | **强制上限** `max_iterations`，双保险 |
| `aggregator` | 分支汇聚 | 5 种合并策略，`strict_single` 可抓错接线 |
| `memory` · `goal` | 长期记忆 / 目标追踪 | 都建在 `langgraph.store` 上，跨运行存活 |
| `supervisor` | 主管路由 | `Command(goto=...)`；**只加动态边，静态边仍会执行** |
| `subgraph` | 子流程 | 编译期内联另一张已保存的图 + 内容哈希钉住 |

> 注意： **`retriever` 是 BM25，不是向量检索。**
> **不要在 UI 或文档里含糊成「语义检索」** —— 它无法匹配同义词（「车」vs「汽车」）。

### [重要] 加一种节点类型 = 只改后端一个文件

```python
# 新增 app/nodes/my_node.py
@register_node
class MyNode(BaseNode):
    spec = MY_SPEC
    async def run(self, ctx: NodeContext) -> dict[str, Any]: ...
```

然后在 `app/nodes/__init__.py` **加一行 import**（否则不注册）。

**前端零改动** —— 面板、端口、表单全从 `/api/node-types` 来。

### `BaseNode` 与 `NodeContext`

```python
class NodeContext:
    node: NodeSpec          # 这个节点的定义
    config: dict            # 合并后的配置（tweaks 已应用）
    inputs: dict            # 来自前驱节点的输入
    state: dict             # 完整共享状态
    vars: dict              # 所有节点的输出（快捷方式）
    settings: Settings
    emit: EmitFn            # 发 chunk 事件
    log: LogFn              # 发 log 事件
```

**节点的返回值**写进 `vars[node.id]`。要标记工作流结果是
`result[OUTPUT_KEY] = ...`（`__output__`）。

---

<a id="l3"></a>

## 3. 编译器 `graph/compiler.py`

三条图语义决策（**状态是主通道 / 分支真执行再跳转 / 多边进同一 input handle 收成列表**）
的理由归 [`design-decisions.md`](design-decisions.md) §编译器与图语义，这里只记实现要点。

### 实现要点

- 事件 emitter 是**编译期闭包捕获**的，不是 contextvar
  （LangGraph 可能在自己的 task 里跑节点，contextvar 静默不传播）
- `GraphBubbleUp` 必须**原样放行**，否则 HITL 会被当成失败
- 节点函数签名**只收 `state`**，`get_config()` 取运行态
  （避免 LangGraph 签名告警）
- ⚠️ `graph/compiler.py` **有** `from __future__ import annotations`（第 27 行），
  但它的模块 docstring（第 22 行）写着「故意不加」—— **两者矛盾，尚未定夺**。
  改哪一边都可能动到 LangGraph 的签名检查，所以没有单方面改（见本刀日志 §3）

**节点级超时（2026-09-14）**：`_make_node_fn` 的节点调用包在
`runtime/attempts.py` 的策略里 —— 超时默认 **120s**、重试默认**关**。
两个陷阱（`TimeoutError` 在 3.11+ 是同一个类；别让它继承 `OSError`）
见 [`../lessons/backend.md#b10`](../lessons/backend.md#b10)。

→ [`../log/phase-2-3-node-timeout.md`](../log/phase-2-3-node-timeout.md)

---

<a id="l4"></a>

## 4. 校验 `graph/validate.py`（**26** 条规则）

> 注意： 旧文档写 11 条、后来写 24 条 —— 都是过期数字。实测唯一规则 code 共 **26** 个
> （`issues.error(` / `issues.warn(` 调用点 28 处，含重复）。

**错误级**（节选）：`unknown_node_type` · `duplicate_node_id` / `duplicate_edge_id` ·
`dangling_edge` · `unknown_source_handle` / `unknown_target_handle` ·
`incompatible_port_types` · `missing_entry` / `multiple_entries` ·
`missing_required_input` / `missing_required_param` · `untrusted_node` ·
`unbounded_cycle` · `missing_parent`

**警告级**：`orphan_node` · `unreachable_node` · `self_loop` · `unused_branch` ·
`branch_without_targets` · `missing_terminal` · `parent_order`

### [重要] 每条规则都必须中英双语

```python
issues.error(code, message, message_zh)   # 三个参数一起传
```

**所以一条规则不可能只发一种语言。** `check_i18n.py` 用 **AST 静态扫描**校验
（不依赖样例图能否触发），**已反向验证过**。

### `unbounded_cycle` 用 Tarjan SCC

不是 DFS 回边 —— 后者报哪个节点取决于**遍历起点**，
同样的图会给出**不同的错误信息**，用户会困惑。

### `missing_required_param` 的一个细节

要区分「**显式设置过**」和「从未设置」：
`param.name in node.config` 时才判空。

否则用户**显式清空**必填字段时校验会通过（因为回退到了声明默认值）。

---

<a id="l5"></a>

## 5. 运行时 `runtime/`

| 文件 | 职责 | 备注 |
|---|---|---|
| `manager.py` | 运行编排 | 编译期失败 → HTTP 422；运行期失败 → 背景任务 + SSE。带 checkpointer + `thread_id` 支持 resume。**`hydrate(run_id)` 从持久化重建 context**（任务 14b），`resume` 找不到就退回它。**不再依赖 `app.db`** —— 持久化走 `runs.py` 的接口（任务 2.6） |
| `events.py` | 有界重放总线 | 历史 2000 条 / chunk 200 条；`after=N` 支持断线重连；慢客户端被丢弃；有 `trim()` + `earliest_seq` |
| `serialize.py` | `jsonable()` + `redact()` | ① LangChain 消息 / datetime / 循环引用 / 超长截断；② 按 key 名遮蔽凭证 |
| `expr.py` | 条件表达式 | 走 **AST 白名单**，**不是 `eval`**。文件里写明「这是正确性护栏，不是安全边界」 |
| `sandbox.py` | Code 节点执行 | 子进程 + Job Object + AST 守卫。**明确写了不是硬安全边界** |
| `sandbox_child.py` | 沙盒子进程入口 | 第一件事是**阻塞读 stdin**，确保限制先生效 |
| `runner.py` | `SandboxRunner` 接口 | 本地子进程 / 容器两个实现，**失败即关闭** |
| `egress.py` | 出网策略 | [重要] **能力层第一个成员** |
| `template.py` | 模板渲染 | **纯函数** |
| `attempts.py` | 节点执行策略 | `AttemptPolicy` + `run_with_policy`。超时默认开、重试默认关。**不做成第 7 个能力接口** —— 只有一个实现 |
| `runs.py` | `RunStore` 接口 | 三个实现：`sqlite`（平台侧 `db/run_store.py`）/ `memory` / `null`。**`app/runtime/` 里模块级不 import `app.db`**。`load()` 与 `summary()` **分开**：后者是线上形状 |
| `checkpoints.py` | `CheckpointerProvider` | [重要] **不定义新接口** —— 上游的 `BaseCheckpointSaver` 就是。这里只管两件上游不知道的事：**哪个 saver**（配置）与**它的生命周期**（每进程 vs 每 run）。默认 `sqlite` |

### 运行的生命周期（[重要] 顺序是承重的）

```python
finally:
    context.finished_at = ...
    context.bus.close()
    self._persist_finish(context)   # ← 先写（要读 bus.history()）
    self._retire(context)           # ← 后裁（会丢掉早期历史）
```

**调换这两行，每次长运行的开头都会从 transcript 里消失。**

---

<a id="l6"></a>

## 6. 数据库

- SQLite（WAL 模式）+ JSON 列存图，**不做表范式化**（图永远整体读写）
- `Workflow` / `WorkflowVersion` / `Run` / `RunStep` / `Schedule` **五张表** + `alembic_version`
  （`models.py` 里 `table=True` 的类；`WorkflowVersion` 供任务 28 的定义版本化，`Schedule` 供任务 22）
- **`runs.workflow_id` 故意不是外键** ——
  草稿图未保存也能运行；删除时显式级联
- `Workflow` 与 `Run` **没有 ORM 关系**（外键不存在，SQLAlchemy 推不出 join）
- `models.py` **故意不加** `from __future__ import annotations`，
  且用 `List[...]` / `Optional[...]`（否则 SQLModel 解析不出泛型）
- 同时导出 `data/workflows/{id}.json`（git 友好）
  + `data/runs/{id}.json`（完整事件 transcript）
- `repository.py` 的删除逻辑**显式清理** runs / steps

### 迁移

`db/migrate.py` 的 **`ensure_schema()` 是唯一入口**：

| 库的状态 | 做什么 |
|---|---|
| 已被 alembic 管理 | `upgrade head` |
| 其它任何情况 | `create_all()` + `stamp head`（**收养**） |

**为什么两条路径并存**：

1. 已有数据库**没有 `alembic_version` 行**（它们由 `create_all` 建的），
   对它们跑 `upgrade` 会去建已存在的表 → 失败
2. 测试每个用例建一个临时库；每个都跑迁移会让套件明显变慢，
   而测试要的是**当前** schema，不关心怎么到达的

**让它是「正确」而不是仅仅「方便」的**，是
`tests/test_migrations.py::TestTheTwoPathsAgree` ——
两条路径各建一个库，**断言结果完全相同**。

> 模型改了但忘了生成迁移 → **套件会失败**，而不是生产失败。

---

<a id="l7"></a>

## 7. AI 子系统

### [重要] 分两类，归属不同

| 文件 | 类别 | 归属 |
|---|---|---|
| `runtime/ai/gateway.py` · `runtime/ai/provider.py` · `runtime/ai/messages.py` | **运行时**（Agent/LLM 节点调模型） | runtime 包 |
| `ai_buildtime/gen_graph.py` · `gen_node.py` · `repair.py` · `copilot.py` · `self_test.py` · `metering.py` · `prompts.py` · `parsing.py` | **构建时**（画图/补节点/修图/编辑副驾/生成后试跑/记账） | 画布平台 |

> 分层判据是**什么时候调模型**，不是用哪个库（任务 27a）。
> `messages.py` 跟着 runtime 走 —— `runtime/egress.py` 与 `security/deps.py` 都要用它。

### 关键设计（不要破坏）

1. **AI 输出必须过校验** —— `_validate_config()` 丢弃节点类型没有的字段。
   用户在 Inspector 里**永远看不到无法编辑的东西**。
2. **绝不静默改画布** —— 一律返回 diff，等用户 Accept/Reject。
3. **工具调用式生成图**（不是让模型吐一大坨 JSON）——
   每次变更立即校验，模型编错节点类型会当场收到 `ERROR:` 反馈。
4. **`GraphDraft.layout()`** 用最长路径分层算坐标 ——
   **模型不懂屏幕坐标，所以不问它要坐标**。
5. **`MockChatModel` 兜底** —— 没有 API key 时整个产品仍可演示、测试仍然 hermetic。
6. **给模型看的文本永远是英文**（工具描述、repair 的 issues）——
   原理与落地见 [`../current/i18n.md`](../current/i18n.md) §2。
7. **构建时 AI 也必须记账，但不能把平台安全包带进 runtime** ——
   `ai_buildtime` 直接建 LangChain model，每次回复后调
   `metering.record_reply_usage()`；运行时网关通过
   `set_usage_sink(record_usage)` 回调进入同一本账。`metering.py` 属于
   平台侧，导出 agent 不包含它，所以依赖边界仍然成立。详见
   [`../current/ai-features.md`](../current/ai-features.md) §2f。

详情 → [`../current/ai-features.md`](../current/ai-features.md)

---

<a id="l8"></a>

## 8. API 路由（实测 **58** 条）

> 实测 `@router.*` 装饰器共 **58** 个：workflows 15、runs 10、ai 10、schedules 9、
> catalogue 5、settings 4、access 3、lab 1、hooks 1。下面只列常用主路由，
> 完整清单以 `app/api/*.py` 为准。

```
GET    /api/health  /api/node-types  /api/tools  /api/state-fields
GET    /api/workflows            POST /api/workflows
GET    /api/workflows/{id}       PUT  /api/workflows/{id}   DELETE /api/workflows/{id}
POST   /api/workflows/validate   /api/workflows/import   /api/workflows/{id}/export
POST   /api/workflows/ports      ← 动态端口解析（前端靠它拿 Condition 的分支出口）
PUT    /api/workflows/{id}/ai    ← 保存画布 + AI 修改记录进版本
POST   /api/runs                 /api/runs/adhoc  ← adhoc = 跑未保存的图（UI 实际用这个）
GET    /api/runs  /api/runs/history  /api/runs/{id}  /api/runs/{id}/steps
GET    /api/runs/{id}/events     ← SSE，支持 ?after=N 与 Last-Event-ID
POST   /api/runs/{id}/resume     /api/runs/{id}/cancel   /api/runs/node-test
GET    /api/ai/status
POST   /api/ai/fill-node  /api/ai/generate-graph  /api/ai/generate-graph/stream
POST   /api/ai/copilot/stream   /api/ai/repair-graph
POST   /api/ai/node-to-code  /api/ai/code-to-node  ← 公开模式 admin-only
POST   /api/ai/workflows/fill-node   /api/ai/suggest-name
GET    /api/settings             PUT  /api/settings/llm
GET    /api/settings/llm/models  POST /api/settings/llm/test
```

### `GET /runs/{id}/events` 的三种结果

| 情况 | 状态码 | 为什么 |
|---|---|---|
| 运行还在内存 | 200 + SSE 流 | 正常 |
| **运行存在过但已淘汰** | **410 Gone** | 「去读历史」 |
| **从没存在过** | **404** | 「你有 bug」 |

**合并成一个状态码，就是让人花半小时去找运行 id 里的错别字。**

响应头 `X-Replay-From` 报告**回放窗口**（截断时能看出来）。

### 认证

`/api/*` 默认**全部需要认证**（中间件方式，默认关闭）。
公开路径白名单在 `security/auth.py` 的 `PUBLIC_PATHS`。

→ [`../current/security.md`](../current/security.md)

---

<a id="l9"></a>

## 9. 能力层（统一形状）

**[重要] 「一个底层」的物理形态。**

```python
class XxxCapability:                           # 接口（抽象约定）
    ...

class DefaultXxx(XxxCapability):               # 默认实现
    ...

def build_xxx(settings) -> XxxCapability: ...  # 从配置构造
def get_xxx() -> XxxCapability: ...            # 进程级注入点
def set_xxx(impl | None) -> None: ...          # 测试/替换用
```

> 注： 上面首行是**形状**而非字面基类 —— 六个接口里只有 `runtime/runs.py` 的 `RunStore` 真的
> 继承 `abc.ABC`；其余五个（`EgressPolicy` / `SandboxRunner` / `ToolRegistry` /
> `ModelGateway` / `SecretProvider`）是普通类，用 `raise NotImplementedError` 表达接口
> —— 「ABC」在本项目里是这类抽象接口的约定俗称。

### 已有的六个

| 接口 | 默认实现 | 生产实现 | 调用点 |
|---|---|---|---|
| `EgressPolicy` | `NetworkEgressPolicy` | 白名单 + 代理 | `tools/builtin.py` |
| `SandboxRunner` | `LocalSandboxRunner` | `ContainerSandboxRunner` | `code.py` |
| `ToolRegistry` | `BuiltinToolRegistry` | `ScopedToolRegistry` | `tool.py` · `agent.py` |
| `ModelGateway` | `LangChainModelGateway` | 配额/成本/路由 | `llm.py` · `agent.py` |
| `SecretProvider` | `SettingsSecretProvider` | `MappingSecretProvider` | `provider.py` |
| `RunStore` | `SqliteRunStore`（平台侧） | `NullRunStore`（导出的 agent） | `manager.py` · `api/runs.py` |

### [重要] 每条都已反向验证

- 故意把某条路由改回**直接 import 实现** → 对应测试**必须报 FAIL**
- `_guardprobe_capability.py` 覆盖能力层 / 迁移 / 回收 / 节点策略 / 运行存储 /
  checkpoint / 遮蔽 / 重启恢复 / 调度与事件触发 / 导出依赖 / SSE 线格式 / 级联删除
  （**通过数归 [`../current/status.md`](../current/status.md) §汇总**，此处不抄）

> 所以「一个 agent 的作者就算想绕开沙盒也绕不开」是**机制**，不是约定。

### 判定**不抽**的

**纯函数（无 I/O、无资源、无外部状态）不抽接口** —— 抽了只是多一层间接。

- `runtime/template.render`（6 个节点在用）
- `runtime/expr.evaluate`（2 个节点）

### 三个设计上的判断（都值得记）

| 判断 | 位置 |
|---|---|
| 接口要暴露**调用**，不只是**构造**（因为 token 数跟着响应回来） | [`../log/phase-1-3-gateway.md`](../log/phase-1-3-gateway.md) |
| **失败必须关闭**（fail closed），不降级 | [`../log/phase-1-1-sandbox.md`](../log/phase-1-1-sandbox.md) |
| 未知的配置值**报错**，不静默回退默认 | 同上 |

---

## 相关

- 概览（各部分怎么配合） → [`../current/architecture.md`](../current/architecture.md)
- 设计决策的理由 → [`design-decisions.md`](design-decisions.md)
- 前端每个文件干什么 → [`frontend-files.md`](frontend-files.md)
- 后端的坑 → [`../lessons/backend.md`](../lessons/backend.md)
