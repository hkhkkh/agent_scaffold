---
title: 安全基线
read_this: 要改安全相关代码 / 要理解安全做到什么程度时
---

# 安全基线（Phase 0 成果，2026-09-11）

目标不是「加功能」，而是**去掉那些一旦共享就会致命的东西**。
每个修复都配了对抗性测试，并且**全部做了反向验证**（计数与「本次未重测」的
caveat 见 [`status.md`](status.md) §汇总）。

---

## 1. 修掉的五个 P0

| # | 漏洞 | 证据 | 修法 |
|---|---|---|---|
| 1 | **AI 自动信任自己生成的 Code 节点** | `gen_graph.py:96` `trusted=bool(spec.requires_trust)` | 一律 `trusted=False`，让校验器报 `untrusted_node` |
| 2 | **全局 API key 可被发往任意 URL** | `provider.py:47/56` | `base_url` 一变就**不继承**全局 key |
| 3 | **`http_request` 无 SSRF 防护** | `builtin.py` 无地址校验、`follow_redirects=True`、JSON 响应无上限 | 新增 `runtime/egress.py` 策略层 |
| 4 | **导入 `id` 路径遍历**（可写任意 `.json`） | `repository.py:115` 直接拼路径 | schema 加正则 + 仓库层 `_artifact_path` 守卫 |
| 5 | **无任何认证** | 全端点开放 | `app/security/` 中间件 |

**第 1 条最值得说**：`requires_trust` 存在的全部意义就是「让人先看一眼」。
自动设为 `True` 让这个门禁**变成了装饰品** —— 用户从没点过那个勾，代码却会执行。

**第 2 条不需要 Code 节点、不需要 trusted**：
只要在节点 `model.base_url` 里填个地址，key 就会以
`Authorization: Bearer` 发过去。

---

## 2. [重要] 认证：默认安全，且不可被意外关闭

```
认证关闭，当且仅当可证明它不必要。
「不必要」只有一个含义：服务器绑定在回环地址上。
```

`resolve_auth_mode()` 的关键行为是**单向**的：
**配置可以让服务器更安全，不能更不安全。**

| 情况 | 行为 |
|---|---|
| `AUTH_MODE=local` + 绑定回环 | [x] 免认证（这是唯一合法的情况） |
| `AUTH_MODE=local` + 绑定 `0.0.0.0` | **自动升级为 token 模式** |
| 升级后没有配 token | **拒绝启动** |

**为什么拒绝启动而不是警告**：
「意外发布一个能执行任意 Python 的无认证控制面」比「启动失败」严重得多。

**第二道纵深防御**：`local` 模式下除了检查配置的 bind，
**还校验真实客户端地址**。这防的是「`SERVER_HOST` 写 127.0.0.1
但 uvicorn 实际 bind `0.0.0.0`」—— 一个配置不一致就会发布控制面。

**Token 设计**：

| 决策 | 理由 |
|---|---|
| **不透明 token**（`sha256(salt\|\|token)` 存储），不是 JWT | 没有第三方需要验签；自托管工具里**吊销**比无状态更重要 |
| 比较用 `hmac.compare_digest` | 会泄漏时序的比较可以被**逐字符爆破** |
| scope：`read` / `write` / `admin` | 中间件校验，**token 先验再看 scope**（无效 token 不能通过响应区分「凭据错」和「权限不足」） |

**中间件 vs 逐路由依赖**：依赖要「记得加」——
每加一个新路由都是一次遗忘的机会。中间件**默认关闭** `/api/*`，
公开路径要显式加进 `PUBLIC_PATHS`。**忘记做事是安全的。**

---

<a id="sec3"></a>

## 3. [重要] 沙盒：子进程是承重的那一层

### 旧实现的问题

`exec` 跑在 API 进程的**工作线程**里，靠 `asyncio.wait_for` 做超时。

