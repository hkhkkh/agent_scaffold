# Coze 对标补充计划（精简版）

> 日期：2026-11-03 · 状态：**待确认执行** · 工期：约 **5 个工作日**（1 人）
> 原则：只补"客户演示闭环"缺的三块；**不做平台生态**。每阶段独立可演示、可验收、可回滚。
> 配套：为什么做、与 Coze 的逐项对比见对话记录；本文只写"做什么、不做什么、怎么验收"。

---

## 1. 一句话

平台已经有「画布 + 运行 + 导出独立 agent」，客户演示时最常被问、而我们没有的三件事是：

1. **能不能问我们自己的资料？**（知识库）
2. **能不能发给别人直接用？**（发布/分享）
3. **用起来像不像一个产品？**（开场白/推荐问题）

本轮只做这三块。评测、插件市场、模板库、渠道矩阵、团队/计费**都不做**。

---

## 2. 现状基线（不要再造一遍）

已有能力（见 [`../current/status.md`](../current/status.md)、[`../current/architecture.md`](../current/architecture.md)）：

- 21 种节点（含 retriever / memory / loop / subgraph / supervisor / human_input / code / sql / pdf / fhir 等）
- 长期记忆（跨 run 语义召回）、定时运行、事件唤醒 hook
- 版本历史、运行步骤可观测、HITL、权限 + 审计、公开演示门禁
- 导出独立 agent 包：`serve --http` + 内置聊天页 + `/v1` API（会话/文件/SSE）
- 测试：后端 1338、前端 78；docs / i18n 检查器

真实缺口：

- `retriever` 是 **BM25 关键词检索**，没有向量、没有知识库管理（[`../../backend/app/nodes/retriever.py`](../../backend/app/nodes/retriever.py) 开头已写明）
- 只有"导出命令"，没有一键发布 / 分享 / 嵌入
- 聊天页是干净输入框，没有开场白 / 推荐问题

---

## 3. 范围

**做（本轮）**

- A. 知识库最小闭环：上传文本/Markdown/PDF → 自动切分 → 向量索引（无 key 时 BM25 降级）→ 检索节点 → 答案带引用
- B. 发布最小闭环：当前工作流 → 导出包 → 启动本地 HTTP 服务 → 分享链接 / iframe / API Key / 停止
- C. 对话壳最小闭环：开场白 + 最多 3 个推荐问题 + 清除上下文

**不做（本轮明确排除）**

- ❌ 表格/图片知识库、Notion/飞书等外部数据源、知识库权限模型
- ❌ 层级分段/自定义分段 UI（只做自动切分，参数走配置）
- ❌ 发布渠道矩阵（飞书/微信/抖音/小程序/商店）、审核、数据看板、模板市场
- ❌ 评测集/评估器/Prompt Playground（下一轮）
- ❌ 插件市场 / OpenAPI 导入 / 新节点（意图、文本处理、变量、批处理、卡片）
- ❌ 语音/TTS、卡片消息、快捷指令、多智能体编排改动、团队协作、计费

---

## 4. Phase A · 知识库（2.5 天）

**用户故事**：把公司手册传进平台，画一个 `开始 → 检索器 → 大模型 → 结束`，运行后回答带"来源：手册第 N 段"。

### A1. 最小功能

1. 知识库列表 + 新建（名称/描述），**平台级资源**（多个工作流复用）
2. 上传：`.txt` / `.md` / `.pdf`（单文件 ≤20MB），存 `data/knowledge/<kb>/`
3. 自动解析 + 切分（固定参数起步：按段落/句号切，600 字符/段，60 字符重叠；不提供 UI 调参）
4. 索引：OpenAI 兼容 `/embeddings`（GLM 等）；**没有配置 key 时自动用 BM25**，界面标注"语义检索未启用"
5. 检索：`retriever` 节点选知识库，`top_k=5`，返回匹配片段 + `source{文档名, 段号}`
6. 引用展示：运行结果卡 / 使用视图答案下方显示来源，可展开看原文片段
7. 删除文档（删除即重传，不做单独 reindex）

### A2. 数据结构（`data/knowledge.db`，独立 SQLite，不动主库）

