---
title: 顺手该修的已知缺陷
scale: 小
read_this: 想找"小活儿"时
---

# 已知缺陷（小的、具体的）

> 大的在 [`../current/limitations.md`](../current/limitations.md)；
> 这里只放**能一次改完的**。

> **只列还开着的。** 修完的条目删掉，记录留在
> [`../log/phase-2-11-small-defects.md`](../log/phase-2-11-small-defects.md) ——
> 否则同一个事实存两份（此处说怎么改、log 说实际怎么改），必然漂移。
> 别处会引用这里的编号，所以**开着的**条目在标题里标「（原 #N）」。

**已修掉的**（原 #1、#2、#3、#4、#5、#7、#8 —— 含改法与日期）→
[`../log/phase-2-11-small-defects.md`](../log/phase-2-11-small-defects.md) §1 / §2 / §7 / §9 / §11。

---

## 1. 容器沙盒镜像**从未构建过**（原 #6）

**文件**：`backend/docker/sandbox.Dockerfile`

**问题**：写它的时候 Docker daemon 没在跑，**至今仍未验证**
（2026-09-17 复查：`npipe:////./pipe/dockerDesktopLinuxEngine` 仍连不上）。
**所以 Dockerfile 本身是未验证的**（文件头已写明）。

**改法**：起一次 Docker daemon，`docker build` 它。

> 注意： **镜像里只放 4 个文件的清单是手写的**
> （`app/__init__.py`、`app/runtime/__init__.py`、`sandbox.py`、`sandbox_child.py`）——
> **改 import 时要同步**，否则镜像里 `ImportError`。

**验证**：build 成功 + 用 `SANDBOX_RUNNER=container` 跑一个 Code 节点。

---

## 2. [重要] `api_key` 明文落盘 —— **但先别改**（原 #9）

**文件**：`app/db/repository.py`（`export_workflow`）

**问题**：节点配置里的 `api_key` 会**原样**写进
`data/workflows/<id>.json`。`redact()` 在界面和事件里会遮，**磁盘不遮**。
（2026-09-17 复查：`export_workflow` 逐字写 `model_dump()`，仍然如此。）

### 注意： 定位已定，所以**先别改**

| 如果 `data/workflows/*.json` 是… | 那么 |
|---|---|
| **灾备副本**（当前设计） | **保留真值是合理的** —— 恢复时需要真值 |
| **交付给客户的产物** | **必须遮** —— 密钥值绝不能进包 |

[`../target/decisions.md`](../target/decisions.md) **待决 #1**（`agent.yaml` 是权威还是派生物）
**已定（2026-10-08）：B · 导出即源码** —— 交付物是「定义 + 外置源码（`!ref`）」形态，
**`data/workflows/*.json` 是本机灾备副本、不是交付产物**，所以按今天的设计**保留真值是合理的**；
一旦它被当作交付内容，就必须遮。

**在它变成交付物之前不要动它** —— 修错方向的代价是丢了灾备能力。

---

## 3. 修复**运行期**错误（原 #10）

> **原文件的汇总里漏了这一条** —— 它既不在「已修」也不在「仍开」，
> 只在正文里。2026-09-17 复查时发现，补上。

**现状**：`repair-graph` 只修**静态校验**错误。
节点*跑失败*时的 traceback 还修不了。（`app/ai_buildtime/` 下无 `traceback` 引用，实测。）

**为什么需要另一个数据源**：静态校验看的是图；
运行期失败看的是 `RunStep.error` / transcript。

**改法**：新的 AI 端点，输入是 `RunStep` 列表（含 error），
输出是「建议改哪里」的 diff。

---

## 相关

- 更大的限制 → [`../current/limitations.md`](../current/limitations.md)
- 待决 #1 → [`../target/decisions.md`](../target/decisions.md)
- 完整的任务索引 → [`README.md`](README.md)