**这个组合挡不住 CPU 死循环** —— `wait_for` 放弃的是 *await*，
线程仍在把一个核跑满，永远不结束。而且**没有任何内存限制**。

### 新实现

| 机制 | 作用 |
|---|---|
| **子进程** | **能真被杀掉**；崩溃/死循环不会带倒 API |
| Windows **Job Object**（内存上限 + `KILL_ON_JOB_CLOSE`） | 内核施加，进程内无法放宽 |
| POSIX `setrlimit(AS, CPU)` | 同上，跨平台对等 |
| AST 守卫（禁 dunder 属性/名字） | 挡掉教科书式逃逸链 |
| **从 builtins 移除 `getattr`/`setattr`/`hasattr`/`delattr`** | **这才是让 AST 守卫有意义的前提** |

**最后一条容易被低估**：`getattr(o, "__cla" + "ss__")` 能绕过任何 AST 检查，
因为守卫看到的是字符串拼接。**移除这个原语之后守卫才不是装饰品。**

**子进程启动顺序是安全关键**：
先创建 Job 并 assign，**子进程第一件事是阻塞读 stdin** ——
所以不受信代码不可能在限制生效前开始跑。

### 注意： 必须诚实说明的残留风险

**子进程限制的是 CPU 和内存，不隔离文件系统。**

Code 节点以后端用户的身份运行，**能读写该用户能读写的一切**。

这是 local profile 的**已文档化限制**
（见 `sandbox.py` 模块 docstring 与 `test_security.py` 的
`TestSandboxResidualRiskIsDocumented`），**不是疏漏**。

也正因如此 `requires_trust` 才有意义：
**勾选它等于「我接受这段代码以我的用户权限运行」。**

真正的文件系统/网络隔离需要**容器 profile**。

---

## 4. 出网管控（SSRF 防护）

`runtime/egress.py` 是**能力层的第一个成员**，也是后续所有接口的样板。

**默认拒绝**：私网 / 回环 / 链路本地地址 —— 挡住云元数据
（`169.254.169.254`）和 LAN 扫描。

**每个重定向跳都重新校验**（`egress_max_redirects` 默认 5），
**响应体有上限**（`egress_max_response_bytes` 默认 2MB）。

配置放宽：

```bash
EGRESS_ALLOW_PRIVATE=true              # 重新打开 SSRF，只在该机器上的服务需要时用
EGRESS_ALLOWED_HOSTS=api.internal      # 只豁免特定主机
EGRESS_ALLOWED_HOSTS=127.0.0.1:8101    # 只豁免一个本机端口（本机网关推荐）
```

`host:port` 条目只放开那一个端口；裸 `127.0.0.1` 会连本机 8001 的管理 API
一起放开，因此不要为了一个网关写裸回环地址（`tests/test_security.py`
有对应断言）。

---

## 5. 凭据

`SecretProvider` 抽象（Phase 1 第四刀）：

```python
class SecretProvider:
    source: str                        # "settings" / "memory" / "file"
    def get(name) -> str | None
    def has(name) -> bool
    def require(name) -> str
    def describe() -> dict[str, bool]  # [重要] 只有「有没有」，绝不含值
```

**`describe()` 是最容易被写坏的一处**：一个「顺手把值也带上」的改动
会让凭据进入日志文件。**反向验证里专门有一条把 `describe()` 改成返回值。**

启动日志因此长这样（每个 `KNOWN_SECRETS` 条目一个，按名排序，
`embedding.api_key` 无配置时为 `absent`）：

```
Secrets      : embedding.api_key=absent, llm.api_key=present
```

**`SECRETS_FILE` 优先于 `.env`** —— 这个顺序就是功能本身。
**缺失/格式错误的 secrets 文件是启动错误，不降级。**

→ 详见 [`../log/phase-1-4-secrets.md`](../log/phase-1-4-secrets.md)

---

## 6. 工具白/黑名单

```bash
TOOLS_ALLOWLIST=      # 空 = 所有已注册工具（通用 agent 保持零配置）
TOOLS_DENYLIST=       # 先 allow 后 deny；deny 优先
```

