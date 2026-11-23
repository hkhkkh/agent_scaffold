---
title: 可复用面：市面已经有什么，我们该拿什么
read_this: 决定"这个要不要自己写"之前
owner_of: 外部依赖与可复用清单的唯一归属地
verified: 2026-09-14（版本为当日实测）
---

# 可复用面

> **用途**：避免重复造轮子。
> 「要不要自己写」这个问题，答案在这里，不要重新调研。

**本文件以 2026-09-14 的实测为基准**（此后各节按自己的日期增补，标题都带日期），
不是抄文档。来源是官方文档站（`docs.langchain.com`），**未在本项目内逐个 POC 验证的会标出来**。

> **注意：§1 的编号是稳定地址，不要重排、不要重新编号。**
> 源码里有 6 处直接引用它们 —— `runtime/attempts.py` §0、`runtime/projection.py` §1.29、
> `scheduling/rules.py` 与 `scheduling/service.py` §1.33、`memory/goals_node.py` §3、
> `scripts/measure_reuse.py` §9.5（这个脚本专门重新实测 §9.5 的数字）。
> 其中一部分**已经随导出物封存在 `data/exports/*/vendor/` 里**，那些副本改不动。
>
> 所以 §1 读起来像流水账、编号也不连续（1.1 → 1.35 → 1.36 → 1.34 …）：
> 那是**追加**的痕迹，不是整理的失败。**不要"整理"它** —— 代价是源码里 6 处引用
> 永久指向错误的位置。文件长是这份稳定性的价钱。

---

## 0. [最重要] 默认动作：先找，再写

**用户 2026-09-14 明确要求：能复用优先用已经有的，不要重复造轮子。**

**这不是「有空时优化一下」，而是默认顺序。** 写任何工具函数、抽象层、协议适配、
解析器之前，先在本文件找一遍，再到上游包里搜一遍 API。

### 三种「重造轮子」，只有第一种是负债

| 形态 | 判据 | 处理 |
|---|---|---|
| 上游有，**我没找到** | 我在读一个对象的**内部结构**，而不是它的公开方法 | **负债** —— 改用它 |
| 上游有，**我判断不适配** | 能说清为什么不适用 | 合理，但**把理由写下来** |
| 上游**真的没有** | 例子：`RunStore` 的每进程能力分层 | **不是冗余** —— 这是我方价值 |

### 自查信号：出现这几条，就停下来搜一遍

- 我的注释在解释**某个库的内部布局**
- 我在 `getattr` 一个**私有属性**，或解析一个**未文档化的元组 / 字典结构**
- 我在写一个**名字很通用**的 helper（`load_state` / `run_with_timeout` / `parse_*`）
- 我在把某个库的对象**翻译**成自己的对象，只为了用它的一个能力

### 反例（本项目的真实记录）

| 我写的 | 上游一直有的 | 记录 |
|---|---|---|
| 手工解析 `CheckpointTuple.pending_writes` 取 `Interrupt` | `CompiledGraph.aget_state(config).interrupts`（**文档字段**） | [`../log/phase-2-8-resume-after-restart.md`](../log/phase-2-8-resume-after-restart.md) §2 |

**那次** 是**有测试、能跑通**的 —— 所以**「能跑通」不是「用对了层」**。

### 动手前的检查清单

1. 本文件的 §1 / §2 / §4 里有没有现成的？
2. 上游包（langgraph / langchain / sqlmodel / fastapi）有没有**文档化**的 API？
3. §6「拿不来的」里是不是已经判定过「自己写」？（是 → **别重新调研**）
4. §9「必须自己做的」里有没有？（有 → 直接写）
5. 都不是 → 才动手，并**把新发现回填到本文件**

---

## 1. 已装可用（零依赖变更）

| 能力 | 用什么 | 对应我们的 |
|---|---|---|
| 持久 checkpoint | `langgraph.checkpoint.sqlite.{SqliteSaver,AsyncSqliteSaver}` | 任务 14 已接 |
| 内存 checkpoint | `langgraph.checkpoint.memory.InMemorySaver` | 已接（`CHECKPOINT_STORE=memory`） |
| **读暂停状态** | `CompiledGraph.aget_state()` → `StateSnapshot.interrupts` / `.next` | 任务 14b 已接 —— **曾经被我手工解析 `pending_writes` 重造过**，见 §4 |
| 跨线程记忆 | `langgraph.store.memory.InMemoryStore` / `langgraph.store.base.BaseStore` | **Phase 3 的地基** |
| 图执行 | `langgraph 1.2.11`：`StateGraph` / `interrupt()` / **原生子图** / `Command.PARENT` | 已在用；子图是任务 30 的执行部分 |
| 嵌套观测 | `compiled.stream(..., subgraphs=True)` | 任务 30 的子流程 UI。**注意：我们现在只用 `ainvoke`**（实测 0 处 `astream_events` / `stream_events`），所以这是**新接入**，不是现成 |
| **内容遮蔽引擎** | `langchain.agents.middleware.pii.apply_strategy` | 已接（见 §4） |
| SSE | `sse-starlette 3.1.1` | 已在用 |
| ORM / 迁移 | `sqlmodel 0.0.42` / `alembic` | 已在用 |

### 1.1 [2026-09-14 实测] 已装可用、但**还没拿**的四项

四项都已导入并**实际跑通**（不是读文档得到的）。它们对应未来三个任务。

| 上游能力 | 是什么 | 对应未来的 | 实测结果 |
|---|---|---|---|
| `langgraph.types.Send` | **动态扇出** —— 按**运行时**的列表长度 map 到 N 个 worker | 编译期并行分支 / 「批量」语义 | 3 项 → 3 个 worker，`doubled=[2,4,6]` |
| `compiled.update_state` / `aupdate_state` | **改状态后继续跑** —— 改某个节点输出，从那里接着走 | 运行面板的「从这步重跑」；画布试跑 | 同步与异步版本都存在 |
| `compiled.get_state_history` / `aget_state_history` | **完整 checkpoint 时间线** | 运行时间线 UI | 存在；`StateSnapshot.parent_config` 可往回走 |
| `langgraph.store` 的 **TTL** | 记忆项带过期 | Phase 3 记忆 —— **不用自己写清理器** | `supports_ttl` / `ttl_config` 存在；`InMemoryStore` 的 put/get/search 跑通 |

**`StateSnapshot` 的字段是**（实测）：
`values` · `next` · `config` · `metadata` · `created_at` · `parent_config` · `tasks` · `interrupts`

任务 14b 只用了 `interrupts`。**`tasks` 还没用** —— 它带每个 task 的细节
（谁在等、等什么），比 `interrupts` 更全，子流程的 UI 会用得上。

### 1.2 [重要] 子图嵌套实测：两种形状，第二种会**静默丢键**

任务 30 的执行部分。实测了三种写法：

| 形状 | 写法 | 结果 |
|---|---|---|
| **A** 共享 state schema | 编译子图**直接当节点**，两边 schema 相同 | 通过：子图读到父的 `x=10`，写回 `x=11` |
| **B** 独立 schema + 显式包装函数 | 一个函数把父状态映射进、把子结果映射出 | 通过：`answer='len+1=6'` 正确回到父状态 |
| **C** 独立 schema 的编译子图**直接当节点** | 不给映射函数 | **不报错，但静默丢键** —— 实测 `answer` 从未出现 |

**判据**：**C 是最危险的那种失败** —— 图跑完了、没有异常、结果少了一块。
**要复用的子流程必须走 B**，别指望框架替你映射两边的 schema。

> 这一条同时修正了 §1 里「原生子图」那行：**能嵌套 ≠ 能复用**。
> 嵌套（A）是有的；**跨 schema 复用（B）要自己写适配函数**。

### 1.35 [2026-09-18 全面搜索] 定义版本化：**官方有范例可抄，但要抄的是模式不是包**

任务 28（定义版本化）开工前的搜索。三条线都查了：本地已装、上游文档、外部实现。

#### [重要] 先厘清一件事：LangGraph 的「version」**不是**这个

搜索时最容易走错的一步 —— `langgraph` 有大量叫 version / history 的东西，
**全都是运行状态的版本，不是定义的版本**：

| 那个东西 | 它版本化的是 |
|---|---|
| `get_state_history` / `update_state` / time travel | **一个线程的 state**（跑到哪一步了） |
| `DeltaChannel` + `version compatibility and rollbacks` | **checkpoint 的编码格式** |
| `Pregel` 的 `version="v3"` | **流式事件的协议** |
| LangGraph 版本号 | 包本身 |

