---
title: AI 功能
read_this: 要改 AI 相关代码 / 要理解 repair-graph 的护栏时
---

# AI 能力全景

**全部实测可用**（DeepSeek 真实调用验证过）。

---

## 1. 能力清单

| 能力 | 入口 | 作用域 | 能看到校验结果 |
|---|---|---|---|
| 填参数 | 右侧面板「让 AI 填参数」 | 单节点 | [ ] |
| **自动填补** | 默认「自动」协助；写完节点描述约 2s 后触发 | 单节点 | [ ] |
| 生成工作流 + **试跑** | 顶栏「AI 生成」 | 整图（从零/追加）；默认试跑，公开演示关闭 | [x] |
| **修复工作流** | **画布问题面板「AI 修复」** | **整图（默认只修错误，可勾选警告）** | [x] |
| **编辑副驾** | 右侧「AI」对话 + 画布上方模式/范围 | 选区或整图；先理解业务再改 | [x] |
| 配置 → Python | 仅 API：`POST /api/ai/node-to-code`（公开模式 admin-only） | 单节点 | [ ] |
| Python → 配置 | 仅 API：`POST /api/ai/code-to-node`（公开模式 admin-only） | 单节点 | [ ] |
| 工作流起名 | 仅 API：`POST /api/ai/suggest-name` | 整图 | [ ] |

**「AI 修复」「AI 生成」「编辑副驾」都能看到校验结果**：修复的完成判据就是校验通过；
生成和副驾默认还会**真实试跑一次**，把运行时失败也变成可纠错的信息。公开演示默认
关闭自动试跑（`AI_SELF_TEST=false`），见 §2f。

公开模式还关闭了 Code 相关能力：生成器的节点清单不包含 Code，
「配置 ↔ Python」两个端点只允许 admin；这不会让旧图打不开，Code 节点仍按
`disabled` 展示，但 Run/Save 会被公开策略拒绝。

### 自动填补（默认开启）

`frontend/src/assist/mode.ts` 的默认值是 `auto`：用户改完某个节点的
`description` 并停止输入约 **2 秒**后，前端调用 `POST /api/ai/fill-node`，
把模型建议直接应用到该节点的配置。它不是“生成/修复”那种 diff 流程，而是
**单节点的普通编辑**：

- 只写原本未设置的键，或它自己写过、用户后来没改过的键；用户手填的值永不覆盖；
- 每次应用都进撤销历史，Ctrl+Z 可以退回；失败只留节点提示，不打断画布；
- **每次调用都算一次公开 AI 请求并计入 token 额度**（`/api/ai/` 的 POST），
  所以“自动”模式也会消耗访客的每日 3 次；
- 顶部的「自动 / 按需」开关可切到手动；「按需」不会主动发请求。

### [重要] 不静默改用户的画布

除**放手执行**和**自动填补**外，所有功能都先给 diff / 方案，等用户 Accept/Reject。
自动填补是单节点、未设置字段的例外；放手执行是用户预先授权的整图例外：改动实时
应用，但整回合只压一次撤销历史，失败时按 §2e 的规则回滚，并随保存写入版本记录。
两者都遵守同一条底线：**用户写的 label / description / prompt 不被无理由改写。**

**原因**：静默改画布 = 图不可预测，与可视化编辑器的目的相反。

---

## 2. [重要] `repair-graph` 与 `generate-graph` 的本质区别

两者复用**完全相同**的机制（同一个 `GraphDraft`、同一个 `draft_tools()`、
同一个 `_dispatch`），但**完成判据不同**，而这就是功能的全部：

| | `generate-graph` | `repair-graph` |
|---|---|---|
| 何时停 | `finish` **且**校验通过 **且**试跑通过（外部错误除外）；校验最多 3 轮、运行时最多 2 轮 | **校验通过** |
| 目标 | 新东西，只能靠人判断 | 机器可判定 |
| 每轮回馈 | 上次调用的结果；`finish` 后回馈剩余错误全文；试跑失败后回馈**失败节点 + 错误 + 日志尾** | **剩余错误全文** |

**「每轮把剩余错误回馈给模型」是收敛的关键。** 只回上次变更结果的话，
模型得自己记住开头那份错误列表 —— 对话一长它就记不住了。