启动时校验拼写并**打印本部署实际拥有的工具清单**。

**拼写错误是 warning 不是 fatal**：agent 会是「没有工具」而不是起不来，
**日志是唯一的线索**。

---

## 7. 注意： 反向验证

每个护栏都被故意破坏过，确认对应测试真的报 FAIL（计数与「本次未重测」的 caveat
归 [`status.md`](status.md) §汇总）：

```
OK  workflow id constraint removed           OK  sandbox falls back to in-process (hangs)
OK  filesystem guard removed                 OK  memory ceiling removed
OK  api key follows a base_url override      OK  local mode honoured on a network bind
OK  egress policy allows everything          OK  token mode starts without tokens
OK  http tool stops consulting the policy    OK  non-loopback peer accepted in local mode
OK  AI auto-trusts generated nodes again     OK  scope implication broken
OK  trust gate stops failing validation      OK  settings demoted to write scope
OK  AST guard neutered                       OK  timing-safe comparison replaced with ==
OK  getattr family restored                  OK  auth middleware removed from the app
                                             OK  cors moved outside auth
```

### 两个值得单独说

**① 「强制 in-process」让测试挂起**，而挂起被记为「已抓到」。
这是**边界承重的实证**：死循环线程永不结束，
`asyncio.run()` 会永远等着执行器关闭。

**② 「从 main.py 移除认证中间件」最初没被抓到** —— 是「控制存在、被测过、
但没接上」的经典盲区，也是最危险的一类：`test_auth.py` 自己构建 FastAPI 实例，
**没有任何测试验证真实应用真的挂上了中间件**。

完整症状、修法（`TestTheRealApplicationIsWired`，并断言 `CORSMiddleware`
在 `AuthMiddleware` 外层）与推广见 [`../lessons/security.md`](../lessons/security.md) §1。

---

## 7b. 公网部署（2026-10-20）

公开模式不是「把 local 模式暴露出去」。它多了一层门禁和一套公开演示策略：

| 层 | 做法 |
|---|---|
| 入口 | Cloudflare Tunnel 出站；公网后端只绑 `127.0.0.1:8002`；本机后端仍在 `8001`；**不做端口转发** |
| 认证 | 保持 `AUTH_MODE=local`，依赖 cloudflared 从回环连接；**必须显式 `--no-proxy-headers`**（uvicorn 默认信任回环代理头，会把 `request.client` 改成真实访客 IP，local 模式的回环检查随后拒绝所有请求） |
| 门禁 | 签名 Cookie（HMAC，12h）；Turnstile 优先，失败时用服务端签名的 SHA-256 PoW；challenge 单次使用 |
| 限流 | 每 IP 滑动窗口；verify/access/AI/run/hook 分开；AI 另有全局日限；429 + `Retry-After` |
| 匿名额度 | 签名 visitor Cookie + IP（IPv6 /64）双账本；AI/运行次数和模型 token 都有每日上限；运行覆盖 `/api/runs`、`/api/runs/adhoc`、`/api/runs/node-test`，`/resume` 只续记 token；账本持久化在 `quotas.db`，重启不清零 |
| 体积/来源 | 纯 ASGI body cap；变更方法要求同源 Origin/Referer 或 `X-Scaffold-Client: web` |
| 公开演示 | Code 禁用（旧图只读，版本历史可查看但「恢复」禁用）、自定义工具禁用、调度器关闭、hook 404、设置/调度/删除/导入/导出/metrics 以及 AI `node-to-code`/`code-to-node` admin-only |
| 数据 | 公网 `DATA_DIR=H:/agent_scaffold_public_data`，本机是 `H:/agent_scaffold/data`；只 seed 选定定义，不复制 runs/checkpoints/memory/exports |
| 错误 | 公开模式 500 不返回异常详情；422 去掉 `input` 回显 |

