---
title: 交付物与交付机制
read_this: 要知道"导出的 agent 里有什么 / 怎么保证同一底层 / 导出怎么走"
---

# 交付物与交付机制

---

## 1. [重要] 交付物清单

**八样里已落 7 样**（2026-10-09 逐项核对）：只有第 3 项部分，其余七件齐 —— 见下表「现状」列。

> **不是「点和边的关系」** —— 业务逻辑（提示词、Python 代码、表达式）**已经在 JSON 里**，
> 这一点今天成立、不需要改。**缺的是「包」。**

| # | 内容 | 是什么 | 现状 |
|---|---|---|---|
| 1 | `agent.yaml` | 图定义 + **业务逻辑**（逻辑用 `!ref` 外置） | [x] 已实现（任务 29；原为 `agent.json`） |
| 2 | `manifest.yml` | 能力声明：要哪些工具、哪个沙盒档位、要哪些密钥**名** | [x] 已实现（任务 29）—— 只含 `tools` / `sandbox` / `credentials` 三项 |
| 3 | `config/` + `.env.example` | 客户适配改的**接口地址 / 模型 / 数值** | [~] `.env.example` 已有；`config/*.yaml`（models / limits / capabilities）未生成 |
| 4 | `tools/` | [重要] **业务工具** —— 客户接口适配的主要地方 | [x] 已实现（任务 31） |
| 5 | `main.py` | 入口：读输入 → 编译 → 跑 → 输出 | [x] 已实现（任务 29，`run` / `serve` / `selfcheck`） |
| 6 | `requirements.txt` | 依赖声明（含**哈希封条**） | [x] 已实现（任务 27c；封条锁存在时写 sealed 版本） |
| 7 | `Dockerfile` | 怎么部署 | [x] 已生成（两阶段 deps/runtime 构建）；§3 那种「同 Dockerfile 出 agent + sandbox 两个 tag」的形态尚未做 |
| 8 | `README.md` | 怎么跑、怎么改、怎么调 | [x] 已生成（`_readme()` 产出运行 / 配置 / 变更指南） |

### 不在包里

| 不在包里 | 为什么 |
|---|---|
| 画布前端（React） | 层 1，构建时用 |
| `app/api/`、`app/main.py` | 层 1，平台的 HTTP 表面 |
| `app/db/`（SQLite、`Run`/`RunStep` 表） | 层 1，给 UI 看运行历史 ← [`vision.md`](vision.md) §4 那个耦合 |
| `ai_buildtime/`（`gen_graph.py` 等 5 个） | 层 1，构建时能力（27a 后从 `app/ai/` 拆出） |
| `security/deps.py` | 层 1，FastAPI 中间件 |
| **runtime 本体** | 层 2，是**依赖**（或基础镜像），不是拷贝 |

---

## 2. [重要] 第 4 项展开：业务工具（**已解**，任务 31）

「业务处理逻辑」有**两半**：

| 哪一半 | 在哪 | 今天 |
|---|---|---|
| 节点内的逻辑 | 提示词 / Python 片段 / 表达式 → **在 JSON 里** | [x] 成立 |
| **调业务系统**（查订单库、提交退款） | 需要一个**工具** | [x] 已成立（任务 31） |

runtime 里通用工具是 **6** 个，全部是**运行时代码**：

```
calculator · current_time · http_request · json_query · text_stats · web_search
```

`register_tool()` 是**进程级**的，所以当时的缺口是：**一个 agent 无法「自带」它自己的业务工具**；
而典型的业务 agent 恰恰是「调自己的 API / 查自己的库」，这必须是 **agent 包里的代码**，而不是 runtime 里的。
**这个机制当时不存在，而且原路线图里没有对应任务。**

现已成立（任务 31）：工具源码存在 `WorkflowGraph.tools`（模块名 → 源码），
导出时写成 `tools/<name>.py` 并由 `tools/__init__.py` 导入；运行时通过
`load_tools_from` 装进**全新隔离的注册表**，不会污染宿主进程。
画布侧：`POST /workflows/tools/check` 检查、保存时校验形状、
`GET /workflows/{id}/tools` 读取。
→ [`../log/phase-6-1-agent-tools.md`](../log/phase-6-1-agent-tools.md)

