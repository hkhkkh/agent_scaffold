---
title: 后端的坑
read_this: 改后端 / 遇到奇怪的框架错误时
---

# 后端的坑

> 这一份是**框架与实现层面**的坑。
> 每一刀改了什么、发现了什么真 bug → [`../log/`](../log/)
>
> 注意： 这些都很隐蔽，**不要重复**。

---

<a id="b1"></a>

## 1. SQLModel 关系解析失败

### 症状

```
InvalidRequestError: expression "relationship("List['Run']")" seems to be using a generic class
```

### 根因

`models.py` 里加了 `from __future__ import annotations` ——
**注解变成字符串**，SQLModel 解析不出泛型参数。

### 修法

`models.py` **不加** future import，且用 `List["Run"]` / `Optional[str]`（typing 形式）。

### 同类陷阱

`compiler.py`（第 27 行，自最初提交起就在）与它自己的模块 docstring
（写着「故意不加」）**直接矛盾**，删导入还是改注释尚未定夺 ——
完整描述见 [`../reference/backend-layers.md`](../reference/backend-layers.md) §3。
（`app/` 下 87 个 `.py` 里有 **77** 个带这个导入；`models.py` 确实没有，那半是真的。）

---

<a id="b2"></a>

## 2. pydantic-settings 解析 list 字段时先 JSON 解码

### 症状

```
SettingsError: error parsing value for field "cors_origins"
```

给的是 `CORS_ORIGINS=http://a,http://b`（**逗号分隔，很自然**）。

### 修法

```python
Annotated[list[str], NoDecode]   # pydantic-settings 2.12 的 NoDecode
```

**`NoDecode` 阻止 pydantic-settings 把原始值当 JSON 解析。**

---

<a id="b3"></a>

## 3. [重要] `GraphBubbleUp` 被自己的 try/except 吞掉

### 这个 bug 最隐蔽

`interrupt()` 靠抛 `GraphInterrupt` 来**暂停执行**，
而它是 `Exception` 的**子类** ——

**所以节点包装层的通用 except 会把它转成「运行失败」。**

**表现为 HITL 完全不可用**，而错误信息看起来像节点代码有问题。

### 修法

```python
except GraphBubbleUp:
    raise                    # ← 必须在通用 except 之前
except Exception as exc:
    ...
```

### 教训

**任何包装第三方控制流的代码，都要先查清哪些异常是控制流。**

---

<a id="b4"></a>

## 4. [重要] contextvar 传事件 emitter 不可靠

### 症状

**「UI 看起来是死的」** —— 极难定位。

### 根因

原设计用 `current_emitter()` contextvar，
但 **LangGraph 可能在自己的 task 里跑节点** ——
contextvar **静默不传播**。

### 修法

emitter 作为**参数**传进 `_make_node_fn`，在**编译期捕获成闭包**。

### 推广

**「静默不传播」比「报错」危险得多** ——
它把「配置错了」变成「什么都没发生」。

---

<a id="b5"></a>

## 5. `langgraph.config.get_config()` 的类型注解必须精确写

写成 `RunnableConfig | None` **仍然告警**
（LangGraph 动态生成警告文案，**grep 不到源码**）。

### 最终方案

节点函数签名**只收 `state`**，运行态用 `get_config()` 取。

→ 告警消失，且**节点契约更干净**（只关于数据）。

---

<a id="b6"></a>

## 6. Tarjan SCC 替代 DFS 回边

DFS 回边法报「**哪个节点**造成环」**取决于遍历起点** ——

同样的图会给出**不同的错误信息**，用户会困惑。

SCC 是顺序无关的。

---

<a id="b7"></a>

## 7. 必需参数校验被默认值掩盖

### 症状

用户**显式清空**必填字段时，**校验通过**。

### 根因

原实现 `node.config.get(param.name)` ——
取不到就**回退到声明的默认值**。

### 修法

区分「**显式设置过**」和「从未设置」：

```python
if param.name in node.config:      # ← 只有显式设置过才判空
    value = node.config[param.name]
```

---

<a id="b8"></a>

## 8. 测试里忘记建表 → 运行期落库静默失败

### 根因

`_persist_start` / `_persist_finish` 里的 `except: pass`
把 `no such table` **吞了**。

### 修法

1. `conftest.py` 的 autouse fixture 调 `create_all()`
2. 落库失败改为 `logger.exception` ——
   **不能因为记录历史而中断运行，但也不能完全静默**

### 顺带修的

`get_engine()` 改成**按 URL 缓存**，而不是单例 ——
否则测试换 `DATA_DIR` 后**仍在写老库**。

---

<a id="b9"></a>

## 9. Code 节点忘了把 `inputs` / `vars` 注入命名空间

`CODE_CONTRACT_HELP` **承诺**有这些名字，但 `_exec_sync` 只传了 `scope`。

### 修法

```python
namespace.update({inputs, state, vars, params})
```

### 教训

**文档承诺的接口要有测试兜着。**

---

<a id="b10"></a>

## 10. 节点的时间上限及其两个陷阱（2026-09-14 已实现）