[重要] **两条正交的轴**：任务 28 要的是**定义**（图长什么样），上面全是**执行**（跑到哪了）。
把这个分清楚，才不会去改错层。

**唯一相关的一条**（`backward-compatibility` 页）：
> pinning a **behavioral version** in state so existing threads keep the old path
> while new threads pick up the latest version

→ 即「**在 state 里钉一个版本号**，老线程走老路，新线程走新路」。
这条对任务 28 有价值：它说明**版本不仅要存，还要在 run 启动时记下用的是哪一版**。

#### [重要] LangSmith Assistants 的版本化模型 = 任务 28 的语义（**抄语义**）

官方原文：

> Assistants support versioning to track changes over time. Once you've created an
> assistant, subsequent edits will automatically create new versions.
> * Each update creates a new version of the assistant.
> * You can **promote any version to be the active version**.
> * **Rolling back** to a previous version is as simple as setting it as active.
> * **All versions remain available** for reference and rollback.

逐条对照任务 28 的要求：**完全同构**。所以设计不用自己发明：

| 概念 | 官方叫法 | 我们的落法 |
|---|---|---|
| 每次保存产生新版本 | automatic new version | `save` 时写一行 `workflow_versions` |
| 任意版本可提升为当前 | promote to active | `Workflow.version` 指向哪个版本行 |
| 回滚 = 切换 active | rollback = set active | 一次 UPDATE，不复制数据 |
| 所有版本都可取回 | all versions available | 版本行不可变，永不删 |

[重要] **它给的一个坑（原话警告）**：
> When updating an assistant, you must provide the **entire configuration payload**.
> The update endpoint creates new versions **from scratch and does not merge**.

→ 即「新版本是**整体快照**，不是差量合并」。
**这一条正好支持任务 28 的「另开表」决定**：版本行存**完整快照**，
不是 patch。理由很实际 —— patch 链要求能重放全部历史才能还原某一版，
而历史里任何一环损坏就断链；快照只要那一行。

[重要] 代价是**存储**。我们这个规模（一张图几十 KB）无所谓，
但要在文档里写明「这是选择，不是疏忽」。

#### [重要] SQLAlchemy 官方**有两个版本化范例**（**抄模式，不装包**）

在 `docs.sqlalchemy.org/en/20/orm/examples.html` 的 "Versioning Objects" 一节，
**两个模式**：

| 范例 | 做法 | 对我们的适用性 |
|---|---|---|
| `versioned_history/history_meta.py` | `Versioned` mixin + **独立历史表**（`SomeClassHistory`），版本号自增 | [重要] **就是任务 28 说的「另开 `workflow_versions` 表」** |
| `versioned_rows/versioned_rows.py` | 把 UPDATE **转成 INSERT**（时序行），旧行原样保留 | 备选：同一张表里堆版本。会改现有查询 |
| `versioned_update_old_row.py` | 同上 + `do_orm_execute` hook 把查询**限制到最新版** | 说明时序行的代价：**每个查询都要挂 hook** |

→ **选第一个**（独立表），因为：
1. 任务 28 的坑 #1 已经这样判了（「另开表更干净，现有查询不用改」）；
2. 时序行的代价在上面第三个范例里明摆着 —— 要把「只看最新」做成全局查询钩子，
   而这个项目有一处 `session_scope()` 到处在用，加钩子等于改所有读路径。

[重要] **关键：这两个范例都在 `/examples` 里，不是库的一部分。**
官方从没打算让人 `import` 它们。所以结论是 **抄代码结构，不引依赖** ——
`history_meta.py` 本身也就 100 多行，而且它做的事（监听 flush、写历史行）
在这个项目里我们用**显式的 `save()` 路径**就能表达，不需要 ORM 事件钩子。

[重要] 但它给了一个**值得注意的能力**：`use_mapper_versioning = True` 会接入 ORM 的
乐观并发 —— 同一版本号并发 UPDATE 时**其中一个会失败**。
这对应「**两个人同时保存同一张图**」的问题。我们目前没有并发保存保护，
**记在这里，不在任务 28 里做**（那是另一个问题）。

#### 本地已装：没有版本化库，但有两个可用的零件

```
deepdiff          False      sqlalchemy_history   False
dictdiffer        False      sqlalchemy_continuum False
jsondiff          False      semver               False
jsonpatch         True   <-- RFC 6902 patch
xxhash            True   <-- 快速哈希
pydantic          True
```

- **`jsonpatch` 已装**：如果要存 delta 而不是全量快照，它在。
  → **不用**，理由见上（快照 > patch，官方警告也指向快照）。
- **`xxhash` 已装**：比 sha256 快。→ **不用**：`definition_digest` 已经用 sha256，
  一张图几十 KB，哈希时间可忽略，换算法只会让既有钉住值全部失效。
- **`sqlalchemy-history` / `sqlalchemy-continuum` 都没装**，也**不装**：
  它们会在 schema 里加自己的表（again 不受本项目 alembic 管理），
  而这个项目有一条测试要求「迁移产物 == 模型」（`TestTheTwoPathsAgree`）。

#### 结论：任务 28 的复用账

| 部分 | 来源 |
|---|---|
| 语义（新版本/提升/回滚/全留） | **抄 LangSmith Assistants** |
| 表结构（独立历史表 + 版本号） | **抄 SQLAlchemy `history_meta` 范例的模式** |
| 「整体快照不 merge」 | **抄官方的警告**（这是它踩过的坑） |
| 「run 要记下用的是哪一版」 | **抄官方 backward-compatibility 的 behavioral version** |
| 哈希实现 | **已有**：`subgraphs.py:definition_digest`，只需修 `position` |
| 要写的代码 | 只是把上面这些接到本项目已有的 `repository.save` / `get` 上 |

[重要] **一句话**：任务 28 没有「可装的轮子」，但有**三份官方范例**给出了语义、表结构、和两个坑。
要写的是接线，不是设计。

### 1.36 [2026-09-18 实测快照] M4 封条：构建源码 wheel，安装时禁止 sdist

本节记录 2026-09-18 的搜索与平台实测；开工时应复核工具版本和 issue 状态。

#### PyPI 同版本不等于同一份代码

本机 editable LangGraph 与 PyPI 都标为 `1.2.11`，但 `pregel/_utils.py` 不同：

| 来源 | 行数 | 子图检测 |
|---|---:|---|
| PyPI wheel | 291 | `ast` / `inspect` / `textwrap`，读取源码文本 |
| `H:/agent_1/langgraph` 源码树 | 221 | `dis`，分析字节码 |

源码树提交 `1e44bda` 是 `fix(langgraph): detect subgraphs from bytecode instead of source (#8569)`。
任务 30 使用子图，不能按版本号换成 PyPI wheel。

#### `--require-hashes` 与 `--only-binary`

pip 要求 hash-checking mode 下所有依赖都 pinned 并带哈希；editable 安装不支持，
本地项目目录也不支持（除非指定具体归档）。本机 editable 安装不能直接封条；
需从源码树构建 wheel，再把 wheel 和完整依赖闭包纳入 `vendor/` 与哈希文件。

