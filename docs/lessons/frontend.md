---
title: 前端与 store 的坑
read_this: 改前端 / 遇到奇怪的渲染问题时
---

# 前端与 store 的坑

---

<a id="f1"></a>

## 1. [重要] store selector 绝不能返回新数组 / 新对象

### 症状

控制台报 React Flow 的 `StoreUpdater` **"Maximum update depth exceeded"** ——
**看起来像 React Flow 的问题，实际是自己的 selector。**

前置信号是：

```
The result of getSnapshot should be cached to avoid an infinite loop
```

### 根因

`selectErrors` / `selectWarnings` 原本用 `.filter()` 从 `issues` 派生。

**`.filter()` 每次返回新数组引用** → Zustand 5 的
`useSyncExternalStore` 认为快照变了 → 重渲染 → 再分配 → **无限循环**。

### 修法

改成**普通函数**，组件订阅**稳定的 `issues`** 再用 `useMemo` 派生：

```typescript
// [ ] 每次调用返回新数组
const errors = useWorkflow(selectErrors);

// [x] 订阅稳定的值，自己派生
const issues = useWorkflow((state) => state.issues);
const errors = useMemo(() => selectErrors(issues), [issues]);
```

**规则**：selector 只能返回**已经存在于 store 里的引用**，
或者**原始值**。

---

<a id="f2"></a>

## 2. [重要] `setGraph` 会清历史，所以 `replaceGraph` 不能走它

### 症状

给修复对话框写提示文案「可用 Ctrl+Z 撤销」时去验证，
发现**撤销按钮是禁用的**。

### 根因

`replaceGraph`（AI 生成和 AI 修复的「应用」都走它）先 `pushHistory()`，
然后调 `setGraph()` —— 而 `setGraph` 里有 `past: [], future: []`。

**它把自己刚压入的历史清掉了。**

### 同一处还有第二个 bug

`setGraph` 设 `dirty: false`。所以应用 AI 改动后「未保存」标记**会消失** ——

用户用了一个糟糕的修复、关掉标签页，**改动就无声无息地丢了**。

### 修法

把「图 → store 字段」的投影抽成 `graphFields()`，两者共用：

| 函数 | 历史 | `dirty` |
|---|---|---|
| `setGraph`（**打开另一份文档**） | 清（撤销回上一个工作流毫无意义） | `false` |
| `replaceGraph`（**应用一次编辑**） | **保留** | `true` |

### 教训

**一个函数同时服务两个调用者时，先问它们的副作用需求是否一致。**

「加载文档」和「应用一次编辑」在**历史与 dirty 上的要求正好相反**。

### 这两个 bug 已经存在很久

**从「AI 生成」上线起就存在**，但画布上看不出来、**类型检查也抓不到**。

现在由 `frontend/src/store/workflowStore.test.ts` 钉住
（前端**首个**测试文件，vitest + jsdom 早就配好了但一直没用）。

---

<a id="f3"></a>

## 3. [重要] React StrictMode 会把自动运行的 effect 跑两遍

### 症状

`RepairGraphDialog` 一打开就自动修复。**浏览器实测确认发了两遍请求** ——
而每次都是**一次花钱的模型调用**。

### 根因

`useEffect` 在开发模式下被 StrictMode 调两次。

### 修法

用 `useRef` 记录上次运行用的 key，**key 相同就跳过**。
这样首次挂载只跑一次，切换开关（key 变化）仍然会重新跑。

### 推广

**这只是开发模式的问题，但成本是真的。**

任何「挂载即执行昂贵副作用」的 effect 都该这样处理。

---

<a id="f4"></a>

## 4. `emptyGraph()` 的字段名是 `id`，store 里叫 `graphId`

直接 `...emptyGraph()` 展开**不会**设置 `graphId`（字段名不匹配），
TS 报缺失属性。

### [重要] 修的同时发现一个真 bug

`reset()` 原本也**没有**更新 `graphId` ——
导致「新建工作流」后保存会**覆盖上一个工作流**。

两处均已显式赋值。

---

<a id="f5"></a>

## 5. vitest 2.x 依赖 vite 5，与项目的 vite 6 冲突

### 症状

类型错误里出现**两条极长的 `Plugin<any>` 路径**，一条在
`node_modules/vite`，另一条在 `node_modules/vitest/node_modules/vite`。

### 根因

npm 装出了**嵌套的第二份 vite**，两套类型互不兼容。

### 修法

升到 **vitest 3.x**（支持 vite 6，**不再产生嵌套副本**）。

---

<a id="f6"></a>

## 6. `vite.config.ts` 的 `test` 字段需要从 `vitest/config` 导入 `defineConfig`

用 `vite` 的 `defineConfig` 时 `test` 是**未知属性**。

另外 `node:url` 需要 `@types/node`。

---

<a id="f7"></a>

## 7. 其他

### pnpm 在 Windows 上 symlink 失败

```
ERR_PNPM_EISDIR: illegal operation on a directory, symlink '...'
```

已在 `frontend/.npmrc` 设 `node-linker=hoisted`。

**代价**：放弃了「未声明依赖不能 import」的严格性。
**收益**：Windows 上每次都能装上。

> 注意： 若 `frontend/node_modules` 处于 Windows *delete-pending* 状态，
> 这条路整个走不通（改名撞上卡住的目录）—— 那时前端跑在 `%TEMP%\agent_web` 且用 npm，
> 不要在 `frontend/` 里跑 `pnpm install`。见 [`../env/frontend-temp.md`](../env/frontend-temp.md)。

> 以后再遇到怪异的模块解析错误，**先看这个文件**。

### 从别的机器访问前端不通

**这是设计如此。** 后端 `AUTH_MODE=local` 只接受回环客户端，
且 `SERVER_HOST` 必须是回环地址才允许 local 模式。

要对外服务：设 `SERVER_HOST=<真实bind地址>`、`AUTH_MODE=token`、
`AUTH_TOKENS=name:token:scope`。

---

## 相关

- 前端文件职责 → [`../reference/frontend-files.md`](../reference/frontend-files.md)
- 前端为什么在 `%TEMP%` → [`../env/frontend-temp.md`](../env/frontend-temp.md)
- i18n 的坑 → [`i18n.md`](i18n.md)
- 方法论 → [`process.md`](process.md)
