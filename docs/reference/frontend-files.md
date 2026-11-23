---
title: 前端文件职责
read_this: 要改前端 / 想知道某个文件干什么
---

# 前端文件职责

> 状态（已可运行、已端到端验证）→ [`../current/status.md`](../current/status.md)
> 本文件只是「**哪个文件干什么**」。
>
> 注意： **实际运行在 `%TEMP%\agent_web`**，不是 `frontend/`
> → [`../env/frontend-temp.md`](../env/frontend-temp.md)

---

## 65 个源文件

目录树（65 个源文件的完整结构）归 [`../current/files.md`](../current/files.md) §前端（65 个源文件）。
本文件只列**每个文件干什么**（下表）。

---

## 逐个说明

| 文件 | 内容 |
|---|---|
| **`App.tsx`** | 应用外壳：Header + Palette + Canvas + Inspector + RunPanel；**build / use 两个视图的切换**；四个对话框；快捷键；`scaffold:*` 事件桥（**SSE 生命周期已移出**，见 `run/runController.ts`）；右侧栏的 **AI 对话 tab** 与画布上方的 `CopilotStageBar` |
| `main.tsx` | 入口（React 19 `createRoot`） |
| **`types/workflow.ts`** | 后端 schema 的完整 TS 镜像，含 `portsCompatible()` / `createNode()` / `emptyGraph()`；也含副驾事件/判断卡/方案/报告类型。手工维护 —— 漂移风险与待做的 `check-schema-drift.ps1` 见 [`../current/limitations.md`](../current/limitations.md) §3 |
| **`api/client.ts`** | `ApiError` + FastAPI 错误体解析 + 超时控制 + **手写 SSE 解析器**；run / generate / copilot 共用 `streamSse`；公开模式统一加 `X-Scaffold-Client`，`403 gate_required` 时派发 `scaffold:gate-required` |
| **`access/accessStore.ts`** | 公开访问状态：是否需要门禁、是否已验证、公开演示能力（Code/custom_tools/settings/schedules/delete/export/metrics/admin）、匿名访客剩余额度（quota） |
| **`access/GateRoot.tsx`** | 启动先取 `/api/access/status`；未验证不挂 App；会话中 Cookie 过期时用覆盖层而不是卸载 App，避免丢未保存画布 |
| **`access/GateScreen.tsx`** | 人机验证页：Turnstile 优先，脚本失败/8 秒超时自动显示 PoW 备用验证；显示进度/错误/重试 |
| **`access/pow.ts`** (+`.test.ts`) | `js-sha256` 分片计算服务端签名的 `nonce:difficulty` 挑战，支持 AbortSignal；客户端不能自降难度 |
| **`access/turnstile.ts`** | 显式加载 `challenges.cloudflare.com` widget，返回失败而不是把异常抛进渲染树 |
| **`access/PublicDemoBanner.tsx`** | 顶栏「公开演示 · 数据共享」提示；有 quota 时显示今日剩余 AI 次数与 tokens（进入页面时的快照，刷新后更新） |
| `frontend/public/landing.html` + `landing.css` + `landing.js` | 公网 `/` 的产品介绍站：首屏产品窗口、能力、运行/副驾示例、模板、场景、安全边界、FAQ、CTA；`landing.js` 负责滚动入场、进度条、视差和提示词轮播；`/app` 才是编辑器（2026-10-21） |
| **`store/workflowStore.ts`** | 单一 Zustand store：graph / selection / undo / run 全在一起（因为它们真的耦合）。**快照式 undo**（50 步） |
| `store/workflowStore.test.ts` | 前端**首个**测试文件，钉住 `replaceGraph` 的两个 bug |
| **`canvas/Canvas.tsx`** | React Flow 挂载、**动态端口解析**（`portSignature` 防抖）、拖放（`screenToFlowPosition`）、运行覆盖层、小地图、空态、错误面板；注册 `loopback` 边类型；应用选中焦点（端点/邻居氛围灯、关联边高亮，焦点外节点与连线变暗）；副驾的**框选模式**（`selectionOnDrag`）与多选范围回写 |
| **`canvas/nodes/BaseNode.tsx`** | 节点卡片。**description 内联可编辑**（这是产品核心）。**故意不 import store**，只派发 `CustomEvent`；Loop 节点显示「第 n/m 轮」 |
| `canvas/NodePalette.tsx` | 分类 + 搜索面板，可拖可点 |
| **`canvas/loopEdges.ts`** (+`.test.ts`) | 识别回边（**目标是 Loop 且源在右侧/下方**，两个条件缺一不可；只看几何会把蛇形布局的普通体边误判）、隧道放在**两个端点卡片下方 18px**、给回边加 `loopback` 类型与轮次；从 run 事件历史取 Loop 的最新轮次 |
| **`canvas/LoopBackEdge.tsx`** | 回边的专用渲染：横平竖直 + 圆角的绕行路径（向右→下→贴近端点行→左→上→进左侧 handle）、箭头、标签；默认**灰色虚线**，点击边/选中 Loop/运行时才转橙色并显示标签 |
| **`canvas/selectionFocus.ts`** (+`.test.ts`) | 选中物的“照明规则”：点连线 → 两端节点 primary；点节点 → 入/出边 incident + 一跳邻居 secondary；自环不算邻居 |
| **`copilot/copilotStore.ts`** | 编辑副驾的状态机：两种模式、对话、阶段栈、业务判断卡/方案/报告、SSE 生命周期、批准后的第二段执行、失败回滚、把 AI 修改记录交给下一次保存 |
| **`copilot/CopilotSidebar.tsx`** | 常驻侧栏：只放对话和本轮产物（判断卡、待批准方案、修改报告、样例输入/期望产出）；不承载模式/范围/阶段 |
| **`copilot/CopilotStageBar.tsx`** | 画布上方的模式开关、范围 chip、框选开关、层叠阶段栈；当前阶段在前，完成/跳过的阶段压到后面 |
| **`versions/VersionHistoryDialog.tsx`** | 版本历史：列表、AI 修改记录、语义 diff、只读画布预览、恢复；对比对象可选当前画布或任一版本；公开演示的 Code 工作流只允许查看，恢复按钮禁用 |
| **`versions/versionDiff.ts`** (+`.test.ts`) | 两版定义的语义 diff：节点增删改、连线增删改；**忽略 position**，拖节点不算改定义 |
| **`inspector/SchemaForm.tsx`** | **泛型表单**：完全由后端 `ParamSpec` 驱动，**无任何按节点类型的硬编码**；多行 string / JSON 使用模板补全输入框 |
| **`inspector/TemplateTextarea.tsx`** | 输入 `{{` 时弹出候选的 textarea；IME 安全，键盘可选，字段锚定菜单 |
| **`inspector/templateCompletions.ts`** (+`.test.ts`) | 从图算候选：`inputs.*` / `params.*` / `vars.<node>.out[.<port>]` / 循环体内的 `vars.<loop>.iteration` / 上次运行观察到的键 |
| `inspector/Inspector.tsx` | 描述区 → AI 补齐（diff 审阅）→ 配置表单 → 单节点测试 → 端口引用。含 Edge 检查器；向表单提供补全候选 |
| `run/RunPanel.tsx` | 运行时间线 + 每步 I/O + 流式输出（编辑器调试面） |
| `run/HumanInputDialog.tsx` | HITL 暂停时的审批弹窗 |
| **`run/runController.ts`** | run 生命周期，**在 React 之外**：`beginRun` / `startStream` / `syncRun` / `watchRun` / `installRunBridge`。两个视图共用一个模块级流 |
| `run/finalOutput.ts` (+`.test.ts`) | 从 `outputs` 里取出最终成果；**后端 `runtime/serialize.final_output` 的镜像** |
| **`use/UseView.tsx`** | **使用视图**（产品面）：**固定一个自由文本框**（`await beginRun({ query: text })`），不随工作流改变；成果置顶，下方只列 `log` 事件（`node_started`/`node_finished` 被 `buildLines` 丢掉）。**不 import 画布**，为将来搬进导出的 agent 包留路。2026-11-03 起，输入经 `use/inputs.ts` 映射到 Start 声明的入口字段（优先 `message`，否则 `query`/首个字符串字段），不再固定发 `query`；内部工程日志（`Start resolved` / `Agent started` / `Step N: calling` / `[warning]`）不再显示为对话气泡。按 `Start.input_schema` 生成表单的是**编辑器的运行对话框**（`run/RunPanel.tsx`），不是这里 |
| ~~`ai/GenerateGraphDialog.tsx`~~ | **已退役（2026-11-03）**：顶栏「AI 生成」与 Ctrl+K 入口移除，图生成本身改由**右侧副驾**完成（空画布场景卡会直接把场景 prompt 发给它）。文件已删除 |
| `ai/GeneratePreview.tsx` | 生成过程中/之后的**只读画布**：同一套节点组件，自动 fitView，新增/修改节点闪光；不写 store（版本对比预览仍在使用） |
| `ai/RepairGraphDialog.tsx` | AI 修复（含**被删节点红色 callout**） |
| **`assist/auto.ts`** · `mode.ts` (+`.test`) | 自动填补引擎：描述停止 2 秒后填单节点参数；只写未设置/自己写过的键，改动可撤销，失败静默。**2026-11-03 起不从 UI 挂载**（「自动/按需」开关退役：AI 只在右侧副驾按需发起），引擎与测试保留供未来 opt-in |
| `db/DbPanel.tsx` | 结果库面板：列出 `results.db` 的表与行，演示写库增长 |
| `ui/Dialogs.tsx` · `ui/Icon.tsx` · `ui/JsonViewer.tsx` · `ui/SettingsDialog.tsx` · `ui/ThemeToggle.tsx` · `ui/WorkflowBrowser.tsx` | 通用组件；`ThemeToggle` 是明暗主题切换（持久化 + `?theme` 强制，2026-11-03） |
| **`i18n/index.ts`** | `useT()`（组件用）/ `translate()`（store / 事件处理器用） |
| **`i18n/translations.ts`** | `MessageKey` 从**英文表推导** → **漏翻译是编译错误** |
| `styles/*.css` | 12 个文件；`tokens.css` 是**双主题语义令牌**（浅色产品默认 + `[data-theme=dark]`，2026-11-03 重写），`refresh.css` 是最后加载的**视觉覆盖层**（外壳/节点卡/面板/使用视图；改外观先看它，改颜色先看 tokens）。`copilot.css` 是画布上方阶段栈、范围遮罩和对话侧栏，`versions.css` 是版本历史，`gate.css` 是人机验证页与公开演示徽章 |