---

## 3. [重要] 完成态：导出的 agent 长什么样

**这是一个独立的 git 仓库**，客户能读、能改、能 `docker build`。

```
my-agent/
├── agent.yaml               ① 图 + 业务逻辑（逻辑用 !ref 外置）
├── agent.lock               ② 版本锁定：runtime / 镜像 digest / 图哈希 / 子图版本
├── prompts/                 ③ 提示词独立成 .md
│   ├── extract.md
│   └── partials/house_style.md       ← 可被多段引用
├── code/                    ④ 代码节点独立成 .py（可单测、可 IDE 检查）
│   └── assess_risk.py
├── tools/                   ⑤ [重要] 业务工具 —— 客户接口适配的主场
│   ├── __init__.py                  在这里 register_tool() 声明本 agent 的工具
│   ├── lookup_regulation.py
│   └── _shared/client.py            他们自己的 API 客户端
├── config/                  ⑥ [重要] 客户改的配置
│   ├── .env.example                 接口地址 / 密钥**名**（值绝不进包）
│   ├── models.yaml                  用哪个模型、温度、超时
│   ├── limits.yaml                  预算 / 超时 / 重试 / 并发
│   ├── capabilities.yaml            [重要] 能力开关盘
│   └── secrets/                     生产挂载点（.gitignore）
├── tests/                   ⑦ 客户改完能自己验证
│   ├── test_agent.py                图能编译
│   ├── test_tools.py                [重要] 业务工具单测
│   └── test_e2e.py                  端到端
├── main.py                  ⑧ 入口
├── requirements.txt         ⑨ agent-scaffold-runtime==1.2.0（带哈希封条）
├── vendor/                  ⑩ runtime 的 whl（离线可装）
├── Dockerfile               ⑪ 多阶段 → agent 镜像 + sandbox 镜像（同批 layer）
└── README.md                ⑫ 怎么跑 / 改 / 调 / 升级
```

### `config/capabilities.yaml` —— 能力开关盘

**这是「同底座不同档位」的落点**，形状每个 agent 完全一样：

```yaml
sandbox:
  runner: container           # local | container
  memory: 512MB
  timeout: 120s
  image: ghcr.io/ourorg/agent-sandbox:1.2.0

egress:
  allow_private: false
  allowed_hosts: [api.internal.corp]
  max_response_bytes: 2MB

tools:
  allow: [lookup_regulation, query_orders]   # 空 = 全部
  deny: []

model:
  gateway: direct             # direct | quota

credentials:
  provider: file              # settings | file | vault
  file: /run/secrets/agent.env

runs:
  store: postgres             # memory | sqlite | postgres
  retention: 30d

checkpoints:
  store: postgres             # 重启可恢复

memory:                       # Phase 3
  store: pgvector             # none | sqlite-fts | pgvector
  scopes: [episodic, semantic]

telemetry:
  tracer: otel                # logs | otel
  endpoint: http://collector:4317
```

### 四种触发方式（`main.py` 的 CLI）

**这就是「对接业务接口」**：

| 命令 | 用途 |
|---|---|
| `agent run --input '{...}'` | 命令行单次跑（调试 / 批处理） |
| `agent serve --port 8080` | [重要] **HTTP 服务** |
| `agent schedule` | 常驻，按时 / 事件唤醒（Phase 4） |
| `agent migrate` | 建/升级它自己的表（`RunStore` / 记忆 / checkpoint） |

**HTTP 接口形态**：

```
POST /v1/invoke              → 同步：给输入，等结果
POST /v1/runs                → 异步：返回 run_id
GET  /v1/runs/{id}           → 查状态
GET  /v1/runs/{id}/events    → SSE 流
POST /v1/runs/{id}/resume    → 人工输入节点恢复
GET  /healthz /readyz        → 探针
GET  /metrics                → 指标
```