```sql
knowledge_bases(id PK, name, description, chunk_config JSON, embedding_model, created_at, updated_at)
documents(id PK, kb_id, name, mime, size, sha256, status, error, created_at)
   -- status: pending | parsing | indexed | failed
chunks(id PK, kb_id, document_id, seq, text, embedding BLOB, dims, metadata JSON)
```

### A3. API（`backend/app/api/knowledge.py`，在 `app/main.py` 注册）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET / POST | `/api/knowledge` | 列表 / 新建 |
| GET / DELETE | `/api/knowledge/{kb}` | 详情（含文档状态）/ 删除 |
| POST | `/api/knowledge/{kb}/documents` | 上传（multipart） |
| DELETE | `/api/knowledge/{kb}/documents/{doc}` | 删除文档 + chunks |
| POST | `/api/knowledge/{kb}/search` | `{query, top_k}` → 片段+分数（召回测试） |

### A4. 改动文件

**后端**

- 新建：`backend/app/knowledge/{__init__,store,chunking,ingest,embedder}.py`
- 新建：`backend/app/api/knowledge.py`（在 `backend/app/main.py` 注册路由）
- 修改：`backend/app/nodes/retriever.py`（参数 `knowledge_base_id` / `mode: keyword|semantic|hybrid` / `top_k`，兼容旧配置）
- 新建：`backend/tests/test_knowledge.py`（切分、解析、fake embedder 检索排序、删除级联、BM25 降级、API）

**前端**

- 新建：`frontend/src/knowledge/KnowledgeDialog.tsx`（列表 + 上传 + 状态 + 搜索测试）
- 修改：`frontend/src/App.tsx`（顶栏「知识库」入口 + dialog kind）
- 修改：`frontend/src/api/client.ts`（knowledge API）；`frontend/src/inspector/Inspector.tsx`（retriever 的知识库下拉）
- 新建：`frontend/src/run/citations.ts`（从 retriever 输出提取来源）
- 修改：`frontend/src/run/RunPanel.tsx`、`frontend/src/use/UseView.tsx`（引用展示）
- 修改：`frontend/src/i18n/translations.ts`（中英 key）、`frontend/src/styles/refresh.css`（知识库弹窗样式）

### A5. 验收

1. 上传 txt/md/pdf 各 1 个，全部 `indexed`；删除后再查为 0
2. 配置 embedding key 时，10 条"换个说法"的查询里目标片段进 top-3 ≥8 条
3. 不配 key 时 BM25 可用，界面有降级提示
4. 真实运行一次：最终答案卡出现来源，展开可见原文
5. 100 页 PDF 入库 <60s；5k chunks 检索 <300ms；`pytest`/`tsc`/`vitest`/build 全绿

### A6. 风险与降级

- **embedding 服务不可用/费用** → 降级 BM25；配置走 `.env`（`EMBEDDING_BASE_URL/KEY/MODEL`），本轮不加设置页
- **文件格式异常** → 状态 `failed` + 错误信息，不阻塞其他文档
- **运行边界**：只做平台内运行；导出包要不要带知识库 → 下一轮（避免影响 runtime 分层）

---

## 5. Phase B · 发布与分享（1.5 天）

**用户故事**：点「发布」→ 启动服务 → 复制链接发给同事，对方打开就能聊。

### B1. 最小功能

1. 发布 = 当前已保存工作流 → 复用现有导出包 → 子进程启动 `python main.py serve --http --port <自动> --token <随机>`
2. 发布管理：列表、启动、停止、删除；状态 `stopped/starting/running/failed` + 最后错误
3. 展示三样东西：**分享链接**、**iframe 片段**、**curl/API 片段**（token 默认隐藏，可复制）
4. `frontend/public/embed.js`：约 20 行，网页一行 `<script>` 出现右下角聊天气泡（iframe 指向发布 URL）
5. 安全：仅本地管理员可用；默认 `127.0.0.1`；公开演示禁用

### B2. 改动文件