**生成也走这条回路了（原本只有修复走）。** 曾经的判据是「模型说 `finish`
就算完成」，理由是「生成的目标是机器判不了的新东西」。这条理由只对了一半：
**图要干什么**机器判不了，但**图连得对不对**机器判得了 —— 而那恰恰是模型
最容易错、用户最难发现的地方。一个四十节点的图里，某条分支接空、某个循环
没有回边、End 接在了副作用节点上，三者**都合法、都能编译**，只有跑起来才
暴露，而不懂代码的用户根本看不出哪里不对。

所以现在的判据是「`finish` **且**校验通过」：模型说完成之后，校验器说一遍，
错误原样喂回去让它改，最多 3 轮。超过 3 轮还没过就把图连同问题一起交出去 ——
能一轮修好的问题一轮就修好了，修不好的再喂三轮也修不好，而每一轮都是一次
带着长对话的完整模型调用。纠错轮次**不占**构建预算（`budget + _MAX_CORRECTIONS`），
否则四十个节点建完就没余量纠错了 —— 而那正是最需要纠错的图。

**另加防打转（只属于 `repair-graph`）**：错误数连续 3 轮不下降就停
（`_NO_PROGRESS_PATIENCE`），避免卡住时把 token 全烧光。`generate-graph` 没有
这个计数器，它靠校验 3 轮、试跑 2 轮的硬上限收敛。

---

## 2b. 试跑：校验之后的第二道门

校验只回答「能不能编译」。它回答不了「这个 agent 跑起来会不会做错事」：
默认输入是空的、模板渲染成空串、Code 读出错误形状 —— 这些都能编译、都会在第一次
运行时失败。所以生成在**校验通过之后**还会真实执行一次草稿。

| 项 | 规则 |
|---|---|
| 输入 | **Start 节点自己声明的默认值**（用户第一次按 Run 用的就是它们） |
| 失败处理 | 把失败节点、错误、最后几条日志、失败节点的产出回馈给模型，最多 **2 轮** |
| 外部错误 | 网络 / 超时 / 鉴权 / 限流标记 `environment`，**不回馈、不改图** |
| Code 节点 | 仍是未信任状态；含 Code 的图过不了校验，因此**不会自动试跑** |
| 预算 | `AI_SELF_TEST_TIMEOUT_SECONDS`（默认 180s）；轮次独立于构建预算 |
| 副作用 | **真实调用模型与工具，可能写数据库** —— 这是“跑通”而不是“假装跑通”的代价 |

公开演示默认 `AI_SELF_TEST=false`，生成后不会自动跑第二遍，以节省访客额度；
副驾如果带了 `expected_output`，为了做产出比对仍会强制试跑（`run_self_test =
settings.ai_self_test or bool(expected_output)`）。所以“公开模式不试跑”只对
没有期望产出的生成/编辑成立。

**为什么外部错误不纠错**：模型最自然的“修复”是删掉那个调用外部服务的节点。
那会让图通过，也会毁掉用户真正要的步骤。

**为什么轮次只有 2**：路径和形状错误一两轮能改；第三轮通常说明设计错了，
再喂一轮也改不出来，只是多一次带长对话的模型调用。

实测（Start → Prompt → LLM → End）：校验 0 轮纠错，试跑 **1.3s 通过**，
LLM 节点真实产出答案；整次生成收到 14 次工具调用、11 张图快照。

---

## 2c. 生成过程可见：SSE 快照 + 只读画布

`POST /api/ai/generate-graph/stream` 在每次**变更型**工具调用后推一份**完整图快照**
（`graph`）和一份 `changed`（新增/修改/删除的节点与边）。对话框内的
`GeneratePreview` 用与主画布相同的节点组件实时渲染。

- 快照而不是增量：客户端不重放编辑，id 与布局只有一份事实来源；丢帧也不会画错。
- `changed` 比较**忽略 position**：`layout()` 每次添加节点都会重排，比较坐标会让整张图都算“变了”。
- 关闭对话框会 abort 流并取消服务端生成 —— 什么都没应用，只损失 token。
- 非流式端点保留，`done.result` 与它返回的 JSON 完全一致。

---

## 2d. 模板路径校验：让“静默空串”变成错误

生成器会写 `{{ vars.<node>.out.<handle> }}`。路径写错时渲染器把它变成空字符串：
图能编译、能运行，只是悄悄少了一段。这条错误此前对生成的自纠回路**完全不可见**。

`validate.py` 现在扫描所有节点 config（跳过 Code 的 Python 源码，但**不跳过 Prompt 的
jinja2 模板**），报告：未知节点、未知 handle、Loop 未知顶层字段、
以及 `vars.<loop>.out.iteration` 这个最常见的错误路径（正确写法是
`vars.<loop>.iteration`，轮次是 top-level）。