> **注意**：这套接口**不是画布那套 API 的复制**。
> 它是 agent 自己的接口，形态由 `capabilities.yaml` 的 `entry` 决定。
> **画布那套还在画布里，与它无关。**

### 运行时数据（不在包里，挂卷）

```
/var/lib/my-agent/
├── memory/          记忆（生产：Postgres + pgvector）
├── checkpoints/     中断状态（重启可恢复）
├── runs/            运行历史（RunStore）
└── transcripts/     每次运行的完整事件流（给排障用）
```

### runtime 包内部（成品形态，11 个能力接口）

```
agent_scaffold_runtime 1.2.0
├── graph/           schema · compiler · state · validate
├── nodes/           16 种（12 + 记忆 + 目标 + Supervisor + 子图）
├── exec/            orchestrator · attempts · events · serialize
├── capabilities/    [重要] 每个都是 ABC + 两种实现 + get/set
│                    sandbox · egress · secrets · models · tools ·
│                    runs · checkpoints · queue · memory · scheduler · telemetry
├── security/        认证原语（哈希 · scope · 常量时间比较 · 入口中间件）
├── settings.py      配置加载（按 capabilities.yaml 收窄）
└── cli.py           run / serve / schedule / migrate / healthcheck
```

### 三条性质，成品同时具备

| 性质 | 由什么保证 |
|---|---|
| **自己就是完整的** | 一个进程 / 一个镜像，画布不在场 |
| **底座是一份实现** | 哈希封条 —— **改过就装不上** |
| **是可改的** | 提示词 `.md` · 代码 `.py` · 工具 `.py` · 配置 `.yaml`，客户工程师都动得了手 |

---

## 4. [重要] 交付机制：怎么保证「相同底层」是事实而不是承诺

### 方案：**声明 + 随包 + 封条**（三者一起，缺一不可）

```bash
# 构建时（有网的机器上，一次性）
pip download -r requirements.txt -d vendor/ --only-binary=:all:
pip hash vendor/agent_scaffold_runtime-1.2.0-py3-none-any.whl
#   → sha256:4f3a...

# requirements.txt 写成带哈希的形式
agent-scaffold-runtime==1.2.0 \
    --hash=sha256:4f3a...

# 客户那台（可能完全没网）
pip install --no-index --find-links vendor/ --require-hashes -r requirements.txt
```

| 要素 | 买到什么 |
|---|---|
| `==1.2.0` | 版本明确（**声明**） |
| `vendor/*.whl` | 离线可装（**随包**） |
| `--require-hashes` | **字节相同 · 改过就装不上**（**封条**） |
| `--no-index --find-links` | 不访问网络 |

> **最后两条是关键**：哈希让「相同底层」从**承诺**变成**安装时强制校验的事实**。

### 更常见也更干净的变体：runtime 做成基础镜像

```dockerfile
FROM python:3.13-slim AS runtime           # runtime 仓库构建并发布
COPY runtime/ /runtime/
RUN pip install /runtime

FROM ghcr.io/ourorg/agent-runtime:1.2.0 AS agent
COPY agent.yaml agent.lock manifest.yml prompts/ code/ tools/ config/ /app/
ENTRYPOINT ["python", "-m", "agent"]
```

runtime 成为 `FROM` 那一行 —— **交付物里 runtime 的字节一个都没有**，
客户拿不到也就改不了。

**顺手解决「第二个交付物」**：容器沙盒本来要单独交付一个镜像。
同一个 Dockerfile 用多阶段构建出两个 tag：

```dockerfile
FROM runtime AS agent     # 带 agent 代码 + 配置
FROM runtime AS sandbox   # 只有 runtime，无 agent 代码、无密钥
```

一次构建 → 两个 tag → **同一批 layer digest**。

### 工程上必须写明的两条约定

1. **升级 runtime = 重新导出**，不允许客户手改 runtime 文件。
   否则哈希校验形同虚设。真要改 runtime，那是**改模子**（属于我们），不是适配。