---

## [重要] 几个关键设计（不要破坏）

### 1. 节点卡片不 import store

`BaseNode.tsx` 只派发 `CustomEvent`，保持**纯展示组件**
（渲染便宜、好测）。

`App.tsx` 上有事件桥把它们接到 store：

```typescript
window.addEventListener('scaffold:update-node', (e) => updateNode(e.detail.id, e.detail.patch))
window.addEventListener('scaffold:select-node', (e) => selectNode(e.detail.id))
```

### 2. 动态端口要问后端

Condition 节点配了几个分支就长几个手柄 —— **端口无法从"节点类型"推出**。

```
/api/workflows/ports ← 用 portSignature 防抖：只在 config 真的变了才重取
```

> 那个 `portSignature` 是个细节：**没有它，Inspector 里每敲一个键都会触发一次后端请求。**

### 3. SSE 是手写 fetch 流解析

原生 `EventSource` **不能设 header、不能读 body、不能发 POST、不能传 `after` 序号**。
所以 `api/client.ts` 里有一个共用的 `streamSse`：

- `streamRun` —— 运行事件，支持 `after` 序号重放；
- `streamGenerateGraph` —— POST 生成，收到 `graph` 快照就画草稿，关闭即 abort。

两处共用同一份切帧/解析逻辑，避免“一个流能用、另一个流在代理后面静默不渲染”。

