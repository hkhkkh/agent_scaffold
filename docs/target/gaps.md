---
title: 差距清单
status: 注意： 快照
snapshot_date: 2026-09-11
read_this: 想知道"当时测出来缺什么"时
warning: 不要当现状读！已关闭项标了 [x]
---

# 差距清单（2026-09-11 快照）

> 注意： **本文件是 2026-09-11 的快照。**
>
> 每一条都经**代码实测、带行号**，所以它是决策依据；
> 但**已关闭的项按"当时"的口吻保留**（不是现状）。
>
> **当前状态** → [`../current/status.md`](../current/status.md) ·
> **当前进度** → [`roadmap.md`](roadmap.md)

---

## [!] 完全缺失

| 能力 | 现状证据（当时） | 现在 |
|---|---|---|
| **持久化 checkpointer** | `manager.py:26,96` 用 `InMemorySaver`，**每 run 一个**（`default_factory`）。重启即丢中断状态 | [x] **已关闭**（2026-09-14）：默认已切 sqlite，真实后端实测跨重启可读回状态 → [`../log/phase-2-7-durable-default.md`](../log/phase-2-7-durable-default.md)。**resume 端点仍用不到它** → [`../log/phase-2-8-resume-after-restart.md`](../log/phase-2-8-resume-after-restart.md) |
| **数据库迁移** | **无 alembic**（`alembic.ini` 不存在），只有 `session.py` 的 `create_all()` → 改 schema 会炸 | [x] **已关闭**（2026-09-14）→ [`../log/phase-2-1-alembic.md`](../log/phase-2-1-alembic.md) |
| **任务队列 / 并发上限 / 限流** | [x] **已关闭**（2026-09-15，任务 15）—— `runtime/queue.py`，排队可见、可取消、关机 drain → [`../log/phase-2-10-queue-budget-export.md`](../log/phase-2-10-queue-budget-export.md)。原记录：每 run 一个裸 `asyncio.create_task` |
| **通用节点超时** | 只有 Code 节点有。`node_timeout_seconds` 全仓库**仅被 `code.py` 使用**；`compiler.py:213` 的 `await instance.run(ctx)` **无 `wait_for` 包裹** | [x] **已关闭**（2026-09-14，任务 18）→ [`../log/phase-2-3-node-timeout.md`](../log/phase-2-3-node-timeout.md) |
| **通用重试策略** | 无（只有 LLM 客户端自带 `max_retries`） | [x] **已关闭**（2026-09-14，任务 18）→ 同上。注意： **默认关**，因为重试是重复副作用 |
| **长时记忆** | 完全没有（无向量库、无记忆层、无 embeddings） | [x] **已关闭**（2026-09-16，任务 19）—— `app/memory/`：三层存储 + GLM embeddings 向量检索，**跨两次独立 run 实测**可检索回之前写入的事实 → [`../log/phase-3-1-five-cuts.md`](../log/phase-3-1-five-cuts.md)。原记录：完全没有 |
| **调度 / 唤醒** | 完全没有 | [x] **已关闭**（2026-09-17，任务 22）—— 定时（APScheduler 在 lifespan 里起停）+ 事件（`POST /api/hooks/<token>`），两者起的是同一条 `create + start` 路径 → [`roadmap.md`](roadmap.md) §8。原记录：完全没有 |
| **Token / 成本统计** | [x] **已关闭**（2026-09-15，任务 17）—— `runtime/budget.py` 汇总 token / 花费 / 墙钟 / 步数四类配额，超限停 run 并说明原因；用量进 `GET /runs` 的 `usage` → [`../log/phase-2-10-queue-budget-export.md`](../log/phase-2-10-queue-budget-export.md) |
| **Trace ID** | 完全没有 | [x] **已关闭**（2026-10-08，任务 25）—— W3C `traceparent` 中间件 + 日志列；已进冒烟断言 → [`../log/phase-5-1-trace-ids.md`](../log/phase-5-1-trace-ids.md) |
| **定义版本化 / 旧定义可重跑** | 没有版本历史；改图即覆盖 | [x] **已关闭**（2026-09-18，任务 28）—— `workflow_versions` 不可变快照 + `/workflows/{id}/versions`。原记录：无 |