2. **多版本共存是正常的**（npm / pip / cargo 的常态）。
   20 个 agent 锁着不同 runtime 版本不是问题，而是特性：升 A 不影响 B。
   补丁的代价是要推 N 次，但**两种方案都可脚本化**。

### 行业参照

| 生态 | 命令 | 产物 |
|---|---|---|
| Python | `pip download` + `--require-hashes` | `vendor/*.whl` |
| Node | `npm ci --offline` | `node_modules` + lockfile |
| **Go** | `go mod vendor` + `go mod verify` | `vendor/`（**官方支持的交付形态**） |
| Rust | `cargo vendor` | `vendor/` |
| Java | `mvn dependency:go-offline` | 本地 `.m2` 或 uber-JAR |

**Go 是最好的参照**：`vendor/` 是官方交付形态，可离线构建，`go mod verify` 校验哈希。

---

## 5. [重要] 导出流程：从画布到交付物

### 八步

| 步 | 发生在 | 做什么 | 今天 |
|---|---|---|---|
| **0** | 画布 | 画节点连线 · 写描述 · AI 补齐 · 在 Inspector 调参 · 试跑 | [x] |
| **0.5** | 画布 | [重要] **写业务工具** + 填**能力面板**（沙盒档 / 出网白名单 / 密钥名） | [~] 工具已支持（31 [x]）；**能力面板 UI 仍无** |
| **1** | 画布 | 保存（定义落 SQLite + `data/workflows/<id>.json`） | [x] |
| **2** | 画布 | **冻结版本**：定义 + 引用的提示词/代码 + 工具 + 配置 | [x] 定义版本化（28）；导出物含 `agent.lock`（图哈希 + runtime 哈希） |
| **3** | 导出器 | **拆文件**：把逻辑从定义里外置出去 | [x] 任务 29（`prompts/*.md` · `code/*.py` · `!ref`） |
| **4** | 导出器 | **写脚手架**：`main.py` / `Dockerfile` / `README.md` / `tests/` / `config/` | [~] `main.py` [x] · `Dockerfile` [x] · `README.md` [x] · `tests/test_agent.py` [x]；只剩 `config/*.yaml`（导出器现出 `config/.env.example`） |
| **5** | 导出器 | **装底座 + 封条**：`pip download` → `vendor/` + 哈希 | [x] 任务 27c（sealed `requirements.txt`） |
| **6** | CI | **门禁**：空环境安装 → 启动 → 冒烟 | [ ] |
| **7** | 交付 | 仓库 + 两个镜像 + 部署说明 + 升级说明 | [ ] |

### [重要] 第 0.5 步的现状（**已解**，2026-10-09）

「**在画布上能写业务工具**」**已解**（任务 31，详见 §2）。
→ [`../log/phase-6-1-agent-tools.md`](../log/phase-6-1-agent-tools.md)

**仍缺**的是**能力面板 UI**（沙盒档 / 出网白名单 / 密钥名），它落在第 3 项的
`config/*.yaml` 上，尚未生成。

### 第 3 步「拆文件」：画布里的东西各去哪

| 画布里的 | 变成包里什么 | 为什么 |
|---|---|---|
| 提示词节点的 `template` | `prompts/<node-id>.md` | 独立文件 → 语法高亮、git diff 看得清改了哪句 |
| 代码节点的 `code` | `code/<node-id>.py` | 普通 Python → 能单测、能被 IDE 检查 |
| 条件节点的 `expression` | **留在** `agent.yaml` | 短表达式拆出去反而更难读 |
| 工具节点的 `tool` 名 | `agent.yaml` 引用 + `capabilities.yaml` 的 allow 列表 | 实现住在 `tools/` |
| Inspector 里调的参数 | `config/*.yaml` | 与「逻辑」分开：逻辑很少改，配置常改 |
| `Settings` 的能力项 | `config/capabilities.yaml` | 这个文件就是「能力开关盘」 |
| 子图引用（Phase 2.5） | `agent.yaml` + `agent.lock` 的 `subgraphs:` | 引用 + **版本锁定** |

### [重要] 导出时的三个安全点（都是"不能自动化"的地方）