- 新建：`backend/app/publish/{__init__,manager}.py`（`data/publish.db` 一张表 + 子进程管理）
- 新建：`backend/app/api/publish.py`（`GET/POST /api/publish`、`POST /api/publish/{id}/start|stop`、`DELETE`）
- 新建：`frontend/src/publish/PublishDialog.tsx`；修改 `frontend/src/App.tsx`（顶栏「发布」）
- 新建：`frontend/public/embed.js`
- 复用：`backend/app/runtime/export.py`、`backend/app/runtime/export_templates.py`（不改导出结构）

### B3. 验收

1. 发布 Excel 示例 → running → 分享链接在无痕窗口打开，上传+多轮对话正常
2. token 错误请求返回 401；停止后链接不可用
3. iframe 测试页能出现聊天；embed.js 悬浮窗可开合
4. 删除发布记录后端口释放、目录清理

---

## 6. Phase C · 对话壳（1 天）

**最小功能**（只这 3 个，不做快捷指令/语音/卡片）：

1. 每个工作流一份 `chat_config`：`opening`（开场白）、`suggested_questions`（最多 3 条）、`clear_context`（按钮固定开启）
2. 配置入口：PublishDialog 内「对话设置」一个小表单；存 `data/chat_configs.json`（按 workflow id）
3. 发布时写入导出包；内置聊天页读取：空会话显示开场白 + 推荐问题 chip（点击即发送）+ 输入框上方「新会话」按钮

**改动**

- 修改：`backend/app/api/workflows.py`（GET/PUT `/api/workflows/{id}/chat-settings`，读写 JSON 文件）
- 修改：`backend/app/runtime/export_templates.py`（发布时注入 `chat_config.json`；聊天页渲染开场白/推荐问题/新会话）
- 修改：`frontend/src/publish/PublishDialog.tsx`（对话设置小节）
- 修改：`frontend/src/i18n/translations.ts`

**验收**：配置开场白 + 3 个推荐问题 → 发布 → 页面正确显示；点推荐问题能发送；「新会话」清空对话。

---

## 7. 排期（5 个工作日）

| 天 | 工作 | 里程碑 |
|---|---|---|
| D1 | A 数据层 + 切分 + 解析 + API | — |
| D2 | A 检索 + retriever 节点 + 前端知识库/引用 | **M1：上传手册→带引用问答** |
| D2.5 | A 测试/文档/验收 | — |
| D3 | B 发布管理器 + API | — |
| D4 | B 前端发布面板 + embed.js | **M2：复制链接给别人用** |
| D5 | C 对话壳 + 导出注入 + 测试/文档/同步 | **M3：开场白/推荐问题/新会话** |

**如果只有 2 天**：只做 A。

---

## 8. 每阶段质量门禁（不绿不算完成）

1. 后端：目标模块单测 + 全量 `pytest`（当前 1338+）
2. 前端：`tsc --noEmit`、`vitest`、`vite build`
3. 真实浏览器验收：CDP 操作 + 截图（发布/知识库/聊天页）
4. `backend/_check_docs.py` PASS、i18n checker PASS
5. 更新：本文档状态、`docs/log/` 一篇阶段记录、`docs/current/status.md` 一行、`HANDOFF.md`
6. 同步：`sync_backup_to_live.py` → 两树 diff 0 → live docs checker PASS；后端改模块需重启 8001

---

## 9. 回滚与风险

- 无 git，按现有习惯：每阶段写 log + 回滚索引；新增文件独立（知识库/发布/评测各自 SQLite 与目录），删除目录即可回退
- 风险表：

| 风险 | 应对 |
|---|---|
| embedding 无 key / 限流 | 自动 BM25 降级，演示不受阻 |
| 上传解析失败 | 文档级 failed + 错误，不影响其他文档 |
| 发布端口冲突 | 自动换端口；停止/删除时释放 |
| 导出包与平台知识库不一致 | 本轮知识库只在平台内运行；导出不接知识库 |
| 范围膨胀 | 本文 §3「不做」清单为准，新增需求先改文档 |

---

## 10. 需要确认的两件事

1. **embedding 用哪家**：GLM embedding / OpenAI / 自建 bge（OpenAI 兼容即可）；没有 key 就先用 BM25 演示。
2. **知识库归属**：建议平台级资源（多工作流复用），与 Coze"资源"概念一致。

确认后从 Phase A 开始，按里程碑逐个交付。
