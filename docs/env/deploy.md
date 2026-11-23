---
title: 公网部署（Windows + Cloudflare Tunnel + 人机验证）
read_this: 要把本机作为公网演示服务器、配置 Turnstile/备用验证、做备份和回滚时
verified: 2026-10-20
---

# 公网部署

目标：本机 Windows 作为服务器。**公网后端只绑 `127.0.0.1:8002`**，通过
Cloudflare Tunnel 出网。访客不需要账号，但要通过人机验证；公开模式是安全演示，
Code 节点只展示不执行。

**本机日常入口是 `127.0.0.1:8001`**（Vite 在 `5173` 代理它）：没有人机验证，
使用原来的 `H:/agent_scaffold/data`，所有已有工作流/agent、Code 节点和本地能力
都照旧。两个后端是独立进程，互不影响。

> **不要做端口转发，不要开 UPnP，不要把后端绑到 `0.0.0.0`。**
> cloudflared 只建立出站连接，家庭 IP 不暴露，路由器也不需要入站规则。

---

## 0. 两个入口

| | 本机入口 | 公网入口 |
|---|---|---|
| 地址 | `http://127.0.0.1:5173` → `127.0.0.1:8001` | quick/named tunnel → `127.0.0.1:8002` |
| 人机验证 | 无 | Turnstile 或 PoW |
| 数据目录 | `H:/agent_scaffold/data` | `H:/agent_scaffold_public_data` |
| 工作流 | 全部 42 个（含 smoke/测试） | 只 seed 选定的 8 个 |
| Code 节点 | 可用 | 只展示不运行 |
| 调度器 / 设置 / 删除 / 导出 | 可用 | 关闭或 admin-only |
| 启动 | `scripts/restart_backend.ps1 -Port 8001` + Vite `5173` | `scripts/deploy/start_public_backend.ps1` |
| 数据影响 | 只写本机数据 | 只写公开数据 |

两边**不共享 SQLite、工作流文件、运行记录、checkpoint、memory 或 exports**。
公开端修改/运行不会改到本机数据，反之亦然。

---

## 1. 公开模式做了什么

| 层 | 行为 |
|---|---|
| Cloudflare 边缘 | TLS、DDoS、可选 WAF/限流；Tunnel 隐藏源站 |
| 后端绑定 | 公网进程 `127.0.0.1:8002`，单 worker，无 `--reload`，**显式 `--no-proxy-headers`**；本机进程仍在 `8001` |
| 为什么不能信任代理头 | uvicorn 默认信任 `127.0.0.1` 的 `X-Forwarded-For`；cloudflared 从回环连入，会把 `request.client` 改成真实访客 IP，`AUTH_MODE=local` 的回环检查随即拒绝所有请求（表现为门禁通过后 401） |
| 门禁 | `/api/*` 需要签名 Cookie；`/api/access/status/challenge/verify` 与健康检查公开 |
| Turnstile | 配置 site/secret 后启用；服务端校验 action 与 hostname |
| 备用验证 | Turnstile 被墙/加载失败时，改用 SHA-256 前导零工作量证明 |
| 限流 | 每 IP 滑动窗口 + AI 全局日限；429 带 `Retry-After` |
| 体积 | `MAX_REQUEST_BYTES`，公开示例 **512 KB**（`524288`） |
| 公开演示 | Code 禁用、自定义工具禁用、调度器关闭、hook 404；设置/调度/删除/导入/导出/metrics 以及 AI `node-to-code`/`code-to-node` admin-only |
| 访客额度 | 每访客 3 次 AI、2 次运行、50k tokens/天；每 IP 10 次 AI、20 次运行、200k tokens/天；全局 5M tokens/天；两个主体同时检查 |
| 数据 | 独立 `DATA_DIR=H:/agent_scaffold_public_data`，不碰本机 `H:/agent_scaffold/data` 里的工作流、运行记录和导出 |

### 旗舰工作流

`radar-intel-01` 有 15 个 Code 节点。公开模式下：