---

## 2e. 编辑副驾：先理解业务，再受控修改

生成解决「从零画一张图」，编辑解决「这张图不符合业务」。入口是右侧栏的 AI 对话；
画布上方是模式、范围和阶段栈。

### 两种模式

| | 放手执行 | 需要确认 |
|---|---|---|
| 确认 | 无 | 先给业务判断 + 方案，批准后执行 |
| 写入范围 | 全画布 | 选区 **+ 已批准方案点名的节点**；其余只读 |
| 目标不确定 | 按最合理理解继续，假设写进报告 | 先让用户看理解和方案 |
| 安全网 | 快照、校验、试跑、版本记录；**最终校验错误数比原图多时**自动回滚 | 同样有快照/校验/试跑/版本；**不回滚，只报告** |

确认模式一轮会发 **两次** `POST /api/ai/copilot/stream`：第一次做理解/研究/方案，
用户批准后第二次才执行。两次都算公开 AI 请求额度（见 §2f），所以访客一天 3 次
大约只够一轮“分析 + 批准执行”。

### 强制业务洞察

每一次请求（包括技术性小改）都先产出业务判断卡：

```
目标 / 大方向 / 成功标准 / 用户明确要求 / 推断 / AI 假设 / 置信度
→ 信息充分：跳过联网，显示“无需搜索”
→ 信息不足：最多 2 条 web_search，来源进卡（AI_BUSINESS_RESEARCH=true 时）
→ 还缺方向性决定：列成“待确认”
```

“不添油加醋”的标志不是提示词，而是**需求台账**：用户明确说的、推断的、AI 假设的、
联网参考的分开标注。生成器和编辑器只能把前三类当作要求；可选增强只作为建议。
公开 `.env.public` 设 `AI_BUSINESS_RESEARCH=false`，所以公开演示不会联网调研，
研究阶段返回 `disabled`，不是“搜了但没结果”。

### 读全图，写选区

`ScopedDraft` 允许读整张图（`inspect_canvas`），但确认模式的可写集合是：

```
用户选区 ∪ 已批准方案里的 changes[].target ∪ plan.focus_nodes
```

方案里点名的额外节点，是用户批准方案时一并授权的；**没有选区时，方案本身
定义可写区域**。名单之外的已有节点只读，`update_node` / `delete_node` 会收到
“只读”错误，不是异常。`add_node` 永远允许；`connect` / `disconnect` 只要一端
在可写集合内就允许（边界连接）。

### 位置与连线

- 编辑**不重排已有节点**；新节点放在入邻居右侧 / 出邻居左侧 / 两者之间；
- 新增 `disconnect` 工具，模型可以改线而不必删节点重建；
- 每次修改后推完整图快照，前端实时应用，整个回合只压一次撤销历史。

### 验证与产出

- 校验通过后，若 `run_self_test` 为真则试跑；可选带 `sample_input`，用真实样例跑；
- 可选带 `expected_output`，试跑后由一次 LLM 判定实际产出，返回
  `matches / reason / gaps / suggested_fix`；不匹配最多再修一轮；
- **只有放手模式**会在“最终校验的 blocking error 数比原图多”时**自动回滚**并在
  报告里说明；错误数相同、确认模式、试跑失败或产出比对失败都只报告，不回滚；
- 公开模式 `AI_SELF_TEST=false` 时通常不试跑；但带 `expected_output` 会强制试跑，
  否则无法做产出比对（§2b）。

### 阶段事件

`POST /api/ai/copilot/stream` 的 `stage` 事件驱动画布上方的层叠栈：
`insight → investigate → research → plan → edit → validate → test → review → done`。
`investigate` 只在洞察判定需要实时事实时出现；跳过/失败都如实显示，不用定时器
假装进度。

### 版本记录

`PUT /api/workflows/{id}/ai` 保存画布并把本次修改记录写进新版本行的
`meta.ai_change`（请求、模式、判断卡、方案、diff、试跑、产出比对）。
记录不进图、不进哈希、不影响导出。

顶栏「版本历史」打开版本对话框：左侧列表（AI 版本带标记），右侧是
请求/摘要/目标/试跑、语义 diff、只读画布预览和「恢复此版本」。
对比对象可选当前画布或任一版本；diff **忽略 position**。

### 多轮与范围