pip issue [#13984](https://github.com/pypa/pip/issues/13984) 报告 pip 25.1.1 的
`--require-hashes` 不校验 `[build-system].requires` 的隔离构建依赖。sdist 可能运行
未校验的 build backend。pip 安全安装文档建议同时使用 `--only-binary :all:`，
禁止安装时构建 sdist；这是封条成立的必要条件。

2026-09-18 以导出器的六个直接依赖解析闭包：Windows CPython 3.13 为 **46/46 wheel**；
Linux CPython 3.13 `manylinux2014_x86_64` 也为 **46/46 wheel**，无 sdist。
`manylinux_2_28_x86_64` 当时解析超时，未单独验证。

#### 导出物没有固定画布验证过的依赖版本

导出器写 `>=` 范围，未固定完整闭包。2026-09-18 对照本机安装与 Linux 解析，
`langchain` 为 1.4.0/1.4.1、`langchain-core` 为 1.6.0/1.6.3、
`pydantic` 为 2.12.4/2.13.5 等。导出物当时不能复现画布验证过的依赖集合；27c 应为
完整闭包生成精确 pin 和哈希。

PEP 440 的 local version identifier 可表示下游兼容补丁，例如
`1.2.11+agentscaffold.1`。只写 `==1.2.11` 会忽略 local label；要精确匹配补丁版须写完整版本。

**未验证**：源码树能否构建并运行 wheel；`hatchling` / `uv` 当时未安装，未执行构建。
开工时应重新核对上游状态并实测构建，不要把本节快照当作当前验证结果。

---

### 1.36b [2026-10-08 补测] 源码树构建 wheel —— **已验证**

§1.36 标为「未验证」的那一项已实测，结论与它当时的推断一致：

| 项 | 实测 |
|---|---|
| 构建 | `python -m build --wheel`（后端 `hatchling`）成功，产物 `langgraph-1.2.11-py3-none-any.whl` |
| 内容是源码树版吗 | [x] **是**。wheel 内 `pregel/_utils.py` **221 行、用 `dis` 读字节码**；PyPI wheel 是 **291 行、读源码文本** |
| 依赖闭包 | **49/49 wheel、0 sdist**（Windows CPython 3.13）。§1.36 记的 46/46 是 2026-09-18 的闭包；2026-10-10 补 `aiosqlite` / `langgraph-checkpoint-sqlite` / `sqlite-vec` 之后为 49 |
| 封条 | 49 条 `--hash=sha256` pin；`--no-index --require-hashes` 离线安装通过 |
| 反向验证 | 往一个轮子追加 1 字节 → pip 报 `THESE PACKAGES DO NOT MATCH THE HASHES` |

**因此 27c 不能替换成 PyPI wheel** —— 那会静默换掉任务 30 需要的字节码版子图检测。
构建与解析已脚本化为 [`../../backend/scripts/build_vendor.py`](../../backend/scripts/build_vendor.py)，
锁文件 `backend/_vendor_requirements.txt` 进版本控制（轮子不进，可由锁重建）。

仍未做：发布到私有源 / 基础镜像（27b 的后半）；本机离线封条不依赖它。

### 1.34 [2026-09-17 官方文档] 事件唤醒：LangChain 把它叫 **Channels**，架构可抄接口不该抄

做任务 22 的「事件唤醒」那半之前先搜了一遍，官方文档里对应的概念是 **Channels**：

> Channels define when your agent starts running. Connect your agent to external
> events so it responds automatically to messages, emails, or other events.

它把一个 channel 拆成三块：

| 官方三块 | 我们的做法 |
|---|---|
| **Inbound**：验证并归一化外部事件，然后起一次 run | 抄这个形状。`POST /api/hooks/<token>` → 归一化成 run 的 inputs → 起 run |
| **Outbound**：把 agent 的回复发回原会话 | **不做**。我们的 run 产出在 run 历史里，没有「回话给发送方」这个需求 |
| **Provisioning**：用 provider API 建资源（webhook 订阅、bot 注册等） | **不做**。我们收的是通用 HTTP，不绑定具体 provider |

[重要] 附带得到一条现成的分类法，省掉自己设计：**定时用 schedules，事件用 channels**，
官方自己就是这么分的（「To trigger an agent on a recurring basis, use schedules」）。
→ 我们的 `kind` 字段（`schedule` / `webhook`）和它同构，这不是巧合，是这个领域本来就二分。

#### [重要] 抄架构，不抄接口

官方那套是为**托管的多租户平台**设计的：provider adapter、身份/线程解析、
回复投递、资源供给。我们的场景是**自托管的单实例**，外部系统只要能发 HTTP 就行，
所以：

- **不引入 provider 概念**。GitHub / Slack / 邮件各自的签名与格式校验是各自的事，
  我们的入口是通用 JSON。要接某个 provider，在那个 provider 侧做转换即可。
- **不做回复投递**。没有「原会话」可以回。
- **不做 HMAC 签名校验**。需要双方共享密钥，而多数发送方配不了；
  token 本身就是认证，再叠一层会排除掉一半调用者。
  （代价写进 `app/api/hooks.py` 的 docstring 了：token 会进访问日志。）

#### [重要] 一个必须自己决定的：hook URL 里的 token 是**唯一**凭证

搜到的同类产品（n8n、Zapier、GitHub）都把 token 放**路径**里而不是 header，
理由不是审美而是兼容性：URL 是每个能发 webhook 的系统都能配的输入。
→ 所以 `PUBLIC_PREFIXES = ("/api/hooks/",)`，而不是往 `PUBLIC_PATHS` 里加一条
（路径带 token，列不出来）。

[重要] 配套的安全决定（反向验证过）：**token 存在但被禁用 → 也答 404，不答 403**。
答 403 等于告诉一个未授权方「这个 token 是真的，只是关着」——
等于白送一个可用 token，暴力探测就有了反馈信号。

### 1.33 [2026-09-17 实测] 调度：**APScheduler 全盘复用**，并纠正 §1.30/§1.32 的错误结论

#### 先说纠错

§1.30 的结论是「只拿『什么时候触发』，其余都自己做」，§1.32 的标题写着「不要装 APScheduler」。
**两条都是错的**，代价是手写了 **1512 行**，其中 ~750 行是从零实现的调度器。用户三次指出
「能复用优先用已有的」「去网上搜，你为什么不遵循指令」。以下是纠错后实测的结果。

**当时错在哪**：我在 APScheduler 的 FAQ 里读到「一个 job store 不能跨进程共享，否则会重复执行
或漏掉任务」（§1.32 引过），把它读成「这个库不行」。
**它其实是**：这个库**知道**这个问题、**写进文档警告你**、并且提供了两种运行模式
（单进程用它自己的 job store；多进程用 scheduler + worker + 共享数据存储 + event broker）。
我读到的是「多实例场景的限制」，却用来否定了「单实例场景的适用性」。
**这和 2026-09-16 那次 `error_handler` 的错误是同一个病**：把「我没搞对」当成「它做不到」。

#### 实测：APScheduler 3.11.3 **可以**直接替换我手写的全部三块

| 我手写的 | APScheduler 的对应物 | 实测结论 |
|---|---|---|
| `dateutil.rrule` 适配器 + 边界/错过计算（177 行） | `CronTrigger` / `IntervalTrigger` | 语义**逐条一致**（见下） |
| 常驻 `while` 循环 + 钳制 sleep + 取消（231 行） | `AsyncIOScheduler` | 在运行中的 asyncio loop 里 start/modify/pause/resume/remove/shutdown 全部正常 |
| 迟到的容忍 + 积压策略 + 重叠保护 + 启用开关 | `misfire_grace_time` / `coalesce` / `max_instances` / `pause_job` | 四个都是现成参数 |
| 自己造的 ABC + 两个实现 + registry（759 行） | 不需要 | 调度是平台侧，不进导出包，**那条约束本来就不该套在它身上** |

#### 语义一致性（逐条实测，不是我推断的）

输入 `CronTrigger(hour=9, minute=0, timezone=UTC)`，与我手写版的对照：

| 情形 | 我手写的 | APScheduler | 一致 |
|---|---|---|---|
| `2026-03-01 09:00` 整（含） | `03-01 09:00` | `03-01 09:00` | 是 |
| 同一时刻 + 1 秒 | `03-02 09:00` | `03-02 09:00` | 是 |
| 跨美国 DST 两天（`03-08`/`03-09`） | 均 09:00 UTC | 均 09:00 UTC | 是 |
| 每周 mon/wed/fri 09:30 | 周一/三/五 09:30 | `day_of_week='mon,wed,fri'` | 是 |

即「含边界 vs 严格之后」的两种查询都有对应方法，我手写时为此专门分的
`first_after` / `next_after`，库里是 `get_next_fire_time(previous, now)` 的 `inc` 语义。

#### [重要] 装它必须知道的一个坑（实测，非文档）

**每次构造 trigger 都要显式传 `timezone=UTC`，不能继承。**

```
# 实测：scheduler 用 timezone=UTC 构造，但 trigger 不传时区
sched.modify_job("probe", trigger=IntervalTrigger(seconds=5))
  -> job.next_run_time == 2026-09-17 08:56:01+08:00     # 本机偏移！
```

即「重建 trigger 时不带时区会掉回服务器本地时区」。后果是排程静默漂移到本地时钟，
症状是「对某些人它整点跑错了」——极难定位。
→ 我们的 `ScheduleRule.to_trigger()` 在**每一个**构造点都传 `timezone=UTC`，
且 `test_the_timezone_is_utc_and_not_inherited` 反向验证过（去掉就红）。

#### 版本选择：3.x 而不是 4.x

APScheduler 官方 README 原话：

> The v4.0 series is provided as a **pre-release** and may change in a backwards
> incompatible fashion without any migration pathway, so **do NOT use this release
> in production!**

3.11.3 是 `Production/Stable`、MIT、Python 3.13 在支持列表、7.6k stars、140k 依赖者。
→ pin `APScheduler>=3.11,<4.0`。

[重要] 附带好处：APScheduler **不依赖 `dateutil`**（只带 `tzlocal`），所以
§1.32 里发现的「导出包缺 dateutil」那个 bug **随这次改写一起消失了**——
`app/runtime/` 不再引用 `dateutil`。这也说明当初手写 rrule 适配器
是在给导出包引入一个原本不需要的依赖。
（[重要] **别写成「不再引用任何第三方包」**：`checkpoints.py` / `manager.py`
仍导入 `langgraph`，工作树里 `export.py` 还导入 `yaml`。）

#### 拿它、不拿它的边界

**拿**：trigger 算术、loop 生命周期、misfire/coalesce/max_instances、pause/resume。

**不拿**（这些是我们自己的）：
- **`SQLAlchemyJobStore`** —— 它会在 schema 里建一张不受 alembic 管的表，
  而这个项目有一致性测试要求迁移产物与模型完全一致。规则存在我们自己的行里，
  APScheduler 用 `MemoryJobStore` 持有，启动时按行重新注册。
- **「工作流被删了」的发现** —— APScheduler 没有「这个 job 指向的东西不存在了」的概念。
- **可靠性台账** —— 哪次火起了哪个 run、迟了多少、错过几次。APScheduler 的 event
  是进程内的，重启即失，而这种记录恰恰要跨重启。

**理由**：和 §1.30 的判断一致 —— 拿「引擎」，不拿「架构」。

### 1.32 [2026-09-16 官方文档] 调度：`CronTrigger` 的**时区/DST** 语义

> 注意： **本节关于「不装 APScheduler」的结论已被 §1.33 推翻**（2026-09-17 实测改为全盘复用）。
> 下面保留的是**当时的判断与它引用的语义文档**（DST / 时钟对齐那几条仍然有效），
> 但**不要据此认为我们不用 APScheduler** —— 以 §1.33 为准。

读 `apscheduler` 的 cron trigger 页与 FAQ（它的调度器我们**不装**，但它的
**语义文档**是这一块最完整的）。

#### [重要] DST：两个真实的反直觉结果

> The cron trigger works with the so-called "wall clock" time … If your schedule
> would have the job executed on either one of these periods, it may execute
> **more often or less often than expected. This is not a bug.**

实测文档给出的例子（`Europe/Helsinki`，`hour=3, minute=30`）：

| 情况 | 结果 |
|---|---|
| 三月的最后一个周日早晨 | **完全不执行** —— 那个墙钟时刻**不存在**（时钟从 2:59 跳到 4:00）|
| 十月的最后一个周日早晨 | **执行两次** —— 那个墙钟时刻**重复出现** |

[重要] **「每天 3:30」在某些日子是 0 次或 2 次，而这被明确称为 not a bug。**
**这是设计必须选边的地方**，不选也会有一个默认行为，只是没人知道是哪个。

**他们的建议**：要么用**不观察 DST 的时区**（UTC），要么避开切换时刻。

→ **我们的选择**：**一律以 UTC 计算**，拒绝把本地墙钟当基准。
「每天 9 点」= **UTC 9 点**，并在参数描述里**写明**。
不假装支持「用户本地 9 点」—— 那需要 DST 语义的完整定义，而我们没有。

[重要] 这条**推翻了我上一版的隐含假设**（我原来想的是「用本地时间更友好」）。
**友好是假的**：在切换日它会是错的，而且错得不一致。

#### [重要] 「时钟对齐」还有第二种非预期行为：**跳过**

> "Every 7 seconds" fires at :00, :07, :14, and so on up to :56, then jumps back
> to :00, a 4-second gap instead of 7

→ 和 n8n 说的是**同一件事**（§1.31 第 3 条），两家独立确认。
**间隔型规则在时钟边界会有一次不均匀的间隔。**

#### [重要] 为什么**不装** APScheduler（他们自己说的）

FAQ 里两条，直接印证了 §1.30 的判断：

> **How do I share a single job store among one or more worker processes?**
> **Short answer: You can't.** … Sharing a persistent job store among two or more
> processes will lead to incorrect scheduler behavior like **duplicate execution
> or the scheduler missing jobs**.

→ 它**连自己的核心问题都没解决**。而我们已经有 `TaskQueue`（带认领语义）。

> Running a `BackgroundScheduler` and then letting the execution reach the end of
> the script … The script above will **exit** right after calling `add_job()`

→ 「后台任务在 lifespan 结束后静默死掉」—— **正是我上一刀标记的风险点**
（第一个常驻后台任务）。他们会踩，我们必须显式管生命周期。

#### [重要] `get_next_fire_time(previous_fire_time, now)` 这个签名值得抄

**「下一次」是 `(上一次, 现在)` 的函数，不是「现在」的函数。**
这解决一个真问题：**错过了几次之后，要能从「上次实际触发」推下一次**，
而不是从「现在」推 —— 否则停机期间错过的那些会被无声跳过。

→ 我们的 `next_after(rule, last_fired_at, now)` 照这个形状写。

### 1.31 [2026-09-16 官方文档] 调度：n8n 的 durable scheduler **踩过的坑可以拿，架构不要拿**

`docs.n8n.io/deploy/host-n8n/configure-n8n/durable-scheduler.md` 是本项目遇到过的
最相关的一篇 —— 它讲的**就是**「把调度从内存搬进数据库」这件事的完整代价清单。
下面只列**我们该采纳的设计**，不列它的实现（那需要 Redis + 多实例租约）。

#### [重要] 四条会踩的坑（都是他们的血）

**1. 「错过的运行」必须有策略，不能悄悄丢**

> A run counts as missed once it's later than its grace period.

**默认策略是「丢弃」**（`Don't Run Missed Executions`），可选「补跑最近一次」。
[重要] **关键**：**任何策略都不逐条重放积压**（`no policy replays it run by run`），
而且**一次性计划没有下一次可恢复** —— 跳过就是永久丢弃。

→ 我们至少要**记录**错过，而不是无声无息。默认**不补跑**是对的
（一个每天 9 点的任务在停机三天后补跑三次，几乎不是用户想要的）。

**2. 「宽限期」有下限，且下限由另外两个参数决定**

> n8n raises a node's grace period to an instance-derived minimum (based on
> `N8N_SCHEDULER_EXECUTOR_INTERVAL` and `N8N_SCHEDULER_MATERIALIZATION_WINDOW`)

