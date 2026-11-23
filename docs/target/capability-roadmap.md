---
title: 能力路线图（P0–P6）
read_this: 决定"接下来补哪个能力、为什么这个顺序"时
owner_of: 能力包 / 会话 / 连接器路线的唯一归属地（里程碑 M1–M5 在 roadmap.md）
---

# 能力路线图（P0–P6）

> 这份路线图回答的是：**「能产出业务需要的大部分 agent」还缺什么、按什么顺序补。**
> 里程碑 M1–M5（画布 / 复杂图 / 批量 / 交付 / 自主）在
> [`roadmap.md`](roadmap.md)，两者不冲突：M 系列是**已有底座的成熟度**，
> P 系列是**能力面的宽度**。

---

## 1. 设计主张

**每个能力一个包（Capability Pack）**：一个目录 + 一份 manifest，声明它的
tools / nodes / permissions / secrets / Python 依赖 / 导出附加项。平台和导出包从
**同一份 manifest** 加载 —— 这是「同一个底座，不同能力档位」的落点。

**所有外部写操作走 `Policy → Approval → Audit`**：权限策略回答 allow / deny /
needs-approval；审批工作台（P1 之后）收集人的同意；审计是 JSONL 追加文件。
当前 `requires_approval` 在没有审批工作台时**失败关闭**，不会假装批准过。

---

## 2. 阶段与状态（2026-11-03）

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P0 能力包底座** | Pack manifest / registry / 依赖与封条 / 权限 / 审计 / 工作区 / 导出 parity / `doctor` | [x] **已完成**（2026-11-03）→ [`../log/phase-9-capability-packs-2026-11-03.md`](../log/phase-9-capability-packs-2026-11-03.md) |
| **P1-a files 能力包** | 工作区受限根；`file_*` / `csv_*` / `excel_*` / `pdf_read` / `docx_*` 共 10 个工具；写前权限+审计 | [x] **已完成**（2026-11-03） |
| **P1-b 最小会话层** | `Session`/`Message`/SQLite store/`SessionService`；历史注入；导出包 `serve --http` + 内置聊天页 + 上传 + SSE | [x] **导出侧已完成**（2026-11-03）；平台内会话 API/UI 未做 |
| **P1-c 通用连接** | HTTP/OpenAPI 工具生成 · SQL 读写（走审批）· IMAP 收信 | [ ] 未开始 |
| **P1-d 平台会话 UI** | `/api/sessions` + 前端会话面板（会话列表、气泡、上传） | [ ] 未开始 |
| **P2 语义检索 + 多模态** | embedding retriever + 分块/混合检索/引用；扫描 PDF OCR；ASR/TTS；LLM 图片输入 | [ ] 未开始（`retriever` 仍是 BM25） |
| **P3 浏览器 + MCP** | Playwright（打开/点击/填表/抽取/截图/人工接管）；MCP 客户端 + 工具发现 | [ ] 未开始；浏览器形态（本地 Playwright vs 远程 CDP）**待定** |
| **P4 批量 + 评测/反馈** | Map/Batch 节点、并发上限、逐项失败；反馈表；黄金用例；批量评测 | [ ] 未开始 |
| **P5 工作区 + 审计增强（可选）** | `workspace_id` 隔离、按工作区 secrets/额度、审计导出、保留/脱敏 | [ ] 未开始（当前单工作区） |
| **P6 工业协议（可选）** | MQTT、Modbus 先做；OPC-UA 后置；私网作用域；写联锁；模拟器 | [ ] 未开始 |

---

## 3. 已完成部分的边界（不要误读）

| 已经成立 | 边界 |
|---|---|
| 能力包声明 → 导出物只加载声明的包 | 一个进程仍是一个能力档位；跨 agent 隔离靠导出包各自运行 |
| files 包可读写工作区 | **一个工作区**（`AGENT_WORKSPACE_DIR`），没有 per-session 隔离（P5） |
| 导出包 `serve --http` 可聊天 | stdlib 服务：无 TLS/无限流/单进程；对外部署要放在反代后面 |
| 会话历史显式注入 | 不做跨会话长期记忆（记忆是另一个能力，见 M5） |
| 文件写操作有权限 + 审计 | 审批工作台未做，`requires_approval` 目前失败关闭 |
| 依赖进封条锁 | `_vendor/*.whl` 还没被复制进导出包（真实交付前要补） |

---

## 4. 建议的下一步（按 ROI）

1. **平台内会话 UI（P1-d）** —— 客户演示从「单次运行」变成「对话」，且复用已完成的
   `SessionService`；工作量集中在 API 一层 + 前端一个面板。
2. **重导出安全 + 打包完整性** —— 临时目录原子切换（见 log §7.1）、把 wheel 复制进
   导出包（§7.2）。这两项不补，交付流程会踩。
3. **审批工作台** —— 把 `requires_approval` 接进 human-in-the-loop，再做 SQL 写/IMAP
   这类外部写。
4. **P1-c 通用连接**：先 `http-openapi`（生成工具，验证 manifest 通用性），再 SQL 写/IMAP。
5. P2/P3 按用户选定的第一个业务 POC 场景决定先后（OCR 用本地还是云端、浏览器本地还是 CDP
   仍是待决）。
