---
title: 代码改动索引
read_this: 要动手改代码时
owner_of: [重要] "改哪些文件"的唯一归属地
---

# 代码改动索引

> **本目录回答一件事：每件事要改哪个文件、改在哪里。**
>
> **只列还没做的。** 任务做完后本目录的条目就删掉，完整记录回 [`../log/`](../log/) ——
> 否则同一个事实存两份（此处说改哪里、log 说实际改了哪里），必然漂移。
>
> [`../target/roadmap.md`](../target/roadmap.md) 回答「做什么、为什么、先做哪个」，
> 但**没有说改哪个文件**。结果是每次开工都要重新把代码读一遍，而且容易漏掉调用点。
>
> **本目录是路线图到代码的映射。**

---

## 每个任务文件的结构

统一五段，按这个顺序读：

| 段 | 内容 |
|---|---|
| **改哪些文件** | 具体路径（含「新增」标记） |
| **为什么是这些文件** | 漏了会怎样 |
| **具体位置** | [重要] **`anchor`** —— 一段真实存在的源码文本 |
| **怎么验证** | 要新增的测试 + 断言什么 |
| **坑** | 已知的、容易踩的 |

### [重要] 关于 `anchor`

`anchor` 是**真实源码文本**（不是 markdown 锚点）。用 `grep_search` 搜它就能定位。

```powershell
Set-Location h:\agent_scaffold\backend
Select-String -Path app\graph\compiler.py -Pattern 'started = time.perf_counter\(\)'
```

**行号会漂，源码文本不会。** 所以文件里给的行号只是 2026-09-14 的快照。

---

## 索引

按**建议执行顺序**排（理由见 [`../target/roadmap.md`](../target/roadmap.md) §12）。

### P0/P1 · 能力包 / 文件 / 会话（**已完成 2026-11-03**）

已完成（要看是怎么做的、改了什么、怎么回滚，去 log）：

- [x] **P0 能力包底座** → [`../log/phase-9-capability-packs-2026-11-03.md`](../log/phase-9-capability-packs-2026-11-03.md)
  （`app/runtime/packs/`；权限/审计/工作区；`doctor`；导出 `manifest.yml` 的 `packs:`）
- [x] **files 能力包**（10 个文件/表格/文档工具）→ 同上
- [x] **会话层 + 导出 agent `serve --http`**（含聊天页/上传/SSE）→ 同上
- [x] **平台文件 API**（`app/api/files.py`）→ 同上
- [ ] **P1-d 平台内会话 UI**（`/api/sessions` + 前端会话面板）——**未做**，
  下一步与文件落点见 [`../target/capability-roadmap.md`](../target/capability-roadmap.md) §4

### M3 · 平台能扛住批量（**已完成**）

已完成（要看是怎么做的，去 `log/`）：

- [x] 任务 18 · 通用节点超时 + 重试 → [`../log/phase-2-3-node-timeout.md`](../log/phase-2-3-node-timeout.md)
- [x] 任务 2.6 · `RunStore` 接口 → [`../log/phase-2-4-run-store.md`](../log/phase-2-4-run-store.md)
- [x] 任务 14 · 持久化 checkpoint（默认已切 sqlite）→ [`../log/phase-2-5-checkpoints.md`](../log/phase-2-5-checkpoints.md)
- [x] 内容级凭据遮蔽 → [`../log/phase-2-6-redaction.md`](../log/phase-2-6-redaction.md)
- [x] 任务 14b · 重启后的 resume → [`../log/phase-2-8-resume-after-restart.md`](../log/phase-2-8-resume-after-restart.md)
- [x] 任务 30 · 图组合（子流程） → [`../log/phase-2-9-subgraphs.md`](../log/phase-2-9-subgraphs.md)
- [x] 任务 12 · 安全/认证错误文案 i18n → [`../log/phase-1-5-security-i18n.md`](../log/phase-1-5-security-i18n.md)
- [x] 任务 31 · agent 自带业务工具 → [`../log/phase-6-1-agent-tools.md`](../log/phase-6-1-agent-tools.md)
- [x] 任务 22 · 定时 + 事件触发 → [`../target/roadmap.md`](../target/roadmap.md) §8