→ **宽限期不能小于检查间隔**，否则正常轮询延迟就会被判成「错过」。
这条我不会想到，是他们的启动警告讲出来的。

**3. 定时「每 N 秒」有个**边界**语义，两种都合理**

> `legacy`: runs fire on clock boundaries … "every 7 seconds" fires at :00, :07,
> :14 … up to :56, then jumps back to :00, **a 4-second gap instead of 7**

> `new`: runs fire a fixed number of seconds apart, **counted from the moment you
> activated**

[重要] **两种都不是 bug，是选择**，而 legacy 是默认（保持行为不变）。
**这是我会漏掉的细节** —— 「每 7 秒」在时钟对齐下有 4 秒的空档。

**4. 时区是定时任务的经典坑，两级设置**

全局 `GENERIC_TIMEZONE` + **每工作流可覆盖**。文档还提到
**变量在「发布时」求值**，之后改变量值不影响已发布的调度 —— 需要重新发布。

#### 该采纳的设计（不是实现）

| 设计 | 为什么 |
|---|---|
| **提前把「即将运行」写进库**（materialization） | 重启不丢；这是「持久」的定义 |
| **认领（claim）后才执行** | 一次运行只跑一次；我们单进程也需要（防重入） |
| **宽限期 + 错过策略** | 上面 §1 |
| **保留期**（跑完的运行留一阵再删） | 否则表无限增长 |
| **所有者核对**：所有者的工作流不存在了就停掉这个计划 | 删了工作流但计划还在 → 会一直失败 |