- 侧栏每轮发送最近 8 条消息（每条 ≤2000 字），后端再裁到 6000 字；
  这样「再短一点」「那个节点」有指代对象，又不会把整段会话塞进每次请求。
- 确认模式下，选区在画布上有半透明虚线框和「AI 写入范围」标签，跟随缩放/平移。

---

## 2e-2. 对话历史、深度思考与 token 实时显示

- **闲聊不建流程，也不固化回答**：问候/闲聊同样交给模型判断；模型返回
  `request_kind=chat` 时只回答，不产生 plan/report，也不显示阶段卡片。
  没有关键词表或固定回复——「你好」的回答由模型生成，混合真实请求的
  「你好，帮我改…」仍走编辑流程。
- **对话历史跟随工作流**：历史按工作流 id 分组，侧栏只显示当前画布的对话；
  切换工作流时自动换入那个工作流最后一段对话，未保存的新画布从空白开始。
  最多保留 20 段（标题、时间），存在当前浏览器 `localStorage`，可切换、删除；
  `新对话` 只清空当前工作流的当前会话，旧对话保留。历史不上传服务器。
- **原生推理（DeepSeek thinking）**：`deepseek-flash` 的 thinking 默认开启。
  `LLM_THINKING=auto|enabled|disabled`（默认 `auto`：DeepSeek 端点启用，其他
  OpenAI 兼容端点不发送该字段），`LLM_REASONING_EFFORT=low|high|max`。
  `ReasoningChatOpenAI` 在流式和非流式响应里保留 `reasoning_content`，因为
  `langchain-openai` 1.6 的 delta 转换会把它丢掉；工具调用后下一轮会把
  `reasoning_content` 原样回传。**原始 SSE 实测**：`reasoning_content` 2.46s
  开始、`content` 2.78s；应用内首个 `thinking` 事件 3.01s。
- **「深度推理」开关**：画布上方模式旁的持久开关；开启后洞察提示词要求模型
  比较至少两种做法并先写 `reasoning`，作为没有原生推理时的兜底。原生
  `reasoning_content` 到达时优先显示，JSON 里的 `reasoning` 不再重复。
  **工具调用轮显式关闭 thinking**：DeepSeek 的 thinking + 30 次工具调用实测
  过慢（一次 212s 后超时），且要求回传上一轮 reasoning；修改轮的价值在工具与
  校验轨迹，不在隐藏推理。关闭时只影响工具轮。
- **副驾能运行、读运行、读数据库**：`run_workflow` 用真实运行时执行当前草稿，
  返回状态、输出、失败节点、逐节点步骤和日志；`get_run_results` 读本工作流已
  持久化的运行（可带 `run_id` 看逐节点输入输出）；`query_database` 读
  `results.db` 的表与行。真实实测：一次请求内运行 start→end 14.9ms 通过并列出
  4 张表 323 行；随后读回持久化 run `565212b1871f` 的 succeeded 与逐步结果。
  公开演示拒绝 AI 触发运行（避免绕过 Run 队列和启动额度），编辑与读取仍可用。
- **副驾的完整工具面（2026-10-23）**：
  - 执行：`run_workflow`（跑整张草稿）、`test_node`（单独跑一个节点，带上游输入）、
    `run_python`（在 Code 节点同一资源受限沙箱里跑短 Python：无 shell、无 `open`、
    无网络，模块白名单；公开演示不绑定）；
  - 数据：`query_database`（读 results.db）、`insert/update/delete_database_rows`
    （增删改；update/delete 必须带 where，最多 500 行，仅本地模式）；
  - 记录：`get_run_results`（持久化运行，带 `run_id` 看逐节点）、`list_versions` /
    `get_version`（历史版本，返回结构摘要而不是整图 dump）；
  - 画布：`inspect_canvas`（流向）、`get_canvas_layout`（坐标/尺寸/边）；
  - 联网：`search_web`（公开搜索结果，按不可信数据对待）；
  - 识图：`deepseek-flash` 原生支持图片，副驾最多 4 张，用户消息里以
    `image_url` 块发送；前端会把大图缩到 1600px/JPEG。**真实图片实测**通过。
- **思考阶段也能调工具（2026-10-26）**：洞察 JSON 先给出 `needs_investigation`；
  为 true 时在方案前插一个只读调查轮，绑定 `get_run_results`、`query_database`、
  `list_versions`、`get_version`、`get_canvas_layout`、`search_web`，最多 3 轮 /
  6 次调用。工具事件进入侧栏「执行记录」，调查摘要写进方案提示词的
  「Evidence gathered before planning」。运行、写库、单节点测试仍留在编辑阶段：
  调查轮不允许任何会改数据或执行图的操作。