---

## [~] 有雏形但不可靠

| 能力 | 当时现状 | 问题 | 现在 |
|---|---|---|---|
| **Run 内存管理** | `manager.py:144` `self._runs: dict` | **从不清理**（grep `_runs.pop/clear` = 0 命中）→ 单调泄漏 | [x] **已关闭**（2026-09-14）→ [`../log/phase-2-2-retention.md`](../log/phase-2-2-retention.md) |
| **并发控制** | `manager.py` 的 `self._lock` | **曾经是死代码**（除定义外 0 引用） | [x] **已关闭**（2026-09-15，任务 15）—— `_lock` 现在守着 `create` 的注册+落库临界区 → [`../log/phase-2-10-queue-budget-export.md`](../log/phase-2-10-queue-budget-export.md) |
| **故障隔离** | `compiler.py` 节点异常冒泡到 `manager._execute` | [x] **已关闭**（2026-09-16）—— 每节点 `on_error`：`stop`（默认，行为不变）/ `continue` / `default`。失败记进 `vars[node].__error__` |
| **事件流** | `runtime/events.py` 有界历史 + 丢弃慢客户端 | 设计良好（`MAX_HISTORY=2000`、背压=丢弃） | [x] 已加强（`trim()` + `earliest_seq`） |
| **SSE 重连** | 后端支持 `after` + `Last-Event-ID` | [x] 后端能力已就绪，**前端无自动重连** | [~] 后端更好了（`X-Replay-From`）；前端仍无 |
| **凭据** | `.env` → 内存 Settings；`redact()` 脱敏 | 无 Vault/KMS 抽象 | [x] 已关闭 → `SecretProvider` |

---

## [x] 值得保留的地基（当时就这么认为，现在仍然成立）

- **注册表驱动节点系统**：加节点类型 = 只改后端一个文件，前端零改动
- **单一事实源**：`graph/schema.py` → `/api/node-types` → 前端泛型渲染
- **SSE 断线可重放**（`after` + `Last-Event-ID`，含 chunk 历史上限）
- **序列化**：有环检测 + 截断（深度 8 / 200 项 / 20k 字符）
- **日志脱敏**：`redact()` 按 key 名掩码
- **校验器**：24 条规则，Tarjan SCC 做环检测（比 DFS 回边更稳定）
- **AI 一律 accept/reject diff**，绝不静默改画布
- **能力层形状已被验证五次**（`egress` / `runner` / `registry` / `gateway` / `secrets`）：
  同一个 ABC + 默认实现 + 进程级 `get/set` + Settings builder。**后续照这个做**

---

## 这份快照之后新增的认识（当时的清单里没有）

| 新增的差距 | 为什么重要 |
|---|---|
| **图组合 / 子流程** | [重要] 「画复杂 agent」的天花板。**原路线图 29 个任务里完全没有它** → [`roadmap.md`](roadmap.md) §Phase 2.5 |
| **`RunStore`** | 运行框架反向依赖平台 → [x] **已关闭**（2026-09-14，任务 2.6）→ [`../log/phase-2-4-run-store.md`](../log/phase-2-4-run-store.md) |
| **agent 自带业务工具** | 「业务处理逻辑」的一大半在这里 → [`../log/phase-6-1-agent-tools.md`](../log/phase-6-1-agent-tools.md) |
| **runtime 与平台分离 + 封条** | 「相同底层」能否可查证 → [`delivery.md`](delivery.md) §4 |
| **一个进程 = 一个能力档位** | 批量产出独立 agent 的主要障碍 → [`vision.md`](vision.md) §2 |

---

## 相关

- 当前进度与里程碑 → [`roadmap.md`](roadmap.md)
- 当前验证状态 → [`../current/status.md`](../current/status.md)
- 已知缺陷（小的具体的） → [`../current/limitations.md`](../current/limitations.md)