[重要] 后两条**我们现在的 `RunStore` 保留策略里已有先例**，照抄形状即可。

#### 不该采纳的

| 不拿 | 为什么 |
|---|---|
| 多实例租约（`N8N_SCHEDULER_LEASE_DURATION`） | 我们是单进程；租约是分布式才需要的 |
| Redis / 数据库队列表 | 我们已有 `TaskQueue` |
| 轮询触发器（poll triggers）那一整套 | 那是「拉外部数据源」，我们没有这个功能 |
| 全套 Prometheus 指标 | Phase 5 的事 |

[重要] **判断**：他们的架构是**为多实例设计的**，而我们的调度器**只需要正确**。
所以拿**语义**（错过、宽限、边界、时区），不拿**机制**（租约、Redis）。

### 1.30 [2026-09-16 实测] 任务 22 调度：**只拿「什么时候触发」，其余都自己做**

调度拆成三块，**复用面差别很大**，所以先拆再决定：

| 部分 | 复用面 |
|---|---|
| ① **什么时候触发** | ✅ **`dateutil.rrule` 已装可用** |
| ② **「下次触发时间」存哪、谁唤醒进程** | ❌ 没有可用的（见下） |
| ③ 触发后干什么 | ✅ **已有**：`RunManager.create` + 队列 |

#### ① `dateutil.rrule` 实测可用

```
DAILY                          → 每天同一时刻
WEEKLY(byweekday=MO..FR, byhour=9) → 工作日 09:00
```

实测输出正确（含跨周末跳到周一）。**这是「重复规则」的完整实现**，
不用自己写「下一天/下一周/跳过周末」的算术。

[重要] **但它不解析 cron 字符串**。`croniter` **未装**。所以要么
`pip install croniter`，要么**只暴露 rrule 的语义**（每天/每周/每月 + 时刻）。

→ **判据**：cron 表达式是 1970 年代的接口，我们的用户是画布作者。
`rrule` 的「每天 9 点」「工作日 9 点」更好懂，而且**现在就能用，零依赖**。
所以先做 rrule，cron 作为以后的可选项。

#### ② 没有可用的：scheduler 库全未装

| 库 | 状态 |
|---|---|
| `apscheduler` | 未装 |
| `aiocron` / `schedule` | 未装 |

**为什么不全装 apscheduler**：它自带 job store、executor、线程池 ——
而**这三样我们都有了**（`RunStore`、`TaskQueue`、`asyncio`）。
装进来等于把已有的东西再包一层，且它的 job store 与我们的 `RunStore`
会**两个地方存运行状态**。

→ **自己写的只有「下一个时间点 + 一个 asyncio 循环」**，
触发时调用已有的 `RunManager.create`。

#### ③ 触发后：全部复用

实测 `RunManager.create(workflow_id, graph, request)` 就是启动一次运行，
而队列、预算、并发上限、事件流**全部照常生效**。
所以「计划运行」= 「触发时创建一个普通运行」，**不是第二种运行**。

[重要] **这条决定了很多事**：一次计划运行和一次手动运行**不可区分**，
所以它自动获得并发上限、预算熔断、失败隔离、运行历史 ——
不需要为调度单独实现任何一条。

#### 内部检查（按任务 23 的教训，先看脚下）

| 接缝 | 状态 |
|---|---|
| `RunManager.create` | ✅ 可调用 |
| 生命周期（`main.py` lifespan） | ✅ 有 `yield` 前后，能挂后台任务 |
| 数据库表 | ✅ alembic 就绪，加表有先例 |
| 队列 | ✅ 有 `Condition`/`acquire`/`snapshot` |
| **已有的后台任务** | ⚠️ **只有** `manager` 和 `queue` 内部的，**没有常驻循环的先例** |

→ **风险点：这是第一个常驻后台任务。** 要确认它能在 lifespan 里干净地
启动与停止，且**测试里不会泄漏** —— 这比写调度逻辑本身更容易出错。

### 1.29 [2026-09-16 核实并修正] 我今天写错的两条，与真正能拿的四块

网络恢复后逐条实测，**推翻了我自己在 §1.25 里下的两个结论**。
记在这里而不是删掉，因为「我当时怎么判断错的」比结论本身有用。

#### 修正一：`error_handler` **能拿到异常**

```python
from langgraph.errors import NodeError

def handler(state: State, error: NodeError) -> State:
    return {"status": f"recovered: {type(error.error).__name__}"}
```

`NodeError` 是 frozen dataclass：`node: str` + `error: BaseException`（**异常本身**）。
实测 `node='boom'`、`error=ValueError('...')`。

[重要] **`error` 参数是靠类型注解注入的**，和 `runtime: Runtime` 同一个机制。
我当时的错误是：只试了 `handler(state)` 一种签名，看到 `keys: ['value']`
就断言「拿不到异常」—— **我把「我没试对」当成了「它做不到」。**
无注解地写两个位置参数会 `TypeError`，这也说明它**不是**可选位置参数。

#### 修正二：跳过后继是**默认行为，不是限制**

| handler 返回 | 失败节点的后继 |
|---|---|
| 普通 dict | **跳过**（实测 `downstream` 未执行）|
| `Command(goto="after")` | **照跑**（实测 `downstream` 执行了）|

所以「它会跳过下游」只对一半。**能路由的**，只是要显式返回 `Command`。

#### 那么我们的 `on_error` 还该不该自己写？

**该，但理由变了。** 不是「上游做不到」，而是：
- 我们要的是**每节点一个声明式开关**（`stop` / `continue` / `default`），
  由**画布作者**在 Inspector 里选。`error_handler` 是**代码级**的——
  我们的图是数据，运行时才知道有哪些节点，装不了 Python 函数。
- 我们要的「继续」语义是**保留失败产出并让下游拿到兜底值**，
  而 `error_handler` 的默认是直接跳过下游。

[重要] **这是「上游有，我判断不适配」（§0 的第二种），不是「上游没有」。**
原先记成第三种是错的。

#### 真正能拿的四块（都实测过）

| 能力 | 状态 | 用途 |
|---|---|---|
| **`set_node_defaults(retry_policy=, timeout=, error_handler=, cache_policy=)`** | ✅ 实测重试生效 | **我们在编译器里手写循环配这些，可以直接换掉** |
| 已在用：`retry_policy` / `timeout` | ✅ | 无需改 |
| **`RunControl` + `request_drain()` + `GraphDrained`** | ✅ 类存在，方法 `drain_requested` / `drain_reason` / `request_drain` | **优雅关机**：超步边界停、存 checkpoint、可续跑 |
| **`Runtime.drain_requested` / `drain_reason`** | ✅ | 节点内感知「要关了」，可跳过昂贵工作 |

[重要] **`RunControl` 补齐了我们 `shutdown()` 的缺口**：我们现在是「先 drain 队列再取消任务」，
而 `RunControl` 是「跑完当前超步再停，且**存 checkpoint 可续**」。后者更强。

#### 附带：`RetryPolicy` 默认不重试 `RuntimeError` 是**设计**

`default_retry_on` 明确列出不重试的异常，含 `RuntimeError` / `ValueError` /
`TypeError` / `OSError` / `ImportError` 等。
→ 这解释了我昨天实测的「不给 `retry_on` 就不重试」——**不是 bug，是刻意**。
（`NodeTimeoutError` 反而是**默认可重试**的。）

### 1.28 [2026-09-16 官方文档核实] `Command` / `Send` / 三条会踩的规则

网络恢复后从 `docs.langchain.com` 核实，**纠正了 §1.27 里我自己的两处判断**。

#### `Command` 就是多 agent handoff 的官方做法

> `graph=Command.PARENT` … This is particularly useful when implementing
> **multi-agent handoffs**.

原文还有一条**用于 `Command` 的返回类型注解要求**：

> you **must** add return type annotations with the list of node names the node is
> routing to, e.g. `Command[Literal["my_other_node"]]`. This is necessary for the
> graph rendering and tells LangGraph that `my_node` can navigate to `my_other_node`.

→ **`Command` 的合法目的地要在返回类型里声明。** 我们的编译器是动态拼图，
所以目的地集合得从**图里的边**推出来，而不是从注解。这条决定了接入方式。

#### [重要] 一条必须是「二选一」的规则（不知道会踩）

> **[Warning]** `Command` only adds dynamic edges — static edges defined with
> `add_edge` still execute. … use either `Command` or static edges to route to the
> next nodes, **not both**.

