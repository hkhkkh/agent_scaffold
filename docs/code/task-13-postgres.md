---
title: 任务 13 剩余 · Postgres + schema drift 脚本
scale: 中
depends: 13 [x]（alembic 已完成）
phase: 2
---

# 任务 13 剩余 · Postgres + schema drift 脚本

**alembic 部分已完成** → [`../log/phase-2-1-alembic.md`](../log/phase-2-1-alembic.md)

---

<a id="t13a"></a>

## 13a · Postgres 后端

### 改哪些文件

| 文件 | 改什么 |
|---|---|
| `app/db/session.py` | 已按 URL 缓存 —— 确认 `postgresql+psycopg://` 走同一条路 |
| `app/settings.py` | `database_url` 已经支持 |
| `migrations/env.py` | 已有 `render_as_batch=_is_sqlite(url)` —— **确认 Postgres 下不加 batch** |
| `requirements.txt` | 加 `psycopg[binary]` |
| **新增** `scripts/` | 一个「起一个临时 PG 跑全量测试」的脚本 |

### 注意： 最大的风险

**`TestTheTwoPathsAgree` 要在两种数据库上都跑。**

理由：SQLite 的 `create_all()` 和 alembic 迁移在 PG 上可能产生
**不同**的 schema（类型名、索引名、约束名）。

**这是这一项最容易出问题的地方** —— 而且它在 SQLite 上是绿的。

### 怎么验证

| 测试 | 断言什么 |
|---|---|
| 两条路径在 PG 上一致 | `TestTheTwoPathsAgree` 改用 `postgresql://` 也通过 |
| 迁移在 PG 上可跑 | `alembic upgrade head` 在空 PG 上成功 |
| 降级也在 PG 上可跑 | `alembic downgrade base` 成功（反向迁移常被忽略） |
| 应用能连 PG | 起一个真实 run，历史正确落库 |

---

<a id="t13b"></a>

## 13b · `scripts/check-schema-drift.ps1`

### 改哪些文件

| 文件 | 改什么 |
|---|---|
| **新增** `scripts/check-schema-drift.ps1` | 后端 JSON Schema ↔ `frontend/src/types/workflow.ts` 对比 |
| `frontend/src/types/workflow.ts` | 文件头**已注明**该命令（现在指向一个不存在的脚本） |

### 为什么重要

TS 镜像是**手工维护**的，会**静默漂移**。

**症状**：某个 Inspector 字段**永远不显示**。

**而极难回溯到原因** —— 你会先去查后端 schema、再查泛型渲染、
再查前端类型，最后才发现是镜像漏了一个字段。

### 做法

```
1. 从后端 dump 一份 JSON Schema
   GET /api/node-types → 或直接 Python import 后 model_json_schema()
2. 从 workflow.ts 提取字段名集合（正则或 TS 编译器 API）
3. 对比字段名与必填性 → 不一致就非零退出
```

> **不用做完整的类型级对比**（那需要 TS 编译器）。
> **只要字段名和必填性**就够抓住绝大多数漂移。

### 怎么验证

| 测试 | 断言什么 |
|---|---|
| 当前一致 | 直接跑 → 退出码 0 |
| [重要] **故意漏一个字段** | 从 `workflow.ts` 删一个字段 → 脚本**报错并指出是哪个** |
| [重要] **故意多一个字段** | 加一个后端没有的 → 同样报错 |

> **第 2、3 条是必须的** —— 否则你不知道这个脚本**有没有在真的检查**
> （见 [`../lessons/process.md`](../lessons/process.md)：检查全绿不代表检查有效）。

---

## 坑

1. **`psycopg` vs `psycopg2`** —— 用 **`psycopg`（v3）**，
   SQLAlchemy 2.0 的 URL 前缀是 `postgresql+psycopg://`。

2. **PG 的 identifier 长度限制 63 字符** ——
   自动生成的索引名（`ix_runs_workflow_id`）够短，但手工命名时要小心。

3. **`render_as_batch` 只在 SQLite 需要** ——
   PG 支持真正的 `ALTER TABLE`。如果 batch 泄漏到 PG，
   会生成一堆「建临时表 + 拷数据」的迁移，**慢且会丢外键**。

4. **不要删除 SQLite 支持** —— 它是**开发默认**（零依赖可跑）。
   PG 是**生产默认**。两者都要维持。

---

## 回滚索引

- `requirements.txt`：删 `psycopg[binary]`
- **新增** `scripts/check-schema-drift.ps1`（删除）
- `migrations/env.py`：如果改了 `_is_sqlite` 的判断，还原

---

## 相关

- 已完成的 alembic 部分 → [`../log/phase-2-1-alembic.md`](../log/phase-2-1-alembic.md)
- 为什么必须做 → [`../target/gaps.md`](../target/gaps.md)
- 相关：`RunStore` 的 PG 实现 → [`../log/phase-2-4-run-store.md`](../log/phase-2-4-run-store.md)