- 节点、端口、配置、连线完整可见；
- 校验只给 `code_node_disabled` **warning**，不把画布变红；
- Run/Save 按钮禁用；后端 `POST /api/runs`、`PUT /api/workflows/{id}` 和
  `PUT /api/workflows/{id}/ai` 返回 `403 demo_disabled`；
- **版本历史仍可查看，但「恢复此版本」禁用**；只读图不能在公网产生新版本或回退；
- 其他不含 Code 的工作流可以正常编辑、运行。

### 1.1 匿名访客额度

公开演示没有账号，所以“每个人”由两个弱身份共同近似：

- `scaffold_visitor`：签名 Cookie，30 天，只作为额度键，不含身份/权限；
- IP（IPv6 按 `/64` 聚合）。

每次 AI 生成、运行和模型 token 都同时记到访客与 IP 两行账本；任一额度用完就
返回 `429 quota_exceeded`，带 `Retry-After`。清 Cookie 后 IP 仍然计数；同一个
NAT 下多人共享 IP 额度，但各自仍有访客额度。账本在
`<DATA_DIR>/quotas.db`，重启不清零。

token 有两条记账路径，写进同一本账：

- **运行时** Agent/LLM 节点：`runtime/ai/gateway.py` 的 usage 回调
  `set_usage_sink(record_usage)`；
- **构建时** AI（生成/修复/补节点/副驾）：`ai_buildtime/metering.py` 的
  `record_reply_usage()`，在每次模型回复后上报。它们刻意不走 `ModelGateway`，
  但仍计入访客/IP/全局 token。
- provider 不返回 usage 时按 **1,000 token/次** 保守计，避免“测不到就当免费”。

额度：

- 单次 AI 请求 `AI_REQUEST_TOKEN_LIMIT`（30k）；
- 每访客 `VISITOR_DAILY_TOKENS`（50k）；
- 每 IP `IP_DAILY_TOKENS`（200k）；
- 全局 `GLOBAL_DAILY_TOKENS`（5M）；
- 每访客运行 `RATE_LIMIT_RUNS_PER_DAY_PER_VISITOR=2`，每 IP 20；
- 全局 AI 请求 `RATE_LIMIT_AI_PER_DAY_GLOBAL=300`；
- `MAX_RUN_TOKENS=50000` 管一次运行；`AI_SELF_TEST=false` 关闭生成后的第二次试跑
  （但副驾带期望产出时仍会试跑做比对）；
- `AI_BUSINESS_RESEARCH=false`：公开副驾不做联网业务调研。

**前端实际调用的是 `/api/runs/adhoc`（画布运行）和 `/api/runs/node-test`（节点测试）**，
两者都按一次运行计数；`/resume` 不再占运行次数，但模型 token 继续计入。确认模式的
副驾一轮会发两次 `/api/ai/copilot/stream`（方案 + 批准执行），各占一次 AI 请求。

超限不总是 `quota_exceeded`：每日账本/单次 token 是 `quota_exceeded`；外层滑动窗口
（每分钟、每 IP 24h、全局 AI 300/日）返回 `rate_limited`。两者都带 `Retry-After`。
运行启动后额度耗尽会表现为运行失败/SSE 错误，而不是 HTTP 429。

公网顶栏会显示今日剩余 AI 次数；它是**进入页面时的快照**，消费后要刷新页面
才会更新。精确数字以 `/api/access/status` 的 `quota` 字段为准。**这不是登录级
隔离**：清 Cookie + 换 IP 仍可重新获得额度，真正的按人隔离需要账号/OAuth。

---

## 2. 前置条件

1. 一个已接入 Cloudflare 的域名（最终 `PUBLIC_ORIGIN`）。
2. Cloudflare Turnstile widget 的 site key + secret（可选；没有就只用 PoW）。
3. 公开数据目录，默认 `H:/agent_scaffold_public_data`。
4. 四个随机密钥（≥32 字符）：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