**后果**：节点返回 `Command(goto=X)` 而它同时还连着静态边到 Y，**X 和 Y 都会跑**。
看起来像"并行"，实际是 bug。

→ **我们的编译器给每个节点都连了静态边**，所以接 `Command` 时**必须把静态边摘掉**，
否则每个 handoff 都会多跑一个不该跑的节点。

#### `Recursion limit` 与 `RemainingSteps`（任务 23 的可复用部分）

| 机制 | 说明 |
|---|---|
| `config={"recursion_limit": N}` | 独立于 `configurable` 的顶层键；默认 **10007** 超步（`langgraph._internal._config.DEFAULT_RECURSION_LIMIT`，可用环境变量 `LANGGRAPH_DEFAULT_RECURSION_LIMIT` 覆盖） |
| `GraphRecursionError` | **反应式**：超了抛异常 |
| **`RemainingSteps`**（`langgraph.managed`） | **主动式**：图内提前知道还剩几步，能优雅收尾 |

→ **这修正了 §1.27 里「23 没什么可拿」的结论**：主动熔断的机制是现成的，
`RemainingSteps` + 条件路由就是官方推荐的"快到上限时收尾"写法。
（我们自己的 `Budget` 管的是 token / 花费 / 墙钟；`RemainingSteps` 管的是**步数**。）

#### 顺带核实的两条

- `cache_policy=CachePolicy(ttl=...)` 需要 `compile(cache=InMemoryCache())` 才生效
- `add_node` 的 `timeout=` 与 `error_handler=` 文档里归在 **Fault tolerance** 一节

### 1.27 [2026-09-16 实测] 任务 23 / 24 的复用面：**24 该做，23 基本没什么可拿**

调研先于动手，结论是**两者的复用面差别很大**，所以先做哪个有了依据。

#### 24 Supervisor：`Command` 是原生 handoff，**实测可用**

`Command(goto=...)` 由**节点直接返回**，不需要条件边。四种形状全部实测通过：

| 形状 | 结果 |
|---|---|
| 节点返回 `Command(goto="specialist", update={...})` | ✅ 路由到指定节点 |
| `Command(goto=Command.PARENT)` 从子图交回父图 | ✅ 子图汇报后父图接手 |
| `Command(goto=END)` | ✅ 提前结束 |
| 我们现在的形状：分支标志 + `add_conditional_edges` | ✅ 也能跑（但多一层） |

[重要] **这一条推翻了之前记过的限制**：`reuse.md` 里写过
「`Send` 只能由**条件边**返回；节点返回 `[Send(...)]` 会 `InvalidUpdateError`」——
**那对 `Send` 成立，对 `Command` 不成立。** `Command` 就是给"节点自己决定下一步"用的。

[重要] **两者是分工，不是同一个限制**（2026-09-16 官方文档核实，见 §1.28）：

| | 返回位置 | 用途 |
|---|---|---|
| `Send` | **只能条件边** | map-reduce：按运行时长度扇出 N 个 |
| `Command` | **节点直接返回** | handoff：路由 + 改状态一次完成 |

之前只记了 `Send` 那条，容易被误推成「节点不能决定下一步」——
**实际上可以，而且官方就是用 `Command` 做 handoff 的。**

其余已装：`ToolNode`、`ToolRuntime`、`InjectedState`、`create_react_agent`、
`langchain.agents.create_agent`。**没有** `langgraph.supervisor` / `langgraph.swarm`
（这两个是独立包，未装）。

派生：`langgraph.errors` 里有 `GraphRecursionError` / `NodeCancelledError` /
`NodeError` / `GraphDrained` —— 硬停与取消的异常类型都现成。

#### 23 熔断：**没有能拿的，而且语义不同**

| 找的 | 结果 |
|---|---|
| `pybreaker` / `aiobreaker` | **未装** |
| 上游的 stop-rule / budget / abort 概念 | `langchain.agents.middleware` 与 `langgraph.types` 里**没有** |

**更重要的原因**：电路断路器的语义是**依赖不健康就别再调它**（对共享资源的健康限流）。
我们要的是**这一次运行花超了就停**（对单次运行的经济限流）。

- 断路器：跨请求、按资源、有半开探测、状态是 shared
- 我们要的：单次 run、按累计用量、无探测、状态是 per-run

**`Budget`（任务 17）已经是后者的完整基础**：四类上限 + `check()` + `BudgetExceeded`。
所以 23 剩下的只是**策略**（在哪个点停、停了之后整批怎么办），不是机制。

→ **先做 24。** 23 的"复用面"结论本身是有价值的产出：它把一个看起来该找库的任务
**判成了"不需要库，需要策略"**。

#### 附带发现：`StateGraph.add_node` 还有两个没用过的参数

| 参数 | 用途 |
|---|---|
| `cache_policy=CachePolicy` | 节点级结果缓存 |
| `trace_policy=TracePolicy` | 节点级追踪（Phase 5 用） |

### 1.26 [2026-09-16 实测] 目标追踪：**提示词与数据形状是真复用**

任务 21 的两半，可复用程度不同，实测确认：

| 要的 | 结论 |
|---|---|
| **给模型看的提示词** | ✅ **直接 import**：`WRITE_TODOS_SYSTEM_PROMPT`（1370 字）+ `WRITE_TODOS_TOOL_DESCRIPTION`（3873 字） |
| **条目形状** | ✅ **直接 import** `Todo`（TypedDict：`content` + `status`）|
| **三状态** | ✅ 取自它的 `Literal["pending","in_progress","completed"]` |
| 中间件本身 | ❌ 拿不了（hooks 是 `create_agent` 驱动的，见 §3） |

■ **提示词里值钱的东西**（这些是自己写要迭代很久的）：
- **什么时候不该用**：「少于 3 步就别用，写 todo 花时间和 token」
- 「**不要批量标记完成**」
- 「除非全完成，否则始终至少有一个 in_progress」
- 「**标记完成不等于交付答案**」—— 最终回复必须在最后一次 `write_todos` 之后单独发

■ **能 import 和只能抄是两回事**：`WRITE_TODOS_SYSTEM_PROMPT` 是**模块级常量**，
所以 `from langchain.agents.middleware.todo import WRITE_TODOS_SYSTEM_PROMPT` 就行。
抄一份就成了会漂移的副本。判据是**常量是否存在**，不是「文档里有没有写」。

### 1.25 [2026-09-16 实测] `add_node` 的容错三件套：拿了两件，第三件不能用

`StateGraph.add_node` 接受 `retry_policy` / `timeout` / `error_handler`，
**三个都实测过**。结论是**两件该拿、一件不能用**，理由都是实测出来的：

| 参数 | 结论 | 实测 |
|---|---|---|
| `timeout=` | 可用，但**我们已有** `AttemptPolicy` | 超时抛 `NodeTimeoutError`，消息带节点名和秒数 |
| `retry_policy=RetryPolicy(...)` | 可用，但**我们已有** `AttemptPolicy` | 见下面那条坑 |
| `error_handler=` | ⚠️ **能用，但语义要选对** | 见下（**我第一版结论是错的**） |

[重要] **`RetryPolicy` 的坑**：**不给 `retry_on` 就不重试**。
实测 `RuntimeError` 在 `max_attempts=5` 下**只调用 1 次**就抛出去了；
显式写 `retry_on=RuntimeError` 才真的重试 3 次。
默认只认它自己认识的那几种异常 —— 所以「配了策略但没生效」的第一嫌疑是这个。

[重要] ~~**`error_handler` 为什么不能用**~~ → **这条我写错了，见 §1.29 的修正**。
当时的实测结论有两条，**一条错、一条只对一半**：
1. ~~handler 拿不到异常~~ —— **错。** 用类型注解 `error: NodeError` 就能拿到，
   文档里写了，我当时只试了 `handler(state)` 一种签名就下了结论。
2. ~~它会跳过失败节点的后继~~ —— **只对一半。** 返回普通 dict 时确实跳过；
   **返回 `Command(goto=...)` 时是可以路由的**，实测下游照跑。
   所以这不是限制，是**默认行为**，而且可以改。

→ 所以 `on_error` 是自己写的，但**只在 catch 分支里加了一个判断**，
超时和重试继续用已有的 `AttemptPolicy`（同一套配置，没有被替换）。

### 1.3 [2026-09-16 实测] `langgraph.store.sqlite` —— 长期记忆，零服务器