### 4. 单一 store

graph / selection / undo / run **全在一个 Zustand store 里** ——
因为它们真的耦合。分成多个会产生同步问题。

### 5. 连线标签默认隐藏

`EdgeSpec.label` 由 AI 在 `connect` 时写入（用户的意图，例如“检索式”）。但画布上
不常显：42 条边同时显示文字会把图变成文字墙。CSS 只在边 **hover / selected** 时
显示 `react-flow__edge-text`；点击边后 Inspector 的「标签」字段始终可编辑。
自定义的 `LoopBackEdge` 同理，标签只在选中或该循环正在运行时渲染。

---

## 注意： 前端的坑

全部展开在 [`../lessons/frontend.md`](../lessons/frontend.md)
（§1 selector 返回新数组 → 无限循环、§2 `setGraph` 清历史、§3 React StrictMode 双跑 effect、
§4 `emptyGraph()` 字段名、§5 vitest/vite 冲突）。坑的家在 lessons，本文件不重复其症状与修法。

---

## 命令

前端同步 / 启动 / 类型检查 / 单测命令归 [`../env/run.md`](../env/run.md)（§前端相关）。

---

## 相关

- 前端为什么不在 `frontend/` 跑 → [`../env/frontend-temp.md`](../env/frontend-temp.md)
- 前端的坑 → [`../lessons/frontend.md`](../lessons/frontend.md)
- 三个面板长什么样 → [`../current/architecture.md`](../current/architecture.md)
- i18n → [`../current/i18n.md`](../current/i18n.md)