**本机/公网是两个进程**：本机 `8001` 用正常 `.env`（`PUBLIC_ACCESS=false`）和
原 `data/`，Vite `5173` 代理它，因此本机访问没有人机验证、Code/调度/设置等能力
与开发时一致。公网 `8002` 用 `.env.public` 和公开数据目录。两者不共享数据库或
工作流文件。

**匿名额度不是身份认证**：`scaffold_visitor` 是签名 Cookie，只是额度键；
清 Cookie 后 IP 账本仍生效，但换 IP 仍可重新获得额度。要真正的“每人”，必须加
账号/OAuth 或邀请码。模型 token 有两条记账路径：运行时节点在
`runtime/ai/gateway.py` 通过 `set_usage_sink(record_usage)` 上报；构建时
生成/修复/补节点/副驾由 `ai_buildtime/metering.py` 的 `record_reply_usage()`
上报，二者进入同一个 quota context。runtime 本身仍然不依赖 FastAPI/平台包
（导出 agent 没有 sink，`test_layer_boundary` / 导出 E2E 守住这条边界）。

**admin 与门禁是两件事**：`AUTH_MODE=local` 的 `LOCAL_PRINCIPAL` 拥有全部 scope，
所以公开模式的管理面不能靠 scope 判断，必须用独立的 `X-Admin-Token` 依赖/中间件。
`GATE_BYPASS_TOKEN` 只给 smoke/verify 脚本，绝不下发前端。

**残余风险**（必须如实告知）：

- PoW 不是强 bot 防护，只是 Turnstile 的兜底；
- 公开数据共享，访客可以互相覆盖工作流（删除/导入/导出已关闭）；
- HTTP 工具、AI 生成和试跑仍会消耗 token/写结果库，靠限流、并发上限和每日配额控制；
- Code 节点禁用后，`radar-intel-01` 只能展示，不能运行；
- 限流/nonce 是单进程内存状态，部署脚本固定 1 worker。

完整步骤、备份/回滚/事故处置见 [`../env/deploy.md`](../env/deploy.md)。

---

## 8. 相关文件

| 文件 | 作用 |
|---|---|
| `app/security/auth.py` | token 哈希/校验、scope 模型、模式解析 |
| `app/security/deps.py` | 中间件、依赖、配置 |
| `app/security/gate.py` | Turnstile、PoW、签名 Cookie、门禁中间件 |
| `app/security/policy.py` | 公开演示的 admin-only 路径、图策略、能力清单 |
| `app/security/ratelimit.py` | 每 IP 滑动窗口 + 全局 AI 日限 |
| `app/security/quotas.py` | 匿名 visitor Cookie、SQLite 用量账本、AI/运行/token 每日额度、`quota_exceeded` |
| `app/security/web.py` | body cap、Origin/Host 检查、安全头 |
| `app/api/access.py` | status/challenge/verify 三个公开端点 |
| `app/web.py` | 公开模式同源托管 SPA |
| `app/runtime/egress.py` | 出网策略（**能力层第一个成员**） |
| `app/security/secrets.py` | 凭据提供者（Phase 1 新增） |
| `app/runtime/sandbox.py` + `sandbox_child.py` | 子进程沙盒 |
| `tests/test_security.py` | 对抗性测试 |
| `tests/test_auth.py` | 认证测试 |
| `tests/test_public_access.py` | 门禁/PoW/限流/体积/公开演示策略 |
| `_guardprobe_security.py` | 护栏反向验证脚本 |

> 上述测试/护栏的**条数与结论归 [`status.md`](status.md) §汇总**，此处不抄。

---

## 相关

- 安全的教训（方法论） → [`../lessons/security.md`](../lessons/security.md)
- 导出时的三个安全点 → [`../target/delivery.md`](../target/delivery.md)
- 已知的安全相关缺陷 → [`limitations.md`](limitations.md)
- 沙盒的实测行为 → [`status.md`](status.md) §沙盒