分别填入 `.env.public` 的 `AUTH_SALT`、`GATE_COOKIE_SECRET`、`ADMIN_TOKEN`、
`GATE_BYPASS_TOKEN`。`ADMIN_TOKEN` 和 `GATE_BYPASS_TOKEN` 只给脚本/owner，
永远不要放进前端。

5. 一个**单独的** LLM API key，并设置消费上限。这个 key 会被通过人机验证的
   访客间接使用。

---

## 3. 配置

```powershell
cd H:\agent_scaffold
Copy-Item .env.public.example .env.public
notepad .env.public
```

关键项：

```text
PUBLIC_ACCESS=true
PUBLIC_DEMO=true
PUBLIC_ORIGIN=https://demo.example.com
TRUSTED_HOSTS=demo.example.com
CORS_ORIGINS=https://demo.example.com
DATA_DIR=H:/agent_scaffold_public_data
AUTH_MODE=local
SERVER_HOST=127.0.0.1
CODE_NODE_ENABLED=false
SCHEDULER_ENABLED=false
AI_SELF_TEST=false
AI_BUSINESS_RESEARCH=false
MAX_CONCURRENT_RUNS=2
MAX_RUN_TOKENS=50000
MAX_RUN_SECONDS=90
MAX_RUN_STEPS=50
MAX_REQUEST_BYTES=524288
RATE_LIMIT_AI_PER_MINUTE=3
RATE_LIMIT_AI_PER_DAY_GLOBAL=300
RATE_LIMIT_AI_PER_DAY_PER_VISITOR=3
RATE_LIMIT_AI_PER_DAY_PER_IP=10
RATE_LIMIT_RUNS_PER_MINUTE=5
RATE_LIMIT_RUNS_PER_DAY_PER_IP=20
RATE_LIMIT_RUNS_PER_DAY_PER_VISITOR=2
VISITOR_DAILY_TOKENS=50000
IP_DAILY_TOKENS=200000
GLOBAL_DAILY_TOKENS=5000000
AI_REQUEST_TOKEN_LIMIT=30000
```

可选的硬成本上限（需要填你所用模型的单价；`0` 表示不启用）：

```text
MAX_RUN_COST=0.10
INPUT_PRICE_PER_MILLION=<provider input price>
OUTPUT_PRICE_PER_MILLION=<provider output price>
```

供应商侧仍必须设置独立 key 的月消费上限和告警；应用层预算不能替代它。

`settings.validate_public()` 会在启动时拒绝以下配置：非回环 bind、缺 gate
secret、Turnstile 只配一半、公开演示却开着 Code/调度器、没有 run 上限、没有
前端构建产物。

---

## 4. 第一次上线

```powershell
cd H:\agent_scaffold

# 1) 备份现有个人数据
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' scripts\deploy\backup_data.py --data-dir H:\agent_scaffold\data

# 2) 构建前端到 frontend/dist
pwsh -NoProfile -File scripts\deploy\build_public.ps1

# 3) 只把选定工作流定义导入公开数据目录（不导入 runs/exports/secrets）
#    deploy.ps1 默认 seed 7 个；a43ea191584e 是从旧公开库迁过来的用户工作流，
#    如需保留，单独指定 --source-data-dir / --files。
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' scripts\deploy\seed_workflows.py `
  --target-data-dir H:\agent_scaffold_public_data `
  --files radar-intel-01.json parallel-launch-review.json business-intel-demo.json `
          wf-security-remediation-expert.json eb209200e0b0.json 44d10d72a849.json g5oib64683q.json
```

> **seed 不复制历史版本。** 它只写当前定义，并生成一条初始 `v1`；本机的
> `workflow_versions`（例如 `parallel-launch-review` 的 v1–v8）不会自动出现在
> 公网。2026-10-21 已把该工作流的 8 个版本手工同步到公开库，其他工作流仍只有
> 初始 v1；重新 seed 后如需要完整历史，必须单独复制版本行，而不是重跑定义导入。

```powershell
# 4) 启动公网后端（8002）
pwsh -NoProfile -File scripts\deploy\start_public_backend.ps1