| # | 规则 | 为什么 |
|---|---|---|
| 1 | **密钥的值绝不进包**，只进**名字**（`.env.example` 里是占位符） | 一个进了 git 的密钥等于泄露；必须由部署方注入 |
| 2 | **`trusted` 标记必须重新显式确认**（⚠️ **尚未实现**） | 画布上的 `trusted` = 「**我**（开发者）接受这段代码以我的用户身份跑」。交付给客户是**另一件事**，不能继承。**代码里还没有这一步** —— `runtime/export.py` 只有 `_strip_credentials`，`trusted` 原样写进导出物 |
| 3 | **能力默认值要保守**（`egress_allow_private: false`、`sandbox_runner: container`） | 导出是**面向客户的**，默认应当是安全的那个；放宽必须是显式动作 |

### 第 6 步的门禁：这是「相同底层」被真正保住的地方

```
导出 → 在干净环境里：
   ① pip install --no-index --find-links vendor/ --require-hashes
   ② python -m agent selftest          ← 图能编译、工具能加载、配置能解析
   ③ docker build .                    ← 镜像能构建
   ④ 跑一次冒烟（mock 模型即可）          ← 端到端能跑通
   ⑤ [重要] 断言：包内没有对平台的引用
```

**第 ⑤ 条是整件事的核心。** 它把「agent 不依赖画布」从架构原则变成**可失败的门禁**。

实现方式：一个测试 grep runtime 包源码，

```python
FORBIDDEN = ("from app.api", "from app.db", "from fastapi", "from sqlmodel")
```

发现任一就 FAIL。那样的反向依赖会在**写下的当天**被挡住 —— 本项目就出过一次
（`manager.py` 的 `from app.db`），出处与修法见
[`../log/phase-2-4-run-store.md`](../log/phase-2-4-run-store.md)。

### 重复导出怎么走

```
改画布 → 再导出 → git diff 两个版本
                      │
                      ├── 只有 config/ 变了  → 配置发布
                      ├── prompts/ / code/ 变了 → 代码发布
                      ├── agent.yaml 变了   → 定义发布（要重跑门禁）
                      └── runtime 版本变了   → 底座升级（全量重测）
```

**这就是为什么需要「定义版本化」** —— 没有版本，两个导出之间无法比较。

> 注意： **反向怎么走（客户改完还能不能拿回画布）是待决** ——
> 见 [`decisions.md`](decisions.md) 第 1 条。
> 它决定 `agent.yaml` 是**权威**还是**派生物**。

---

## 6. `agent.yaml` 的形态（示意）

不再是转义过的 JSON，而是**人类可写的 YAML**，且**逻辑外置成文件引用**：

```yaml
apiVersion: agent/v1
name: contract-review
version: 1.3.0

entry: { mode: http, path: /review }

graph:
  nodes:
    - id: input
      type: start
      inputs: { text: "string", tenant: "string" }

    - id: extract
      type: llm
      prompt: !ref prompts/extract.md            # ← 逻辑在文件里
      model: !ref config/models.yaml#primary
      output: clauses

    - id: lookup_law
      type: tool
      tool: lookup_regulation                     # ← 指向 tools/ 里的业务工具
      args: { clauses: !ref extract.clauses }
      timeout: 30s

    - id: assess
      type: code
      code: !ref code/assess_risk.py              # ← 代码在文件里
      requires_trust: true

    - id: review
      type: human_input
      message: "确认风险评级"
      fields: { approve: boolean, comment: text }

    - id: out
      type: end
      output_label: memo

  edges: [ ... ]

state:
  clauses: { type: json }
  risk: { type: json }
```

**`!ref` 是核心** —— 提示词和代码住在**独立文件**里，
编辑器有语法高亮、git diff 能看清改了哪句。

---

## 相关

- 三层架构与约束 → [`vision.md`](vision.md)
- 怎么走到这 → [`roadmap.md`](roadmap.md)
- 具体改哪些文件 → [`../code/README.md`](../code/README.md)
- `agent.yaml` 形态的待决 → [`decisions.md`](decisions.md)