### M2 · 能画复杂 agent —— **已达成**

- [x] 任务 30 · 图组合（子流程） → [`../log/phase-2-9-subgraphs.md`](../log/phase-2-9-subgraphs.md)
- [x] 任务 28 · 定义版本化 → [`../log/phase-2-5-definition-versioning.md`](../log/phase-2-5-definition-versioning.md)

### M4 · 能拿出去独立部署

| 文件 | 任务 | 规模 | 依赖 |
|---|---|---|---|
| [x] 任务 27a/b/c · runtime 分层 + 源码 wheel + 封条 → [`../log/phase-6-2-runtime-split-seal.md`](../log/phase-6-2-runtime-split-seal.md) | 大 | 2.6 |
| [~] 任务 29 · 导出八件套 → [`../log/phase-6-3-export-package.md`](../log/phase-6-3-export-package.md) | 中 | 27 [x] · 31 [x]（**主要形态已完成**；仍缺 `capabilities.yaml` / `models.yaml` / `partials/` / 镜像 digest / 外置文件哈希） |

### 其余

| 文件 | 任务 | 规模 | 依赖 |
|---|---|---|---|
| [`task-13-postgres.md`](task-13-postgres.md) | Postgres 后端 + schema drift 脚本 | 中 | 13 **[~]**（schema 侧已做；**Postgres 后端未做** —— `requirements.txt` 里没有 psycopg） |
| [`known-defects.md`](known-defects.md) | 顺手该修的几处 | 小 | — |

---

## [最重要] 开工前的三步（**必须先执行，不是参考**）

**这三步在执行任何编辑之前跑完。跳过它们已经被证明会重复造轮子（2026-10-08，两次）。**

### 第 1 步 · 搜现成的（不搜不许动手）

```powershell
Set-Location h:\agent_scaffold\backend
# ① 这个能力有没有现成的注入点？
Select-String -Path app\*\*.py,app\*.py -Pattern "^def (set|get|has)_" |
  ForEach-Object { "{0}:{1}" -f $_.Path.Replace('H:\agent_scaffold\backend\',''), $_.Line.Trim() }
# ② 我要解决的问题，附近有没有人解决过？
Select-String -Path app\*\*.py -Pattern "<关键词>" | Select-Object -First 20
```

**现有注入点清单**（新 seam 照抄这个形状，别另发明）：

| 模块 | 注入点 |
|---|---|
| `app/graph/subgraphs.py` | `set_subgraph_provider` · `has_subgraph_provider` · `load_subgraph` ← **模板** |
| `app/runtime/workflows.py` | `set_workflow_loader` · `has_workflow_loader` · `load_workflow` |
| `app/runtime/runs.py` | `set_run_store` · `set_database_run_store` |
| `app/runtime/checkpoints.py` | `set_checkpointer_provider` |
| `app/runtime/egress.py` | `set_egress_policy` |
| `app/runtime/queue.py` | `set_task_queue` |
| `app/runtime/runner.py` | `set_sandbox_runner` |
| `app/runtime/ai/gateway.py` | `set_model_gateway` |
| `app/security/secrets.py` | `set_secret_provider` |
| `app/memory/store.py` | `set_memory_store_provider` |
| `app/tools/registry.py` | `set_tool_registry` |
| `app/runtime/packs/registry.py` | `set_pack_registry` · `builtin_registry` · `register_builtin_tools`（2026-11-03） |
| `app/runtime/permissions.py` | `set_permission_policy` · `build_permission_policy`（2026-11-03） |
| `app/runtime/audit.py` | `set_audit_recorder` · `build_audit_recorder`（2026-11-03） |