- **AI 试跑在运行面板可见**：试跑结果带逐节点 `trace`，前端存入 `selfTest`，
  在 Run 标签显示「AI 试跑」卡片和节点步骤；卡片明确标注它是草稿试跑、不写入
  运行历史，避免被误认为 `/runs/history` 里的持久化运行。
- **多轮执行显式可见**：侧栏「执行记录」按顺序列出每一次 `add_node` /
  `connect` / 校验 / 修正 / 试跑 / 产出比对，带成功/失败状态；上方阶段条只
  显示当前阶段，不再把四轮校验压成一行。思考块按「第 N 轮」编号。
- **放手模式不因人工信任门回滚**：`untrusted_node` 是等人勾选「受信任」的
  闸门，不是模型可修复的缺陷。它不再计入回归判定，也不触发修正轮；实测
  Code 求和 agent 22.9s 完成、`applied=True`、`corrections=0`。
- **流式快照保持文档 id**：编辑期间每张快照沿用原 graph id。此前每次
  `to_graph` 都生成新 uuid，前端把它当成切换工作流，清空该工作流的对话记录；
  现在快照 id 与保存后的文档一致。
- **流式思考与回答**：洞察调用改用 `gen_node._stream_message`（`model.astream`）。
  `AnswerStreamer` 先增量解析 JSON 的 `reasoning` 字段（`thinking` 事件，`append`
  表示续写同一块），再在确认 `request_kind` 为 chat/question 后解析 `answer`
  （`token` 事件）。提示词要求字段顺序为 `reasoning → request_kind → answer`，
  所以用户先看到思考、再看到回答；前端把 deltas 追加到同一气泡，`done` 时用
  最终答案收尾。编辑请求没有 `answer`，不会泄漏流式文本；若模型字段顺序异常
  则自动退回整段。
- **思考块属于那条回答**：`thinking` 数组挂在对应的 assistant 消息上，渲染在
  正文上面；正在流式时展开，结束后折叠。之前它是一个全局数组、固定在消息列表
  末尾渲染，所以无论后端顺序多正确，界面上思考总在回答下面。旧会话仍兼容：
  消息上没有思考块时才回退到全局块。
- **正文可读性**：assistant 正文从 12px muted 改为 14px 正文色、1.75 行高；
  回答用 Markdown 渲染（GFM），列表/标题/代码块不再当纯文本挤成一段。
  编辑报告保持 13px，用户消息保持气泡样式。
- **工具语气**：构建时 AI 的 system prompt 统一追加 tone 规则——它是生产工具里的
  工作流工程助手，不是生活助手；直接给结论，证据不足时点名缺什么，不问候、不道歉、
  不煽情、不用 emoji/感叹号、不堆“也许/可以考虑”。温度固定 `0.0`（新增测试钉住）。
  真实 `deepseek-flash` 实测：「你好」不寒暄，直接说明画布现状与可做的操作；
  「够不够用？」一句话给出结论。
- **为什么有时像“憋很久突然一条”**：问题/闲聊的答案是真流式；但编辑流程在
  洞察之后还有一次**方案 JSON** 调用和多次**工具调用**，这两段没有可读的文本
  token（方案是结构化 JSON，工具调用是参数），所以侧栏会安静几秒到十几秒。
  实测一次编辑：thinking 4.6s 开始、方案 11.3s 返回、工具 13–19s、done 19.3s。
  侧栏现在会在等待期间显示当前阶段（理解/方案/修改/校验/试跑）的进度行，不再
  只有最后的报告。
- **token 实时显示**：
  - 副驾：每次模型回复推 `{"type":"usage","usage":{...},"total":{...}}`；
    侧栏标题旁显示本轮累计 token（`CopilotResult.usage` 也会在 `done` 中返回）。
  - AI 生成：同样推送 `usage` 事件，对话框状态行显示累计 token；
    结果附带 `usage`。
  - Agent 运行：画布下方运行面板按 `node_output_chunk.usage` 实时累加，
    `run_finished` / `run_failed` 用预算的权威总数覆盖，显示在运行步骤下方。
  - 供应商未上报 usage 时不显示一个假的 0；运行面板沿用「token 未上报」。

---

## 2e-3. 画布 AI 的平台知识：不只画图，还知道怎么跑