# 或：后端 + 命名隧道一起启动（重启机器后最省事）
pwsh -NoProfile -File scripts\deploy\start_public_all.ps1
```

本机自查：

```powershell
# 本机入口：无门禁、原数据
Invoke-RestMethod http://127.0.0.1:8001/api/access/status
# required=False, public_demo=False
(Invoke-RestMethod http://127.0.0.1:8001/api/workflows).workflows.Count
# 42

# 公网进程在本机回环上的状态
Invoke-RestMethod http://127.0.0.1:8002/api/health
Invoke-RestMethod http://127.0.0.1:8002/api/access/status
# required=True, public_demo=True
```

浏览器打开 `http://127.0.0.1:5173`（本机入口），应直接进入编辑器，不出现验证页。
公网 `/` 是静态落地页（无门禁），点「免费试用」进入 `/app` 才加载编辑器并出现验证页；
直接分享 `https://<域名>/app` 也可以。

---

## 5. Cloudflare Tunnel

### 5.1 安装/登录（一次性）

```powershell
pwsh -NoProfile -File scripts\deploy\install_cloudflared.ps1
pwsh -NoProfile -File scripts\deploy\install_cloudflared.ps1 -Setup -Hostname demo.example.com
```

`-Setup` 会：

1. 打开浏览器执行 `cloudflared tunnel login`；
2. 创建 `agent-scaffold` tunnel；
3. 把 `%USERPROFILE%\.cloudflared\config.yml` 的 ingress 指向
   `http://127.0.0.1:8002`；
4. 创建 `demo.example.com` 的 DNS route。

### 5.2 启动

```powershell
pwsh -NoProfile -File scripts\deploy\start_cloudflared.ps1
```

> 本机当前部署：域名 `hkhkk.dev`，入口 `https://app.hkhkk.dev`，命名隧道
> `hkhkk`，ingress 指向 `127.0.0.1:8002`；配置在
> `C:\Users\yangh\.cloudflared\config.yml`。`start_cloudflared.ps1` 默认
> `-TunnelName hkhkk`，另一台机器用 `-TunnelName` 覆盖。
>
> 本机 `config.yml` 固定 `protocol: http2`：QUIC 走 UDP，在这台机器的
> Clash TUN 路径上出现过间歇性 `failed to dial ... quic`，表现为偶尔 530；
> TCP/7844 的 HTTP/2 在 precheck 和实测里都稳定。配置同时使用
> `ha-connections: 4`、`edge-ip-version: "4"`、`retries: 10`，并在
> 2026-10-21 一次 18 秒的 502 掉线后启用。若换网络，QUIC 也可以。

Cloudflare 侧建议：

- DNS 记录保持代理（橙云）；
- SSL/TLS 用 Full 或 Full (strict)，开启 Always Use HTTPS；
- 不要用 Flexible；
- 关闭 Rocket Loader；Bot Fight Mode 的全局挑战可能拦截 SSE/API，建议关闭；
- 如需 WAF 限流，只针对 `/api/access/verify`、`/api/ai/*`。

### 5.3 没有域名时先冒烟

```powershell
pwsh -NoProfile -File scripts\deploy\quick_tunnel.ps1 -Background -UpdateEnv
pwsh -NoProfile -File scripts\deploy\start_public_backend.ps1
```

脚本打印 `https://....trycloudflare.com`，并把 `PUBLIC_ORIGIN`/`CORS_ORIGINS`/
`TRUSTED_HOSTS` 改成该 URL。Turnstile 绑定的是正式域名，quick tunnel 下会走 PoW。
临时 URL 每次启动都会变，只适合验证，不适合长期演示。

---

## 6. Turnstile

1. Cloudflare Dashboard → Turnstile → Add widget，hostname 填 `demo.example.com`；
2. 把 site key / secret key 写入 `.env.public`；
3. 重启后端；
4. 浏览器应看到 Turnstile；如果脚本被阻断，8 秒后页面出现「使用备用验证」；
5. 服务端校验 `success`、`action == scaffold-gate`、`hostname == PUBLIC_ORIGIN host`。

Turnstile 加载失败不是故障：PoW 兜底仍然可用，只是防护强度略低。

---

## 7. 验证

```powershell
# 本机上的公网进程（HTTP 下 Secure Cookie 不会回传，cookie 一项会失败，
# 这是预期；完整检查请用下面的 HTTPS 地址）
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' scripts\deploy\verify_public.py `
  --base http://127.0.0.1:8002 `
  --admin-token <ADMIN_TOKEN> `
  --gate-bypass <GATE_BYPASS_TOKEN>

# 通过命名隧道
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' scripts\deploy\verify_public.py `
  --base https://demo.example.com
```

检查项包括：健康检查、status、未验证 403、PoW 验证、Cookie 解锁、设置/指标
403、docs 404、SPA、安全头、静态资源缓存、坏 Origin 403、请求体 413、
admin/bypass。

浏览器再手工确认：

- 打开 `/` 看到落地页；点 CTA 到 `/app`，清 Cookie 后出现门禁；验证后 App 加载；
- 打开 `radar-intel-01`：42 节点可见，Run/Save 禁用；
- 打开一个无 Code 的工作流：可以运行，SSE 事件流正常；
- 删除 gate cookie 后点保存：覆盖层出现，画布不丢；
- 控制台 `fetch('/api/workflows')` 无 cookie → `403 gate_required`。

---

## 8. 开机自启

有两条路，按机器是否自动登录选择：

### A. 登录后启动（简单）

在「任务计划程序」创建两个任务，触发器选「登录时」，操作分别是：

```text
pwsh.exe -NoProfile -ExecutionPolicy Bypass -File H:\agent_scaffold\scripts\deploy\start_public_backend.ps1
pwsh.exe -NoProfile -ExecutionPolicy Bypass -File H:\agent_scaffold\scripts\deploy\start_cloudflared.ps1
```

设置「如果任务失败，每隔 1 分钟重启」。启用自动登录后，重启即可恢复。

### B. 无人登录也要跑（服务）

需要管理员权限，且 cloudflared 的配置文件要放到服务账户能看到的位置。
推荐用 NSSM 把 `start_public_backend.ps1` 和 `cloudflared tunnel run` 包成
Windows 服务；不推荐把 `.env.public` 放在用户目录以外的可读路径。

无论哪种方式，都在「电源选项」里关闭睡眠/休眠，或设置插电时永不睡眠。

### C. 公网看门狗（TUN 不稳定时）

这台机器的 TUN/代理路径会间歇性把 cloudflared 的两条 edge 连接同时掐掉；
进程还在，但 Cloudflare 返回 **1033/530**。看门狗每 20 秒检查一次公网健康，
连续 2 次失败才重启（本地后端也挂了会先重启后端），避免单次抖动误重启：

```powershell
Start-Process pwsh.exe -WindowStyle Hidden -ArgumentList @(
  '-NoProfile','-ExecutionPolicy','Bypass',
  '-File','H:\agent_scaffold\scripts\deploy\watch_public.ps1'
)
```

日志：`data/logs/public-watch.log`。它只是缩短恢复时间，**不能消除 TUN 断线**；
长期稳定仍建议 VPS/frp 或稳定代理。需要开机自启时，把上面的命令做成登录任务。

---

## 9. 备份、恢复、回滚

```powershell
# 备份公开数据
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' scripts\deploy\backup_data.py `
  --data-dir H:\agent_scaffold_public_data

# 恢复：先停后端，把备份目录里的 *.db 和 workflows/ 复制回去，再启动
```

备份包含 `scaffold.db`、`checkpoints.db`、`memory.db`、`results.db`、
`quotas.db` 和 `workflows/*.json`，每份带 `manifest.json`（大小 + sha256）。
默认保留 7 份。

回滚：

1. `Stop-Process` 停掉 8002 上的公网后端（本机 8001 继续运行）；
2. 恢复上一份公开数据备份；
3. 如隧道/域名有问题，停 cloudflared 并删除 DNS route；
4. 本机入口始终用 `PUBLIC_ACCESS=false` 的 `.env` 和原数据，不受影响。

---

## 10. 监控与事故处置

- Cloudflare Dashboard：Tunnel 状态、请求量、4xx/5xx、WAF 命中；
- `data/logs/public-backend.*.log`、`data/logs/cloudflared.*.log`；
- `GET /api/metrics` 需要 `X-Admin-Token`；
- 429 增多：先看 `/api/access/verify` 和 `/api/ai/*`，必要时提高 PoW 难度或降低
  `RATE_LIMIT_AI_PER_DAY_GLOBAL`；
- 怀疑被滥用：停 cloudflared → 轮换 `GATE_COOKIE_SECRET`（所有人重新验证）、
  `ADMIN_TOKEN`、`GATE_BYPASS_TOKEN`、Turnstile key → 检查日志/备份 → 重新上线。

---

## 11. 已知残余风险

- **PoW 不是强 bot 防护**，它是 Turnstile 的兜底；主要防线是 Turnstile、Cloudflare
  边缘、限流和 AI 配额。
- **公开数据共享**：访客可以互相覆盖工作流。删除/导入/导出已 admin-only，但
  编辑仍共享；靠独立数据目录、数量上限、版本历史和每日备份降低损失。
- **本机执行**：Code 已禁用；HTTP 工具仍受 SSRF 策略约束；AI 生成/试跑会消耗
  token 并写结果库，靠配额和 `MAX_CONCURRENT_RUNS` 控制。
- **单 worker**：限流/nonce 不跨进程；不要加 `--workers >1`。
- **家庭网络**：断电、重启、ISP 抖动都会导致离线；没有 100% 可用性承诺。
- **Cloudflare 国内可达性**：Turnstile/Cloudflare 可能慢或被阻断；PoW 兜底始终
  可用。长期面向国内可换国内 VPS/frp + 国内验证码。

---

## 12. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 启动报 `PUBLIC_ACCESS requires ...` | `.env.public` 缺项；按报错补齐 |
| `403 gate_required` | 没有 gate cookie；打开首页完成验证 |
| `403 origin_forbidden` | `PUBLIC_ORIGIN` 与实际访问域名不一致；改 `.env.public` 后重启 |
| `403 admin_required` | 该接口需要 `X-Admin-Token`；前端不会发送它 |
| `429 rate_limited` | 等 `Retry-After`；或调整限流设置 |
| Turnstile 一直转圈 | 国内网络被阻断；点「使用备用验证」 |
| `502` / 隧道 1033 | cloudflared 没连上 8002；看 `cloudflared.err.log` |
| 530 间歇出现 / `failed to dial ... quic` | 本机 TUN 下 QUIC 不稳；在 `config.yml` 加 `protocol: http2` 并重启隧道（当前部署已用） |
| 本机打开也要求验证 | Vite 代理到了 8002；确认 `BACKEND_PORT` 未设置，默认代理 8001 |
| 门禁通过后 API 401 | uvicorn 信任了 cloudflared 的代理头；启动脚本必须带 `--no-proxy-headers` |
| quick tunnel 530 / 7844 超时 | Clash 的 TUN 服务没启动：管理员 PowerShell `Set-Service clash_verge_service -StartupType Automatic; Start-Service clash_verge_service`，再开 TUN；`Test-NetConnection region1.v2.argotunnel.com -Port 7844` 应为 True |
| 控制台 CSP 拦字体 | `font-src` 已有 `'self' data:`；若改 CSP 不要删掉 `data:` |
| 打开旧图缺 Code 节点 | catalogue 必须仍返回 disabled 的 code spec；不要从 `/api/node-types` 过滤掉它 |
| 修改后前端没更新 | 重新 `build_public.ps1`，并在 Cloudflare purge cache |
