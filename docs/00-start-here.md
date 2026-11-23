---
title: 新会话从这里开始
read_this: 每次新会话第一份
next: env/run.md
---

# 从这开始（5 分钟）

> **本文件的唯一目的**：让一个没有上下文的新会话在 5 分钟内知道
> ① 现在是什么状态 ② 下一步该做什么 ③ 有哪些坑不能踩。
>
> 需要更细的内容时，本文件会指向具体文件。
>
> **上下文压缩恢复按新会话处理。** 继续任何工作前，按 [`README.md`](README.md) §9
> 完整重读项目基线，并检查 `git status`；不得只依据压缩摘要或记忆继续改动。

---

## 三步

### 第 1 步 · 确认源码状态（30 秒）

```powershell
Set-Location h:\agent_scaffold\backend
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' -m pytest tests/ --tb=short
```

**期望全部通过。** 如果不是，先看 [`lessons/process.md`](lessons/process.md)
（尤其是「探针被中断」那节 —— 源码可能被留在变异状态）。

> 具体数字 → [`current/status.md`](current/status.md)（[重要] 唯一归属地）。
> **本文件不抄数字** —— 抄了就会漂。

> 注意： **不要加 `-q`**。`pytest.ini` 已有 `addopts = -q`，
> 命令行再加一个会变成 `-qq`，**汇总行 `N passed in Xs` 会被吞掉**。

### 第 2 步 · 读目标与进度（2 分钟）

| 读 | 为了知道 |
|---|---|
| [`target/vision.md`](target/vision.md) | **这个产品最终是什么** —— 画布是模具，产出的是 agent |
| [`target/roadmap.md`](target/roadmap.md) | **五个里程碑的达成情况**；下一步该做哪个 |
| [`target/decisions.md`](target/decisions.md) | **待决清单** —— 不要自行假设，会影响产出物形状 |

### 第 3 步 · 找到要改的文件（1 分钟）

[`code/README.md`](code/README.md) —— 逐任务列出**文件 · 位置 · 验证 · 坑**。

**当前建议顺序**（进度、任务号与执行顺序的唯一归属地是
[`target/roadmap.md`](target/roadmap.md)）：下一步是 **M4**（导出八件套）；具体阻塞与顺序
以路线图为准，不要把历史任务序列当作当前待办。

---

## 一分钟了解这个项目

**可视化 AI Agent 编排平台。** 在画布上拖节点连线，每个节点用自己的话写
「这一步做什么」，AI 读懂描述后填好参数，然后运行并逐节点查看 I/O。

**核心主张**（决定几乎所有设计）：

> 画布是编辑器，LangGraph 是执行引擎，两者解耦。
> 节点写共享状态 `vars[node_id]`，边只表达依赖。

**最终形态**：画布是**模具**，一个底层实现**一份**，所有 agent 从同一个模子压出来。
导出的 agent 运行时不依赖画布。

| 想深入了解 | 去哪 |
|---|---|
| 三层架构（构建时 / 运行时） | [`target/vision.md`](target/vision.md) |
| 交付物长什么样 | [`target/delivery.md`](target/delivery.md) |
| 现在的架构与机制 | [`current/architecture.md`](current/architecture.md) |

---

## 环境速查

| 项 | 值 |
|---|---|
| Python | **必须用 venv_d 解释器**（绝对路径见 [`env/redlines.md`](env/redlines.md)）；**不用系统 Python** |
| 终端 | **pwsh 7.6.6**（不要 powershell 5.1） |
| 后端端口 | **8001**（为什么不是 8000 → [`env/ports.md`](env/ports.md)） |
| 前端 | 源码在 `frontend/`，**实际跑在 `%TEMP%\agent_web`** |
| 凭据 | `.env`（已 gitignore），DeepSeek 已实测可用 |
| git | `main` 已跟踪到 GitHub；改前检查 `git status`，保留已有未提交改动 |

完整的启动 / 测试 / 冒烟命令 → [`env/run.md`](env/run.md)
环境红线 → [`env/redlines.md`](env/redlines.md)

---

## 五个最容易踩的坑（先看，能省时间）

| # | 坑 | 一句话 |
|---|---|---|
| 1 | **端口 8000 是僵尸** | 后端用 8001（前端 `vite.config.ts` 的代理默认也是 8001） → [`env/ports.md`](env/ports.md) |
| 2 | **前端不在 `frontend/` 里跑** | 改完必须 `pwsh scripts\sync_src.ps1` → [`env/frontend-temp.md`](env/frontend-temp.md) |
| 3 | **长任务会被下一条终端命令杀掉** | 发起后**不要再碰那个终端** → [`env/tooling.md`](env/tooling.md) |
| 4 | **中断反向验证后必须先恢复** | 先恢复再跑测试（步骤见 [`lessons/process.md`](lessons/process.md) §3） |
| 5 | **写守卫必须反向验证** | 先问「**这个测试有没有可能失败**」→ [`lessons/process.md`](lessons/process.md) |

---

## 三条工作纪律（这个项目的既有规则）

### 1. 用户说「该做什么」是**提问**，不是授权

回答完**停下来等**。想动手先问一句「要我现在做吗？」

区分：用户说「继续」「做吧」「写吧」或点名一个任务是**授权**；
「接下来该做什么」「怎么样」「能不能」是**提问**。

### 2. 改源码前保护工作树

先检查 `git status`，确认并保留已有改动；不要用 `reset` / `checkout` 覆盖工作区。
提交是独立操作，未获授权不要提交。

### 3. 改完立刻验证，并登记

- 跑 `pytest tests/ --tb=short`
- 新加守卫 → 加反向验证用例，确认它会 FAIL
- 改过 `docs/` → 跑 `backend/_check_docs.py`（索引完整性 + 死链 + 死锚点 + status 数字）
- 更新 [`current/status.md`](current/status.md) 与 [`current/files.md`](current/files.md)
- 写进 [`log/`](log/)（含「回滚索引」）

**未验证的代码不留，需求不丢** —— 需求写进 [`code/`](code/)，不要留半成品在源码里。

---

完整目录索引与维护规则 → [`README.md`](README.md)