**现状**：所有节点都经 `runtime/attempts.py` 的策略层执行 ——
节点类型数与超时/重试的默认值见
[`../reference/backend-layers.md`](../reference/backend-layers.md) §2 / §5，
逐刀经过见 [`../log/phase-2-3-node-timeout.md`](../log/phase-2-3-node-timeout.md)。

留下两条容易反复踩的：

### ① `TimeoutError` 在 Python 3.11+ 是**同一个类**

`asyncio.TimeoutError` 就是内置 `TimeoutError`。所以「节点自己超时」
和「预算到了」在语言层面无法区分。
只写 `except TimeoutError` 会把节点自己的原因（`connection ... timed out after 3s`）
**丢掉**。必须问上下文：`asyncio.timeout(...).expired()`。

### ② 别让超时异常继承 `OSError`

`TimeoutError` **是** `OSError`，而 `runtime/runner.py`、`runtime/sandbox.py`
里有 `except OSError`。所以「预算超时」必须用自己的类型，
否则会被**为文件描述符写的错误处理**接走。

---

<a id="b11"></a>

## 11. 运行时顺序：transcript 先写、内存后裁

`_execute` 的 `finally` 里两行的**顺序是承重的**：

```python
self._persist_finish(context)   # 写库 + 写 transcript（读 bus 历史）
self._retire(context)           # release → bus.trim() 丢掉早期历史
```

**调换这两行，每次长运行的开头都会从 transcript 里消失。**

→ [`../log/phase-2-2-retention.md`](../log/phase-2-2-retention.md)

---

<a id="b12"></a>

## 12. 迁移的五个坑

全部在 [`../log/phase-2-1-alembic.md`](../log/phase-2-1-alembic.md)：

| # | 坑 | 一句话 |
|---|---|---|
| 1 | `alembic.ini` **必须纯 ASCII** | 被 `configparser` 以 `encoding="locale"` 读（这台机器 GBK）→ 一个破折号就让所有命令死于 `UnicodeDecodeError` |
| 2 | 自动生成的迁移**缺 `import sqlmodel`** | alembic 不会为 `sqlmodel.sql.sqltypes.*` 加 import → 只在**真正运行迁移时**才 `NameError` |
| 3 | `fileConfig` 必须**加守卫** | ini 里没有 `[loggers]` 段时它 `KeyError: 'formatters'` |
| 4 | `path_separator = os` | 不加则每次运行抛 25 条 `DeprecationWarning` |
| 5 | **Windows 文件锁** | 删 SQLite 文件前必须 `dispose_engines()`，否则 `PermissionError: [WinError 32]` |

---

<a id="b13"></a>

## 13. 沙盒相关的三个真 bug

全部在 [`../log/phase-1-1-sandbox.md`](../log/phase-1-1-sandbox.md)：

| # | 症状 | 根因 |
|---|---|---|
| 1 | 用户看到 `Code node failed: MemoryError:`（**后面什么都没有**） | `str(MemoryError())` **是空字符串**。只有分配量**小超限**、child 还能自己捕获时才走那条分支 |
| 2 | 容器**启动正常**，但每个 snippet 都死在 `ModuleNotFoundError: No module named 'app'` | `-I` 隐含 `-P`，所以 `/workspace` 不在 `sys.path` 上 |
| 3 | `memory_limit` 参数**看起来存在、实际被忽略** | `run_python` 只从 `SANDBOX_MEMORY_BYTES` 读，runner 传不进去 |

> **第 3 条是典型的「死守卫」**：参数在签名上，调用方传了，
> 但实现根本没读它。

---

<a id="b14"></a>

## 14. 工具注册表的三个真 bug

全部在 [`../log/phase-1-2-tools.md`](../log/phase-1-2-tools.md)：

| # | 症状 | 属性 |
|---|---|---|
| 1 | **下拉框里有一个实际不存在的工具**，用户配好保存，**直到运行时才发现** | 注册表在 **import 时**被冻结 |
| 2 | 关掉 `fail_on_error` 但「工具未启用」**仍然整个 run 失败** | 工具解析在 `try` **外面** |
| 3 | `unknown_tool_names` 对**它唯一存在的那个输入**返回「没问题」 | 它问的是「在名单里吗」而不是「这个工具存在吗」 |

---

<a id="b16"></a>

## 16. 同一个值有两条读路径 → 迟早不一致，而且方向很遮

2026-09-14 任务 2.6 时发现的**真实缺陷**（不是猜的）：

| 路径 | 做什么 |
|---|---|
| `api/runs.py::_tracked_step`（**实时**） | `redact(record.inputs)` / `redact(record.outputs)` |
| `manager._persist_finish`（**落盘**） | 只 `jsonable(...)` —— **没有遮蔽** |

**后果**：一个 run 在内存里显示遮蔽的 `api_key`，一分钟后被淘汰、
UI 从库里读回同一步就看到**明文**。**等待一分钟就泄露一个凭据。**