**形状固定为**：模块级 `_X: Callable | None = None` + `set_X(fn | None)` + `has_X()` + 实际调用的函数。
**禁止**：自己写 ABC / Provider 类 / Null 对象（已验证是过度设计，同一问题多写 4 倍代码）。

**这条有门禁兜底**：`tests/test_layer_boundary.py::test_a_definition_seam_uses_the_function_shape`
会解析 `graph/subgraphs.py` 与 `runtime/workflows.py`，发现 `ABC` / `*Provider` 类就 FAIL。
它自带反向验证（`test_the_seam_convention_would_catch_the_class_version`），
**2026-10-08 实测：注入一个违规类后确实报 FAIL 并指名类名。**

### 第 2 步 · 确认层与边界

确认改的是**哪一层**（[`../target/vision.md`](../target/vision.md) §3）。
**层 2（runtime）里出现 `app.db` / `app.api` / `fastapi` / `sqlmodel` 一律算错，
即使是函数内的惰性导入** —— 导出是源码级复制，惰性行照样被复制走。
门禁：`tests/test_layer_boundary.py`。

### 第 3 步 · 保护工作树

检查 `git status`，保留已有改动；不用 `reset` / `checkout` 覆盖工作区。

---

## [重要] 改代码的纪律（这个项目的既有规则）

| 时机 | 做什么 |
|---|---|
| **改之前** | [最重要] **先找现成的** —— 能复用优先用已有的（[`../target/reuse.md`](../target/reuse.md) §0）。**动手写抽象层 / 解析器 / 适配器之前先搜一遍上游 API**；不要读库的内部结构（那是重实现上游） |
| **改之前** | [重要] 检查 `git status`，保留已有改动；不要用 `reset` / `checkout` 覆盖工作区 |
| **改之前** | 确认改的是**哪一层**（[`../target/vision.md`](../target/vision.md) §3 三层）—— 层 1 和层 2 的改动标准不同 |
| **改之前** | 查 [`../reference/design-decisions.md`](../reference/design-decisions.md) —— 别破坏一条已经想清楚的决定 |
| **改之后** | 立刻跑 `pytest tests/ --tb=short`（**不要加 `-q`**，`pytest.ini` 已有，`-qq` 会吞掉汇总行） |
| **改之后** | 新加守卫 → **必须加反向验证用例**到 `_guardprobe_capability.py`，并确认它会 FAIL |
| **改之后** | 跑 `python _guardprobe_capability.py`，确认 `SOURCES_RESTORED=True` |
| **改之后** | 更新 [`../current/status.md`](../current/status.md)（数字）与 [`../current/files.md`](../current/files.md)（文件清单） |
| **每次变更/进度** | 记入对应 [`../log/`](../log/)：日期、实际改动、涉及文件、验证、未完成项、下一步；完成任务保留回滚索引 |
| **不确定时** | [重要] **先问这个测试有没有可能失败** —— 本项目的头号教训 |

### 头号教训（三条，都来自真实事故）

1. **写守卫必须反向验证。** 故意制造一个它该抓的错误，确认报 FAIL。
   本项目因此抓到 **5 次**严重问题，其中一次是「**守卫本身是虚构的**」。
2. **不可证伪的测试 = 永远绿的装饰品。** 症状和「产物没 bug」完全一样。
3. **遇到同一个 bug 第二次修复也失败 → 停止改代码，先写最小复现。**

→ 全部展开在 [`../lessons/process.md`](../lessons/process.md)

---

## 相关

- 做什么 / 为什么 / 先做哪个 → [`../target/roadmap.md`](../target/roadmap.md)
- 要变成什么样 → [`../target/vision.md`](../target/vision.md) ·
  [`../target/delivery.md`](../target/delivery.md)
- 某一层怎么实现的 → [`../reference/backend-layers.md`](../reference/backend-layers.md)
- 设计决策的理由 → [`../reference/design-decisions.md`](../reference/design-decisions.md)
