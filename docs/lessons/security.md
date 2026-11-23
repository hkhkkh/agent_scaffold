---
title: 安全的教训
read_this: 改安全相关代码之前
---

# 安全的教训

> 安全的当前状态 → [`../current/security.md`](../current/security.md)
> 本文件是**教训**。

---

<a id="s1"></a>

## 1. [最重要] 最危险的一类：「控制存在、被测过、但没接上」

### 症状

写 `test_auth.py` 时，**所有中间件测试都自己构建一个 `FastAPI()` 再挂中间件**。

结果是：**从 `main.py` 删掉 `app.add_middleware(AuthMiddleware)`，
整套测试依然全绿。**

### 为什么这是最危险的一类

| 维度 | 状态 |
|---|---|
| 控制逻辑 | [x] **完全正确** |
| 测试覆盖 | [x] **充分** |
| 产品里 | [ ] **根本没启用** |

**没有任何测试会失败，而且代码读起来完全没问题。**

这是**反向验证**抓到它才暴露的（护栏计数与它带的 caveat 见
[`../current/status.md`](../current/status.md) §汇总）——
它是 Phase 0 唯一一个「最初没被抓到」的用例。

### 修法

新增 `TestTheRealApplicationIsWired`，**直接断言真实应用对象**：

```python
class TestTheRealApplicationIsWired:
    def test_the_shipped_app_installs_the_auth_middleware(self):
        installed = [m.cls for m in app.main.app.user_middleware]
        assert AuthMiddleware in installed

    # [重要] 顺序也要断言
    def test_cors_is_outermost_so_preflight_is_answered(self):
        installed = [m.cls for m in app.main.app.user_middleware]
        assert installed.index(CORSMiddleware) < installed.index(AuthMiddleware)
```

**`CORSMiddleware` 必须在 `AuthMiddleware` 外层** ——
元素顺序 = 包裹顺序，index 越小越外层。
**顺序错了预检请求会被 401 掉。**

### [重要] 推广

> **凡是「必须挂上才生效」的东西**（中间件、路由注册、事件订阅、迁移），
> 测试都该断言**真实应用对象**，而不是自建的替身。

这条适用于：`main.py` 的中间件、`nodes/__init__.py` 的注册、
`register_spec_refresher()` 的挂载……

---

<a id="s2"></a>

## 2. [重要] 门禁被自动化绕过的三种形态

| 形态 | 例子 | 发现方式 |
|---|---|---|
| **自动设为通过** | `gen_graph.py` 把 AI 生成的 Code 节点的 `trusted` 自动设为 `True` | 读代码（`requires_trust` 存在的**全部意义**就是「让人先看一眼」，自动设为 True 让门禁**变成装饰品**） |
| **静默降级** | 沙盒 daemon 挂了 → 降级成本地执行 | 反向验证（「容器不可用时回退本地执行」必须报 FAIL） |
| **未知值静默回退默认** | `SANDBOX_RUNNER=containers`（拼错）→ 回退 local | 同上 |

### 规则

**失败必须关闭（fail closed）。**

静默降级会把「**隔离坏了**」变成「**隔离被悄悄拿掉了**」，**后者严重得多**。

同理，**未知的配置值必须报错**，不能静默回退默认。

---

<a id="s3"></a>

## 3. [重要] 守卫被自己的清理步骤删掉

### 故事（先前就存在的 bug）

`safe_builtins()` 先装上**守卫版 `__import__`**，
紧接着 `for blocked in _BLOCKED_NAMES: pop()` ——

**而 `_BLOCKED_NAMES` 里正好有 `"__import__"`。**

### 结果

**所有 `import` 全部失效**，报 `ImportError: __import__ not found` ——
**而那个守卫看起来还在**。

### 修法

1. 从 blocklist 移除它（AST 守卫负责挡裸引用）
2. [重要] **把 allowlist 改成真正的机制**（blocklist 变成兜底网）

### [重要] 第 2 步的验证很关键

配了一个测试 `test_blocklist_overrides_the_allowlist`：
**人为把 `getattr` 加进 allowlist**，断言被 blocklist 摘掉。

**否则那一步「减法」是无操作，删掉也不会有测试发现。**

> 第一版**正是如此** —— 那个减法步骤在，但没有任何测试覆盖它。

---

<a id="s4"></a>

## 4. [重要] 移除原语是让 AST 守卫有意义的前提

### 容易被低估的一条

`getattr(o, "__cla" + "ss__")` 能**绕过任何 AST 检查** ——
因为守卫看到的是**字符串拼接**。

### 所以

从 builtins **移除 `getattr` / `setattr` / `hasattr` / `delattr`**。