一条**推翻原计划**的实测：记忆不需要等 Postgres。

| 能力 | 实测结果 |
|---|---|
| 跨重启读取 | ✅ 写入后关掉再打开同一个文件，数据还在 |
| **向量索引（语义检索）** | ✅ `SqliteIndexConfig(dims=, embed=)`，自己建 `store_vectors` 表 |
| TTL + 清理 | ✅ `sweep_ttl()` 真的删掉了过期项 |
| 命名空间 / filter / delete | ✅ 与内存版一致 |
| 依赖 | **已装**（`langgraph-checkpoint-sqlite` 带来），无需装任何东西 |

**注意两个坑**（都踩过）：
- 索引配置类**不在** `langgraph.store.sqlite` 里，而在 `langgraph.store.sqlite.base`。
  传基类的 `IndexConfig` **会被接受，然后静默退化成字面匹配** —— 必须用 `SqliteIndexConfig`。
- `supports_ttl` 在配了 TTL 之后**仍然报 False**（实测）。别信这个属性，
  直接测 `sweep_ttl()`。

→ 结论：**任务 13 的 Postgres 不再是 Phase 3 的前置。**

### 1.4 [2026-09-16 实测] embeddings：GLM `embedding-3` + 现成的 `OpenAIEmbeddings`

- `langchain_openai.OpenAIEmbeddings` **原样可用**，只改 `base_url` / `model` / `api_key`，
  **零适配代码**。实测 `embed_query` / `embed_documents` / `aembed_query` 全部返回 2048 维。
- 中文语义质量好：相似句 0.86，无关句 0.43。
- `dimensions=1024` 也支持（GLM 允许 256/512/1024/2048）。
- `sentence_transformers` + `onnxruntime` **本机也已装** → 完全离线的备选，
  不需要任何 key。适合「客户不想对外发数据」的场景。

### durability 三档（零成本，随时可开）

`graph.stream(..., durability="sync" | "async" | "exit")` —— 在性能与持久性之间选。
默认 `async`。**`exit` 是最省事的一档**：只在退出时写一次。

---

## 2. 装一下就能拿

| 能拿的 | 命令 | 用途 |
|---|---|---|
| `langgraph-checkpoint-postgres 3.1.2` | `pip install` | 任务 13 剩余（Postgres） |
| `langgraph-cli 0.4.31` | `pip install` | **M4 的答案**（见 §5） |

---

## 3. middleware：**能导入，但不是 drop-in**

> [重要] **这一条最容易误判。**

`langchain 1.4.0` 提供约 10 个预建 middleware。**但它们的全部 hooks 是**
`before_agent` / `before_model` / `wrap_model_call` / `wrap_tool_call` ——
**那是 `create_agent` 循环驱动的**。

**我们的节点不走 `create_agent`**（直接调 `ModelGateway`）。所以：

| 路子 | 代价 |
|---|---|
| 把 Agent 节点改成基于 `create_agent` | 大改，且会替换掉我们自己的 agent 循环 |
| **只抽零件** | 小改，但只能拿**自包含**的部分 |

| middleware | 我们对应的事 | 能抽零件吗 |
|---|---|---|
| `PIIMiddleware` | 凭据泄露 | **已抽**（检测器 + `apply_strategy` 可独立调用） |
| `ModelCallLimitMiddleware` | 任务 17 的一半 | 计数语义可参考，但计数器在 state 里，要自己接 |
| `ToolCallLimitMiddleware` | 同上 | 同 |
| `ModelRetryMiddleware` | 任务 18（我们做了） | — |
| `ModelFallbackMiddleware` | **我们没有** | 同上 |
| `ToolRetryMiddleware` | — | |
| `TodoListMiddleware` | 规划（Phase 4） | |
| `SummarizationMiddleware` | 长上下文 | |
| `HumanInTheLoopMiddleware` | 我们有自己的人工节点（层级不同） | — |
| `FilesystemFileSearchMiddleware` | 记忆存储层（文件搜索） | 部分 |
| `ShellToolMiddleware` | 我们没有 | 部分 |
| `ToolErrorMiddleware` | 工具失败的处理 | 部分 |
| `ContextEditingMiddleware` | 长上下文的裁剪 | 部分 |
| `LLMToolSelectorMiddleware` | 工具太多时的选择 | 部分 |
| `ProviderToolSearchMiddleware` | 同上（厂商侧搜索） | 部分 |
| `AgentMiddleware` | 基类（所有 middleware 的父类） | 部分 |

> **2026-09-14 实测修正**：本表原来写「约 10 个」，**实际 import 得到 16 个**。
> **2026-10-10 再测**：表里原来有一行 `FilesystemMiddleware` —— **该名字不存在**
> （真实名字是上一行的 `FilesystemFileSearchMiddleware`），已删。现表列 **16 行**，
> 与实测的 16 个 `*Middleware` 一一对应（含 `PIIMiddleware` 与 `ModelCallLimitMiddleware`）。
> **hooks 的结论不变** —— 实测 `ModelCallLimitMiddleware` 的
> 公开面是 `before_model` / `after_model` / `wrap_model_call` / `wrap_tool_call`
> 加上 `transformers` / `trace_policy`，确实都是 **`create_agent` 循环驱动**的，
> 所以「能导入但不是 drop-in」这个判断成立。

---

## 4. 已经拿的（有记录）

| 拿了什么 | 怎么拿的 | 记录 |
|---|---|---|
| `AsyncSqliteSaver` | 直接用，自建 provider 管生命周期 | [`../log/phase-2-5-checkpoints.md`](../log/phase-2-5-checkpoints.md) |
| `apply_strategy` + `PIIMatch` 形状 | **抽零件**，规则用我们自己有前缀的模式 | [`../log/phase-2-6-redaction.md`](../log/phase-2-6-redaction.md) |
| `adelete_thread` | 直接用（`BaseCheckpointSaver` 的公开方法） | [`../log/phase-2-5-checkpoints.md`](../log/phase-2-5-checkpoints.md) |
| **`aget_state` / `StateSnapshot.interrupts`** | 直接用 —— **但第一版是我自己写的** | [`../log/phase-2-8-resume-after-restart.md`](../log/phase-2-8-resume-after-restart.md) §2 |

### [重要] 怎么判断自己是不是在重造轮子

最后一行是**反例**：我原本手工解析 `CheckpointTuple.pending_writes`，
从 `(task_id, "__interrupt__", [Interrupt(...)])` 里抠对象。
它**能跑、有测试、还写了注释记录「interrupt 在这个字段里」** ——
**而那句话本身就是信号**：它记的是一个不该需要知道的事实。

| 判据 | 问题 |
|---|---|
| 我在读一个对象的**内部结构**（而非它的公开方法）吗？ | 是 → 先搜上游有没有直接回答这个问题的 API |
| 我的注释在解释**库的内部布局**吗？ | 是 → 同上 |
| 上游确实没有这个抽象吗？ | 没有（如 `RunStore` 的每进程能力分层）→ **不是冗余** |

三种「重造轮子」的判据见 §0 —— 只有第一种（上游有、我没找到）是负债。

---

## 5. [重要] `langgraph.json` + CLI = M4 的部署形态

```
langgraph.json          → 依赖 + graphs 映射（"<id>": "./<file>:<var>"）+ env
langgraph dev           → 本地跑
langgraph build -t tag  → 产出 Docker 镜像（FROM langchain/langgraph-api:3.11）
langgraph dockerfile    → 把 langgraph.json 翻成 Dockerfile
```

镜像用 `LANGSERVE_GRAPHS='{"agent":"...:graph"}'` 声明有哪些图。

**含义**：我们「八件套」里的 **`Dockerfile` / `requirements.txt` / 入口**，
**格式现成**。导出器只要生成 `langgraph.json` + 一个薄入口。

> [最重要 2026-09-14 实测] **`langgraph` 在本机是「源码 / 可编辑安装」**
> —— 来自 `H:\agent_1\langgraph`，而**其余全部依赖都是 wheel**（实测逐个 `importlib.metadata`）。
>
> **这直接打穿了下面 §9 计划的 `vendor/*.whl` + `--require-hashes`** ——
> **源码树没有 wheel 哈希可以固定**，于是「所有 agent 同一个底层」无法用哈希证明，
> 而那正是 M4 的核心承诺。
>
> **M4 开工前必须先解决**，两条路：
> ① 从那个源码树**构建 wheel** 再 vendor；② 改成**从 PyPI 安装** langgraph。
> 无论哪条，都要在封条生效后**重新验证**「画布能跑 + 导出物能跑」。