此前模型只看到节点目录和当前图，不知道「一次运行有界」「重复靠调度器」「没有
Wait 节点」「导出包有 daemon」，因此会把连续监控画成一张不成立的循环图。

现在每次生成/修复/副驾洞察/方案/编辑的 system prompt 都追加一段
**live platform brief**（`app/ai_buildtime/platform_brief.py`）：

- 单次运行的边界（`recursion_limit`、`MAX_RUN_*`，从当前 settings 读）；
- 调度器支持的频率（minutely…monthly，最小 1 分钟），并明确 schedule 是平台
  对象，不是节点或边；
- Loop 的硬上限（从节点 spec 读）与「没有 Wait/Delay 节点」；
- 导出包的 `main.py daemon`、周期日志/健康文件、它属于部署而非图；
- 当前是 public demo / Code / 调度是否启用；
- **决策表**：持续监控 → 一个有界 DAG + 调度；重试/迭代 → 有界 Loop；
  写生产系统但无 OPC UA/MQTT 连接器 → 明说缺什么，不许用 HTTP 假装；
- 保存是人的动作，计划指向最后一次保存的版本；Human 节点等人，不是定时器。

副驾还能调用平台工具（`app/ai_buildtime/platform_tools.py`）：

| 工具 | 行为 |
|---|---|
| `get_platform_context` | 读当前工作流的真实计划 + 上面那份 brief |
| `create_schedule` | 创建计划，**强制 paused**；输入必须匹配 Start schema |
| `set_schedule_enabled` | 暂停/启用本工作流的计划；跨工作流拒绝 |
| `deployment_plan` | 返回导出包的 daemon 命令，不导出、不运行 |

公开演示和 `SCHEDULER_ENABLED=false` 下，创建/启用计划会返回错误，不会绕过策略。
副驾不会声称「已创建并正在运行」：创建结果明确写着 PAUSED，需要用户保存后显式启用。
洞察阶段没有工具，所以当前计划列表直接放进上下文（读不到时写「无法读取」，不猜）。

### 实测（2026-10-23，真实 DeepSeek）

- 问 IM-07「现在有定时计划吗，下次什么时候跑？」——模型从 live 上下文准确报出
  minutely 与 daily 两条计划、UTC 时间与下次触发，并指出日报那次的时区问题。
- 对一个探针工作流说「设为每分钟运行，只创建计划不要改图」——模型先调用
  `get_platform_context` 和 `inspect_canvas`，再调用 `create_schedule`，创建出
  `enabled=False` 的计划；总结明确说「已保存？还需手动启用」。

## 2f. 公网演示：AI 额度、试跑与 token 记账

公开演示没有账号，AI 也不是“无限试用”。额度键、账本和完整配置见
[`../env/deploy.md`](../env/deploy.md) §1.1，这里只记 AI 侧的行为。

### 两条记账路径，进入同一本账

| 调用来源 | 入口 | 记账方式 |
|---|---|---|
| 运行时 Agent/LLM 节点 | `runtime/ai/gateway.py` 的 `complete()` / `stream()` | 网关回调 `set_usage_sink(record_usage)`，由 `app/main.py` 安装 |
| 构建时 AI（生成/修复/补节点/副驾） | `ai_buildtime/*` 直接建 LangChain model | 在每次回复后调用 `metering.record_reply_usage(response)` |

构建时 AI **仍不走 `ModelGateway`**：它花的是平台侧的“人点 AI”预算，不是某个
Agent 运行的成本。但这不代表不记账——`metering.py` 把它的 token 也写进同一个
访客/IP/全局账本；它属于平台侧模块，导出 agent 不包含 `ai_buildtime`，所以
runtime 不依赖平台安全包的边界没有被破坏。

### 额度规则（`.env.public`）

| 维度 | 值 |
|---|---|
| 每访客 AI 请求 | 3 / UTC 日 |
| 每 IP AI 请求（IPv6 `/64`） | 10 / UTC 日 |
| 全站 AI 请求（rate limiter） | 300 / UTC 日，先于门禁执行 |
| 每访客运行 | 2 / UTC 日 |
| 每 IP 运行 | 20 / UTC 日 |
| 单次 AI 请求 token | 30,000 |
| 每访客每日 token | 50,000 |
| 每 IP 每日 token | 200,000 |
| 全站每日 token | 5,000,000 |
| 单次运行 token | 50,000 |