**移除这个原语之后，AST 守卫才不是装饰品。**

`{}.__class__` 会被 AST 守卫拦（并给出**行号**），
`getattr({}, 'x')` 会 `NameError`。

---

<a id="s5"></a>

## 5. [重要] 认证的规则必须是**单向**的

```
认证关闭，当且仅当可证明它不必要。
「不必要」只有一个含义：服务器绑定在回环地址上。
```

`resolve_auth_mode()` 的关键行为：
**配置可以让服务器更安全，不能更不安全。**

| 情况 | 行为 |
|---|---|
| `AUTH_MODE=local` + 回环绑定 | [x] 免认证 |
| `AUTH_MODE=local` + `0.0.0.0` | **自动升级为 token 模式** |
| 升级后没有配 token | **拒绝启动** |

**为什么拒绝启动而不是警告**：

> 「**意外发布一个能执行任意 Python 的无认证控制面**」
> 比「启动失败」**严重得多**。

### 第二道纵深防御

`local` 模式下除了检查配置的 bind，**还校验真实客户端地址**。

**防的是什么**：`SERVER_HOST` 写 `127.0.0.1` 但 uvicorn **实际 bind `0.0.0.0`** ——
**一个配置不一致就会发布控制面**。

---

<a id="s6"></a>

## 6. [重要] 中间件 vs 逐路由依赖

选了**中间件**，因为它**默认关闭**。

| 方式 | 失败模式 |
|---|---|
| 逐路由依赖 | 每个新路由都是一次**遗忘的机会** —— 忘了就静默公开一个端点 |
| **中间件** | `/api/*` **默认受保护**，公开路径要显式列进 `PUBLIC_PATHS` |

> **让「安全的做法」成为「默认会发生的事」** ——
> 忘记做事是**安全**的。

**同一个思路也用在了**路径遍历的修复上
（schema 加正则 **+** 仓库层二次守卫）。

---

<a id="s7"></a>

## 7. [重要] 凭据不能进日志

`SecretProvider.describe()` 是**整个接口里最容易被写坏的一处**：

```python
def describe(self) -> dict[str, bool]:
    return {name: self.has(name) for name in KNOWN_SECRETS}   # 值绝不出现
```

**一个「顺手把值也带上」的改动会让凭据进入日志文件。**

启动日志因此长这样：

```
Secrets      : llm.api_key=present
```

**反向验证里专门有一条把 `describe()` 改成返回值** ——
必须有测试报 FAIL。

---

<a id="s8"></a>

## 8. 界面不能被允许「静默无效」

### 故事

`PUT /api/settings/llm` 允许在界面里改 API key（写内存，不落盘）。

但配了 `SECRETS_FILE` 之后，那个写入**会被 provider 无视** ——

**界面报成功，模型继续用旧 key。**

### 修法

现在返回 **409** 并说明要去改 secrets 源。

**静默无效比报错糟糕得多** —— 用户以为改好了。

---

<a id="s9"></a>

## 9. 残留风险必须**写进文档**，不是藏着

**子进程限制的是 CPU 和内存，不隔离文件系统。**

Code 节点以后端用户的身份运行，**能读写该用户能读写的一切**。

### 处理方式

| 做法 | 说明 |
|---|---|
| **写进 `sandbox.py` 模块 docstring** | 那里是读代码的人第一眼会看的地方 |
| **写进 `security.md`** | 运维要能看到 |
| **配一个测试断言它被文档化** | `TestSandboxResidualRiskIsDocumented` |
| **为什么 `requires_trust` 才有意义** | 勾选它等于「**我接受这段代码以我的用户权限运行**」 |

> **这是已文档化限制，不是疏漏。**
>
> 真正的文件系统/网络隔离需要**容器 profile**。

---

<a id="s10"></a>

## 10. 认证的「误判」会造成功能不可用

### 现象

「从前端从别的机器访问不通」

### 这是**设计如此**，不是 bug

后端 `AUTH_MODE=local` **只接受回环客户端**，
且 `SERVER_HOST` 必须是回环地址**才允许** local 模式。

要对外服务：

```
SERVER_HOST=<真实bind地址>
AUTH_MODE=token
AUTH_TOKENS=name:token:scope
```

> **排查时不要把它当成 bug。** 这是「默认安全」的直接后果。

---

## 相关

- 安全当前状态 → [`../current/security.md`](../current/security.md)
- Phase 0 的完整记录 → [`../log/phase-0-security.md`](../log/phase-0-security.md)
- 反向验证方法论 → [`process.md`](process.md)
- 导出时的三个安全点 → [`../target/delivery.md`](../target/delivery.md) §5