> 注意 **未验证**：`langchain/langgraph-api:3.11` 这个基础镜像能不能
> **免费用于生产**。文档只说「自托管 LangSmith 是 Enterprise 附加项」，
> 没说这个镜像的授权。**这卡着「队列用现成还是自建」的决策。**

---

## 6. 拿不来的（只有一样）

**队列 / worker pool / 并发上限 / 恰好一次** ——
属于 **LangGraph Platform / Agent Server**（`N_JOBS_PER_WORKER`、租约、Postgres MVCC），
而**自托管 LangSmith 是 Enterprise 附加项**（要 license key）。**开源部分没有。**

**任务 15 自己写**，但设计模型明确了：

```
1. 请求 → 往持久队列里放一个 pending run
2. worker 取走，拿租约，加载图，开始跑
3. 跑的过程中按 durability 写 checkpoint，事件走 pub/sub
4. 客户端订阅 → SSE 转发
5. 结束 → 更新状态、释放槽位
```

我们现在的实现差的就是 1、2、5 步。

---

## 7. 该抄设计、拿不到代码的

| 生态做法 | 我们该采纳的 |
|---|---|
| checkpointer（线程内）vs **Store**（跨线程）的分工 | **按这个分**，别自己发明 |
| `langgraph.json` 的形态 | 导出物清单的格式 |
| vendor + `--require-hashes` | 与 `go mod vendor` 同源，**我们已计划，是对的** |
| n8n：「读不到的凭据」只记名字、记成 requirement | **和我们 `delivery.md` 独立收敛** → 这条对 |

---

## 8. 同类产品：**没参考价值**

| 产品 | 导出产物 | 运行需要原平台吗 |
|---|---|---|
| **n8n** | `.n8np`（gzip，含 workflow/文件夹/依赖**引用**） | **需要** —— 给另一个 n8n 实例导入用 |
| **Langflow** | `/api/v1/flows/download/` → ZIP，内含 Langflow JSON | 需要（喂回平台） |

**两家都把「平台」当运行时**，所以它们**不需要**解决我们的问题。

> **「导出的 agent 独立完整运行、不依赖画布」市面上没做。**
> 这是**我们的差异点**，不是我们落后。但也意味着**这块没有现成答案可抄**。

---

## 9. 必须自己做的

| 自己做的 | 为什么上游没有 |
|---|---|
| 画布 + 节点契约 + 节点类型集（清单归 [`../reference/backend-layers.md`](../reference/backend-layers.md) §2） | 这是产品本身 |
| `EgressPolicy`（SSRF 防护） | 上游没管出网 |
| 沙盒的 Windows Job Object | 平台特定 |
| `SecretProvider` / `ToolRegistry` / `ModelGateway` | **「一个进程 = 一个能力档位」是我们的部署需求** |
| `AttemptPolicy`（节点超时/重试） | 上游有 middleware 版，但**在我们的架构里接不上**（见 §3） |
| **导出（八件套）** | **市面上没有** |

---

## 9.5 [2026-09-16 实测] 复用了多少代码：数字

**先说方法**：数字全部来自 `rglob("*.py")` 数行，排除 `__pycache__` 与自带测试。
不是估算。但**「上游有多少行」不等于「省了多少行」** —— 我们只调用其中一部分。
所以下面分两列：**站在多少代码上**（事实）与**估算的等效手写量**（判断，标明依据）。

### 站在多少代码上

| | 文件 | 行数 |
|---|---|---|
| **我们依赖的上游**（11 个包） | 747 | **427,910** |
| 我们自己的 `app/` | 87 | **25,405** |

**比例 16.8 : 1。** 其中最大的三块是 SQLAlchemy（238,753）、langchain-core
（70,148）、pydantic（45,264）—— 这三家我们基本没碰，属于"框架税"。

### 今天两刀各自站的东西

| 任务 | 上游提供 | 行数 | 我们写的 | 行数 |
|---|---|---|---|---|
| **19 长期记忆** + **21 目标追踪** | `langgraph/store/` 整个包（接口 + 内存实现 + sqlite 实现 + 向量索引 + embed 集成） | **2,718** | `app/memory/` 五个文件 | **1,672** |

**上游 store 是我们记忆代码的 1.6 倍。**

[重要] **但这个比例低估了节省**，因为两边一行不等价：
- 上游那 2,718 行里有 `batch.py` 的批量操作、`embed.py` 的三种 embedder 适配、
  两个实现类的完整 CRUD + 向量表维护 + TTL 清扫 —— 这些**不是行数问题，是正确性问题**
  （SQL 事务边界、向量维度的存储布局、异步连接的并发安全）
- 我们的 1,672 行**几乎全是参数声明和文档字符串**，逻辑部分只有几十行

### 估算的等效手写量

如果不用 `langgraph.store`，记忆 + 目标要自己实现：

| 要写的 | 最小估计 |
|---|---|
| 存储接口 + 内存实现 + sqlite 实现（含建表/迁移） | 400–600 行 |
| 向量索引（维度校验、序列化、相似度、上限剪枝） | 250–400 行 |
| 命名空间 + filter + 前缀匹配 | 150–250 行 |
| TTL + 清扫 | 100–150 行 |
| embedder 适配 | 100–150 行 |
| **小计** | **1,000–1,550 行** |

**而且这 1,000+ 行是要写对的**：SQL 事务、并发、向量维度的坑我们一个都没踩，
因为没写。

### 其他四刀的节省（粗估，依据更弱）

| 任务 | 上游提供 | 我们写的 | 说明 |
|---|---|---|---|
| 31 工具 | `langgraph.store` 复用 | ~450 | 节省主要体现在**没写第二个存储** |
| 12 i18n | `pii.py` 的抽取方式 + `render` 机制 | ~343 | 省的是"设计"，不是行数 |
| 故障隔离 | `_retry.py` 854 行的重试（我们直接用 `AttemptPolicy` 薄封装） | ~813 | **真实节省记在任务 18，不是今天** |
| 21 目标 | `todo.py` 357 行里**两段提示词** | ~821 | 省的是**调优时间**，不是行数 |

[重要] **12 / 21 的节省不是行数，是"别人已经调好了"。**
`WRITE_TODOS_TOOL_DESCRIPTION`（3,873 字）里那些规则 —— "不要批量标记完成"、
"少于 3 步就别用" —— 是自己写要反复试的。**这部分省的是时间，不是代码。**

### [重要] 不要拿这张表当成绩单

**20:1 这个比例会误导人。** 它大是因为我们装了 SQLAlchemy —— 一个数据库 ORM ——
而我们只用它的一小部分。**真正有意义的是下面这条**：

> 今天两刀里，**凡是"数据怎么存、怎么查、怎么过期"的部分，一行都不是我们写的**。
> 我们写的是"这个产品要什么参数、给出什么提示词、怎么呈现给画布"。

**这才是复用的价值：不是少打字，是少踩坑。**「一条东西两条读路径」那个失败模式
今天出现了四次，**每一次都在我们自己的代码里**，没有一次在上游代码里。

---

## 10. 依赖现状（2026-09-14 实测）

```
langgraph                          1.2.11
langgraph-checkpoint               4.2.0
langgraph-checkpoint-sqlite        3.0.3
langgraph-sdk                      0.4.2
langchain                          1.4.0   ← 本次升的（原 0.3.28，坏掉的传递依赖）
langchain-core                     1.6.0
langchain-openai                   1.6.0
openai                             3.3.1
pydantic                           2.12.4
aiosqlite                          0.22.1
```

**未装、可能需要**：`langgraph-checkpoint-postgres`、`langgraph-cli`、`deepagents`。

> 注意： 这台机器的 venv 是**共享的**，另有 `deepseek-langgraph-service`
> 声明 `langchain<0.4` / `langgraph<0.4`，**与本项目全部冲突**。
> `pip check` 在该环境报 8 条冲突 —— **其中多条在本项目升 `langchain` 之前就存在**。
> 要干净的环境只能分开建 venv。

---

## 相关

- 交付机制与八件套 → [`delivery.md`](delivery.md)
- 三层架构（为什么 runtime 要能独立） → [`vision.md`](vision.md)
- 下一刀记录 → [`../log/`](../log/)
