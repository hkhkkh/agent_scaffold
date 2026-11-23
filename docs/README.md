# docs/ — 总索引

> **这是唯一的入口。** 读完本页你就知道该去读哪个文件，不用翻。
>
> **本目录是给 AI 读的**，所以设计目标是：**每个文件小且只回答一类问题** ——
> 这样你可以只加载需要的那份，而不是被迫吞 2400 行。
>
> 维护规则与会话恢复流程复核：**2026-10-08**。各文档内容的时效见下表。

---

## 怎么用这份索引

1. **新会话 / 刚接手** → [`00-start-here.md`](00-start-here.md)（5 分钟）
2. **带着具体问题** → 查下面的 [按问题查](#按问题查)
3. **要写代码** → [`code/README.md`](code/README.md)

---

## 目录结构（按职责分）

| 位置 | 回答 | 什么时候读 |
|---|---|---|
| **[`00-start-here.md`](00-start-here.md)** | 我从哪开始 | **每次新会话第一份** |
| **[`user-guide.md`](user-guide.md)** | 画布怎么用、Agent 怎么导出运行 | **使用与编排 Agent 时** |
| **[`env/`](env/)** | 怎么把它跑起来 | 启动 / 测试 / 排查环境 |
| **[`current/`](current/)** | 现在是什么样 | 需要现状事实 |
| **[`target/`](target/)** | 要变成什么样、为什么、先做哪个 | 决定做什么之前 |
| **[`code/`](code/)** | 代码要改哪里 | **要动手时** |
| **[`reference/`](reference/)** | 某一层怎么实现的、为什么这么写 | 读代码 / 改代码 |
| **[`lessons/`](lessons/)** | 踩过的坑（按症状查） | **出错时第一站** |
| **[`log/`](log/)** | 每一刀做了什么（历史） | 想知道某处为什么长这样 |

---

## 按问题查

### 上手 / 运行

| 问题 | 去哪 |
|---|---|
| 我从哪开始？ | [`00-start-here.md`](00-start-here.md) |
| 怎么启动后端 / 前端？ | [`env/run.md`](env/run.md) |
| 测试怎么跑？期望几个通过？ | [`env/run.md`](env/run.md) + [`current/status.md`](current/status.md) |
| 为什么端口是 8001 不是 8000？ | [`env/ports.md`](env/ports.md) |
| 前端为什么跑在 `%TEMP%`？ | [`env/frontend-temp.md`](env/frontend-temp.md) |
| 环境红线（不能做什么）？ | [`env/redlines.md`](env/redlines.md) |
| **怎么公网部署 / 配人机验证？** | [`env/deploy.md`](env/deploy.md) |
| **注塑产线 IM-07 持续监控 agent 怎么跑？** | [`env/im07-line-agent.md`](env/im07-line-agent.md) |
| **导出的 agent 包怎么脱离平台持续跑？** | [`user-guide.md`](user-guide.md) §八 + [`log/phase-8-5-packaged-daemon-2026-10-23.md`](log/phase-8-5-packaged-daemon-2026-10-23.md) |
| 某个命令卡住了 / 输出被吞了？ | [`env/tooling.md`](env/tooling.md) |
| **`git push` 推不上去？** | [`env/tooling.md`](env/tooling.md) §10 —— 这台机器到 `github.com` 的 TLS 被阻断，**但 `gh api` 可用**，有手工推送的步骤和四个坑 |

### 现状

| 问题 | 去哪 |
|---|---|
| **测试 / 冒烟全绿吗？数字多少？** | [`current/status.md`](current/status.md) [重要] **数字的唯一归属** |
| 有哪些文件？ | [`current/files.md`](current/files.md) |
| 整体架构是什么？ | [`current/architecture.md`](current/architecture.md) |
| 中英切换怎么做的？ | [`current/i18n.md`](current/i18n.md) |
| **运行暂停之后，重启还能接着跑吗？** | [`log/phase-2-8-resume-after-restart.md`](log/phase-2-8-resume-after-restart.md) [重要] **可以**，但未保存工作流的运行不行 |
| AI 有哪些功能？ | [`current/ai-features.md`](current/ai-features.md) |
| 安全做到什么程度？ | [`current/security.md`](current/security.md) |
| 还有什么没做 / 已知缺陷？ | [`current/limitations.md`](current/limitations.md) |

### 目标

| 问题 | 去哪 |
|---|---|
| **这个产品最终是什么？** | [`target/vision.md`](target/vision.md) [重要] 产出物定义 + 三层架构 |
| **导出的 agent 里有什么？** | [`target/delivery.md`](target/delivery.md) [重要] 完成态目录树 |
| 怎么保证所有 agent 同一个底层？ | [`target/delivery.md`](target/delivery.md)（声明 + 随包 + **封条**） |
| 一次性导出怎么走？ | [`target/delivery.md`](target/delivery.md)（八步流程） |
| **现在能交付什么？** | [`target/roadmap.md`](target/roadmap.md) [重要] 五个里程碑与达成情况 |
| **能力面还缺什么、按什么顺序补？** | [`target/capability-roadmap.md`](target/capability-roadmap.md) [重要] P0–P6 能力包路线（2026-11-03：P0 + files + 导出侧会话已完成） |
| **怎么补 Coze 那类产品化能力？（精简版）** | [`target/coze-parity-plan-2026-11-03.md`](target/coze-parity-plan-2026-11-03.md) [重要] 只做知识库 / 发布 / 对话壳三刀，5 个工作日，明确不做生态 |
| **下一步做什么？** | [`target/roadmap.md`](target/roadmap.md) §顺序 + [`code/README.md`](code/README.md) |
| 为什么不能跳过某个 Phase？ | [`target/roadmap.md`](target/roadmap.md) §依赖链 |
| 差距清单 | [`target/gaps.md`](target/gaps.md)（**2026-09-11 快照**） |
| 商业化 / 产品体验差距复盘 | [`product-gap-analysis.md`](product-gap-analysis.md)（**2026-10-09 快照**） |
| **有什么还没定（待决）？** | [`target/decisions.md`](target/decisions.md) [重要] |
| **这个要不要自己写？（可复用面）** | [`target/reuse.md`](target/reuse.md) [重要] **市面已有什么、能拿什么** |
| 某条设计决策为什么这样 | [`reference/design-decisions.md`](reference/design-decisions.md) |

### 动手

| 问题 | 去哪 |
|---|---|
| **某件事改哪些文件？** | [`code/README.md`](code/README.md)（逐任务索引） |
| 改代码前要注意什么？ | [`code/README.md`](code/README.md) §纪律 |
| 后端某一层怎么实现的？ | [`reference/backend-layers.md`](reference/backend-layers.md) |
| 前端每个文件干什么？ | [`reference/frontend-files.md`](reference/frontend-files.md) |
| 怎么加一种新节点类型？ | [`reference/backend-layers.md`](reference/backend-layers.md) §节点系统 |

### 出错时

| 问题 | 去哪 |
|---|---|
| **遇到报错，先查哪里？** | [`lessons/README.md`](lessons/README.md)（按症状查） |
| 某个坑是不是踩过？ | [`lessons/`](lessons/) 按领域分 |
| 方法论教训（怎么避免重犯） | [`lessons/process.md`](lessons/process.md) |
| 反向验证怎么做？ | [`lessons/process.md`](lessons/process.md) + [`log/README.md`](log/README.md) |
| **探针被中断了怎么办？** | [`lessons/process.md`](lessons/process.md) [重要] §3 |

---

## 按角色读（推荐顺序）

| 你是 | 读这个顺序 |
|---|---|
| **新会话，要接着干** | [`00-start-here.md`](00-start-here.md) → [`code/README.md`](code/README.md)。[重要] **压缩恢复**的完整重读基线见 §9；命令按需查 [`env/run.md`](env/run.md)，数字按需查 [`current/status.md`](current/status.md) |
| **要写代码** | [`code/README.md`](code/README.md)（找到任务）→ 该文件里的「改哪些文件 / 怎么验证 / 坑」→ 需要时查 [`reference/`](reference/) |
| **要动架构 / 加能力** | [`target/vision.md`](target/vision.md) → [`target/delivery.md`](target/delivery.md) → [`reference/design-decisions.md`](reference/design-decisions.md) → [`log/`](log/) 里的 Phase 1 四刀 |
| **要判断进度 / 汇报** | [`target/roadmap.md`](target/roadmap.md)（里程碑表）→ [`current/status.md`](current/status.md) |
| **要排查故障** | [`lessons/README.md`](lessons/README.md) → [`env/`](env/)（环境类问题） |
| **要改安全相关** | [`current/security.md`](current/security.md) → [`target/vision.md`](target/vision.md) §三条约束 → [`lessons/security.md`](lessons/security.md) |

---

## 时效性（决定哪些数字可信）

| 位置 | 时效 | 说明 |
|---|---|---|
| `00-start-here.md` · `env/` · `current/` · `code/` · `lessons/` | 部分更新 | **2026-10-10**：docs-truth 审计逐份勘误了这五处（Git 保护 / 上下文恢复纪律为 2026-10-08 补入，其余内容 2026-09-17 复核后各自更新） |
| `target/vision.md` · `delivery.md` · `decisions.md` | 部分更新 | **2026-10-08 ~ 10-10**：三份此后各有更新（见 `log/docs-truth-audit-2026-10-10.md`），不再是 2026-09-17 的整份复查 |
| `target/roadmap.md` | 部分更新 | **2026-10-10**：修正 M4 源码 wheel 前置（46/46→49/49）、M5 摘要（3/5→4/5）与目录计数 |
| `target/reuse.md` §1.36 | 快照 | **2026-09-18** 打包与封条搜索；开工前复核上游状态 |
| `target/gaps.md` | 注意： **快照** | **2026-09-11**。已关闭项标了 [x]。**不要当现状读** |
| `reference/` | [x] 当前 | 数字已逐条重新实测 |
| `log/` | 历史 | 每份都标了日期与「本刀当时」的数字。**已完成的任务文件也移到这里**（见下） |

> **`code/` 与 `log/` 的分工**：`code/` **只放还没做的** —— 做完的文件移进 `log/`，
> 链接改成 `../log/...`。原因是 `code/` 会被当成待办清单读，
> 把已完成的任务留在那里等于让人去做已经做过的事。
> 2026-09-17 移了两份：任务 12 → [`log/phase-1-5-security-i18n.md`](log/phase-1-5-security-i18n.md)、
> 任务 31 → [`log/phase-6-1-agent-tools.md`](log/phase-6-1-agent-tools.md)。

---

## 维护规则（改文档前读这一节）

### 1. 事实只写一处，其他地方链接过去

这条是**最重要**的。文档曾经各自复述同一批事实（测试数、文件数、端口），
结果是**改一处漏两处**。现在的唯一归属：

| 事实 | **唯一归属** |
|---|---|
| 测试数 / 冒烟数 / 验证状态 | [`current/status.md`](current/status.md) |
| 文件清单 / 目录结构 | [`current/files.md`](current/files.md) |
| 端口 / 启动 / 测试命令 | [`env/run.md`](env/run.md) |
| 环境红线 | [`env/redlines.md`](env/redlines.md) |
| 坑与教训 | [`lessons/`](lessons/)（按领域） |
| 能力接口的形状 | [`reference/backend-layers.md`](reference/backend-layers.md) §能力层 + [`log/`](log/) |
| 进度 / 里程碑 | [`target/roadmap.md`](target/roadmap.md) |
| 待决 | [`target/decisions.md`](target/decisions.md) |
| **改哪个文件** | [`code/`](code/) |
| 设计决策的理由 | [`reference/design-decisions.md`](reference/design-decisions.md) |

**引用时写「见 …」，不要抄数字。**

### 2. 按职责放，不按"重要性"放

一份文档只回答**一类**问题。混在一起就是"乱"。

**判断方法**：如果它需要写两个不同的「什么时候读」，它就该拆开。

### 3. 时间快照要显式标注

状态会变，但「当时怎么判断的」有价值。所以：

- 标题写清年月，如「实测证据（2026-09-14）」
- 整份是快照时，文件开头写「本文件是 YYYY-MM-DD 的快照，当前值看 …」

**没标注的默认是现状** —— 这是最容易误导人的地方。

### 4. 过期就删，不要留"已被取代"的横幅

从 2026-09-14 那次清理学到的：先加横幅保留，结果是文档一半有用一半是死的，
读者要**处处提防**，成本高于收益。

**正确做法**：过期段落直接删；若那段内容**独一无二**（别处没有），
把它更新到正确再留下。

### 5. 每份 log 都留「下一步」和「回滚索引」

`log/` 里每份结尾都有。**作用是让会话中断后能接上**，不是礼节。

### 6. 交叉引用用锚点，别写行号

行号会漂。用短锚点（如 `<a id="x1"></a>`），节标题怎么改都不影响链接。

**校验**（悬空锚点 / 悬空链接 / 索引完整性，一次跑完；数字那节见 §11）：

```powershell
Set-Location h:\agent_scaffold\backend
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' -X utf8 _check_docs.py
```

> 注意： **[`code/`](code/) 里的 `anchor` 是刻意的例外** —— 那些是**源码文本**
> （给 `grep_search` 定位用），不是 markdown 锚点。理由：行号会漂，源码文本不会。

> 注意： **标题锚也算数。** [`user-guide.md`](user-guide.md) 与本文件用的是
> `](#一快速上手与环境说明)` 这种 GitHub 从标题生成的锚点，不是 `<a id>`。
> 检查两种都认 —— 早先只认后者时，它把 8 条**活链接**报成了死的（见 §11）。

### 7. 改源码前保护工作树

先检查 `git status` 并保留已有改动；不要用 `reset` / `checkout` 覆盖工作区。
提交是独立操作，未获授权不要提交。

### 8. 每次变更和进度都必须留记录

每次获准的代码或文档变更，都记入对应的 `docs/log/` 任务记录：日期、改了什么、涉及文件、验证结果、未完成项和下一步。
任务/里程碑状态变化时同步更新 `target/roadmap.md`；测试与验证数字只更新 `current/status.md`。
遇到上下文压缩或会话中断，在继续前先把已完成、未完成、未验证及下一步写进对应 log。

### 9. 上下文压缩后必须完整重读项目基线

每次上下文压缩恢复、接手新会话或长任务中断后，**继续任何工作前完整通读**：
[`target/roadmap.md`](target/roadmap.md)（计划）、[`target/vision.md`](target/vision.md) 与
[`target/decisions.md`](target/decisions.md)（设计与待决）、[`current/status.md`](current/status.md)（现状与进度）、
[`code/README.md`](code/README.md)（任务到文件的映射）。再读当前任务对应的 code spec 和最新 log，
并检查 `git status`。不得以摘要、记忆或先前回答替代这些文档；发现冲突先以文件现状为准并修正计划，不可直接开工。

### 10. [最重要] 开工前三步 —— **必须先执行，不是参考**

细节与可执行命令在 [`code/README.md`](code/README.md)「开工前的三步」。摘要：

1. **搜现成的** —— 跑那条 `Select-String` 列出所有 `set_*` / `get_*` / `has_*` 注入点。
	**没搜完不许动手。** 新 seam 一律照抄 `backend/app/graph/subgraphs.py` 的形状
	（模块级可调用 + `set_*` + `has_*`），**禁止自写 ABC / Provider 类 / Null 对象**。
2. **确认层与边界** —— 层 2 里出现 `app.db` / `fastapi` / `sqlmodel` 一律算错，**惰性导入也算**。
3. **保护工作树** —— 先看 `git status`。

**为什么单列一条**：2026-10-08 一天内违反两次 —— 先造了 `set_run_store_factory`（字符串名→工厂注册表），
再造了 `WorkflowProvider`（ABC + Null 类，~60 行），而 `subgraphs.py` 对同一问题只用 ~15 行。
**第二次的错误代码就紧挨着正确答案。** 清单已存于项目记忆 `/memories/repo/no-reinventing.md`。

> **另两条同批教训**：**不要反复跑测试**（只在改动直接影响处跑，或完成时跑一次）；
> **数字只记在 `current/status.md`**，别处写「见 status.md」，不抄数字。

### 11. [重要] 目录索引是手写清单，必须有检查

每份子目录 `README.md` 都是一份**手写**清单，而手写清单**静默失败**。

**2026-10-16 实例**：`log/README.md` 漏了当天的 log —— 文件在、内容对、
`ls` 看得见，**只有从索引进来的人找不到它**。这与
`app/runtime/export.py` 里 `RUNTIME_PATHS` 是同一类失败（那处自己写着
"a hand-maintained list fails silently, so it needs a check rather than care"），
是同一件事在**文档层**的重演。清单不检查就会一直漏，而漏了不报错。

**规则**：一份文档可达 = 它所在目录的 `README.md` 链到它；该目录没有 `README.md` 时，
链自本文件。

**检查**：`_check_docs.py` 第 2 节，命令见 §6。
**加一份文档、加一行索引之后跑一次。** 反向验证用例（必须报 FAIL 的那几条）见
[`log/docs-check-2026-10-16.md`](log/docs-check-2026-10-16.md)。