- **前端实际的“运行”也计数**：编辑器 POST `/api/runs/adhoc`，节点测试 POST
  `/api/runs/node-test`；两者都按一次运行扣访客/IP 次数，并建立 token 账本。
  `/api/runs/{id}/resume` 是对已计数运行的续跑，不再占一次运行，但模型 token
  继续计入（2026-10-21 修正）；
- 确认模式一轮是两次 `/api/ai/copilot/stream`（理解/方案 + 批准执行），各占一次
  AI 请求；自动填补、修复、生成也各算一次；
- 请求计数在进入 AI/运行 handler **之前**原子扣减，处理失败或中途取消也占一次；
- token 按模型返回的 usage 记账；provider 不返回 usage 时按 **1,000 token** 保守计；
- 已花掉的 token 先入账；若本次请求超过 `AI_REQUEST_TOKEN_LIMIT` 会**立即中止当前
  调用**（非流式 429，流式 SSE `quota_exceeded`），后续请求也会被 headroom 拒绝；
- **429 有两个 code**：`quota_exceeded` 来自每日账本/单次 token 上限；
  `rate_limited` 来自外层滑动窗口（每分钟、每 IP 24h、全局 AI 300/日）。
  外层限流在 Gate 之前，未通过人机验证的请求也可能先消耗全局 AI 日限；
  QuotaMiddleware 的访客账本在 Gate 内，未验证请求不会扣它；
- 流式请求开始前超限返回 HTTP 429；运行真正启动后超限会表现为运行失败/SSE
  错误，而不是 HTTP 429（HTTP 响应已经返回 201 了）。

### 公开演示下的 AI 行为

- 生成器的 `add_node` 不提供 Code；`node-to-code` / `code-to-node` 是 admin-only；
- `AI_SELF_TEST=false`：生成后不做自动试跑；副驾带 `expected_output` 时仍会跑一次
  做产出比对；
- `AI_BUSINESS_RESEARCH=false`：业务洞察卡不做联网搜索，研究阶段为 `disabled`；
- 公开模式 provider 异常只返回通用错误（完整异常进服务端日志），不把 base URL、
  请求体或密钥回显给浏览器；流式和非流式端点都走同一条脱敏逻辑；
- 顶栏额度是进入页面时的快照，消费后不会实时刷新；刷新页面或重新请求
  `/api/access/status` 才会更新；
- 额度不是身份认证：清 Cookie 后 IP 账本仍生效，但换 IP 仍可重新获得额度。

**相关**：门禁/限流/账本的实现 →
[`security.md`](security.md) §7b；备份时 `quotas.db` 已纳入 →
[`../env/deploy.md`](../env/deploy.md) §9。

---

## 3. [重要] 三重护栏：防止「删光就没错」

**最危险的失败模式**：模型把用户的节点删光 → 校验通过 → 用户体验灾难。

按可信度递增排列：

| # | 护栏 | 是否承重 |
|---|---|---|
| 1 | **prompt 明令禁止**（`REPAIR_SYSTEM` 规则 2） | [ ] 最便宜，但模型可以无视 |
| 2 | **结构化 diff**（`describe_diff`） | [x] **承重** |
| 3 | **`regressed` 标记**（仍失败 **且**节点变少） | [x] **承重** |

**第 2 条为什么对比「两张图」而不是解析 tool transcript**：
transcript 记录的是「**尝试**」，含失败的操作；**图之间的差异才是「实际发生」的**。

UI 把**被删节点单独列在红色 callout 里，排在变更明细之前**（摘要和状态徽章之后）。

**而且是「警告不阻断」** —— 合法修复偶尔确实需要删重复节点，**用户是裁判**。

**diff 用 node id 判定身份**，所以改个名字显示为「修改 1 个」
而不是「删 1 个 + 增 1 个」—— 后者会让用户**不再相信这个 diff**。

---

## 4. 其它刻意的设计

| 设计 | 理由 |
|---|---|
| **已合法则立即早退**（`_clean_result`） | 不只是省 token：对健康图做修复只有风险，而且会给用户看一个「改了 0 项」的 diff |
| **保留 id 和 name** | `GraphDraft.to_graph()` 会生成新 id（**生成**确实是新工作流）；**修复**必须覆盖回原 id，否则保存会变成**新增一份**而不是更新 |
| **不改用户的 `label` / `description`** | 那是用户的话。新增节点才由 AI 起名 |
| **喂给模型的 issues 永远英文** | 原则归 [`i18n.md`](i18n.md) §2 —— 语言设置不该**悄悄改变模型的推理** |
| **消息里内嵌的目录标签要翻译** | `「LLM」的参数「提示词」`（不是「Prompt」）—— 指向屏幕上不存在的名字比不翻译更糟 |
| **severity 默认只修 `error`** | 缺 End 节点在本项目里是 **warning**，所以默认修复不会顺手加一个用户没要的节点；警告**照实报告**，不然用户看到徽章变干净会以为工作流已经完整。UI 有「同时修复警告」开关 |