为什么一直没被发现：**没有任何测试比较过这两条路径**。
它之所以被发现，是因为要把读路径收到 store 后面，于是不得不问
「这两条路会不会不一样」。

**推广**：**凡是同一个东西存在两条读出路径，就写一个测试断言两边相等。**
键集、值、形状都要 —— 只比键集的话，「两边都遮蔽」和「两边都不遮蔽」
同样能通过（本项目就差点如此）。

**第二次现形**（2026-09-14 任务 14b）：同一个字段**值的类型**也能漂 ——
`RunManager.list()` 按 `created_at` 排序，而 `create` 造的 context 是 aware、
`hydrate` 从 SQLite 读回的是 **naive**（SQLite 没有原生 datetime，偏移被丢掉）。

```
retained_ids RAISED: TypeError: can't compare offset-naive and offset-aware datetimes
```

**`GET /runs` 直接 500**，触发条件是「重启后 resume 过一次」。
而两条路径的**键集完全相同** —— 所以 `TestTheTwoSummaryShapesAgree`（比键集）看不见它。

> **判据**：决定权在**谁身上**就修在谁身上。
> 偏移消失的原因只有一个，而且属于**列**（`UtcDateTime`，见 `db/models.py`）；
> 在每个读取方各修一次 = 每个读取路径一份知识副本。

---

<a id="b18"></a>

## 18. [重要] 在写「读某个库内部结构」的 helper 之前，先找它的文档化 API

2026-09-14 任务 14b 里，我为了从 checkpoint 里取出人工提问的载荷，
**手工解析** `CheckpointTuple.pending_writes`：

```python
for _task_id, channel, items in tup.pending_writes or []:
    if channel != "__interrupt__":
        continue
    for item in items:
        payload = _interrupt_payload(item)
```

我甚至写了一行注释记录「interrupt 在 `pending_writes` 里，**不在** `channel_values`」——
**那句话是真的，而它记录的正是一个不该需要知道的事实。**

实测（探针，不猜）：

```
graph.aget_state(config)  →  StateSnapshot
  .interrupts = (Interrupt(value={'message': 'Approve this?', ...}),)   ← 文档字段
  .next       = ('h',)
same payload? True      ← 与手工解析一致
```

**上游一直有 `aget_state`，`.interrupts` 就是为这个问题准备的字段。**

> **判据**：手工解析更底层的结构**不是「复用上游」**，
> 而是**针对一个更容易变的结构重新实现上游**。代码看起来一模一样，性质相反。
>
> **完整的检查清单与自查信号 → [`../target/reuse.md`](../target/reuse.md) §0。**
> 用户 2026-09-14 把这条提成了项目的第一规则。

### 三种「重造轮子」要分开

| 形态 | 例子 | 怎么判 |
|---|---|---|
| 上游有，**我没找到** | 本节的 `aget_state` | 写之前**先搜一遍上游 API** |
| 上游有，**我判断不适配** | `ChatPromptTemplate`、`Runnable` 作节点协议 | 要写清为什么不适用 |
| 上游没有，**确实是平台的** | `RunStore`（每进程的能力分层） | **不是冗余**，别为了"少写代码"删掉 |

**触发这次复查的是一个用户的质疑**：「怎么感觉全是新写的，没有拿来的？」
—— 质疑本身不构成证据，但它让我去**量**了一下，然后找到了证据。

---

<a id="b17"></a>

## 17. `redact` 按键名**和**字符串内容两条路遮蔽

`runtime/serialize.py` 的 `redact()` 走两条路：

```python
# ① 按键名：SECRET_HINTS 命中就把该键的值换成 ***，递归走 dict/list
{"api_key": "sk-live-..."}                          # → {"api_key": "***"}

# ② 按内容：字符串值本身再过一遍 secret 形状的正则，命中就遮蔽
{"out": "key is sk-live-DO-NOT-PERSIST"}            # → {"out": "key is [REDACTED_API_KEY]"}
```

内容那一路**默认开着**（`settings.redact_content` 默认 `True`，
拿不到配置时 `content_redaction_enabled()` 也返回 `True`；可按调用传 `content=False` 关）。
所以一个提示词节点把 key 拼进自己的输出（`"key is {{ vars.start.out.api_key }}"`）时，
凭据会**在离开进程前被遮掉**。

但它**不是万能的**：内容那一路只认**带前缀的**形状
（`sk-` / `AKIA` / `ghp_` / `xoxb-` / JWT 的 `eyJ`），
故意没有「长随机串」这种通用规则 —— 一个形状不典型的凭据仍然逃得掉。

**对测试的影响**：拿「拼进字符串的凭据」去测遮蔽**不再是恒真的** —— 它确实会被遮。
要测键名那条路，构造**嵌套的凭据键**（`{"api_key": ...}`）。

---

## 相关

- 逐层实现 → [`../reference/backend-layers.md`](../reference/backend-layers.md)
- 设计决策的理由 → [`../reference/design-decisions.md`](../reference/design-decisions.md)
- 反向验证方法论 → [`process.md`](process.md)
- 每一刀的完整记录 → [`../log/`](../log/)