---

## 5. 可测性：把「信任决策」从「驱动模型」里拆出来

`conftest.py` 强制 mock 模式，所以循环本身在 pytest 里跑不了
（会抛 `AINotConfigured`）。

于是把「由前后两张图算出结论」的部分单独抽成 **`build_repair_result()`**：
身份保留、diff、`regressed`、summary 全在里面。

**这不是为了好看** —— 第一次实现时 `regressed` 和 id 保留**根本没人测**，
负向验证直接证明它们是「装饰品」。

`tests/test_repair.py` 共 **35 项**，覆盖纯函数 + 早退守卫 +
`build_repair_result` 的每条规则。**真实模型调用放在 `smoke.py --ai`**
（那是唯一该放的地方）。

---

## 6. 注意： 负向验证：护栏可能是装饰品

写完守卫**必须**故意制造一个它该抓的错误，确认真的报 FAIL，再恢复。

第一次写完时 4 个护栏里有 **2 个完全没被测试覆盖**：

```
FAIL  regressed flag never set -> the guard is decorative
FAIL  identity no longer preserved -> the guard is decorative
```

补测后 7/7 全部被抓：

```
OK  early exit removed -> healthy graph reaches the model
OK  regressed flag never set
OK  regression warning dropped (flag set, user never told)
OK  removed nodes omitted from the diff
OK  identity no longer preserved
OK  diff stops keying nodes by id (a rename would look destructive)
OK  severity filtering dropped (would also chase warnings)
```

→ 方法论 → [`../lessons/process.md`](../lessons/process.md) §反向验证

---

## 7. 顺带修掉两个前端真 bug

给修复对话框写提示文案「可用 Ctrl+Z 撤销」时去验证，发现**撤销按钮是禁用的**：
`replaceGraph`（AI 生成与 AI 修复的「应用」都走它）先 `pushHistory()`，
再调了会清历史、并把 `dirty` 置 `false` 的 `setGraph()`。

→ 完整根因、`graphFields()` 修法，以及同一处的 React StrictMode 双跑 effect，
见 [`../lessons/frontend.md`](../lessons/frontend.md) §2 / §3。

---

## 8. 文件位置（分层时注意）

`app/ai/` 在 27a 已按**什么时候调模型**拆开：`gateway.py` / `provider.py` / `messages.py`
进了 `runtime/ai/`（运行时）；`gen_graph.py` / `gen_node.py` / `repair.py` /
**`copilot.py`** / **`self_test.py`** / **`metering.py`** / `prompts.py` / `parsing.py`
进了 `ai_buildtime/`（构建时）。清单归 [`files.md`](files.md)，逐层含义见
[`../reference/backend-layers.md`](../reference/backend-layers.md) §AI 子系统。

**AI 辅助功能没有迁移到 `ModelGateway` 是刻意的**：它们是**平台功能**（人点「AI 填充」），
花费属于**平台的预算**，不属于某个 agent 的成本 —— 与 agent 节点的区分、
来源与理由见 [`../log/phase-1-3-gateway.md`](../log/phase-1-3-gateway.md) §为什么没迁移。
但“不走 `ModelGateway`”不等于“不记账”：2026-10-21 起，构建时 AI 通过
`ai_buildtime/metering.py` 的 `record_reply_usage()` 上报 token，和运行时网关的
`set_usage_sink` 进入同一套访客/IP/全局额度（§2f）。`metering.py` 属于平台侧，
导出 agent 不包含 `ai_buildtime`，所以 runtime 的依赖边界仍然成立。

---

## 相关

- 分层与归属 → [`../target/vision.md`](../target/vision.md) §三层
- 逐层实现 → [`../reference/backend-layers.md`](../reference/backend-layers.md) §AI 子系统
- 护栏的负向验证 → [`../lessons/process.md`](../lessons/process.md)
- 相关的坑 → [`../lessons/frontend.md`](../lessons/frontend.md)
