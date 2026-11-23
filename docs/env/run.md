---
title: 启动、测试、冒烟
read_this: 要跑任何命令之前
owner_of: 所有命令的唯一归属地（别处不要再抄命令）
---

# 怎么跑起来

> **本文件是所有命令的唯一归属地。** 别的地方引用时写「见 `env/run.md`」，不要抄。
>
> 公网部署（Cloudflare Tunnel、人机验证、`.env.public`）不在这里，见
> [`deploy.md`](deploy.md)。公网后端是**另一个进程**，固定 `8002`；本机后端 `8001`
> 用正常 `.env` 和原 `data/`，不经过门禁。

---

## 后端

```powershell
# 启动（推荐：用脚本，它会先释放端口、轮询就绪、再自检两处本地化）
pwsh -NoProfile -File h:\agent_scaffold\scripts\restart_backend.ps1 -Port 8001

# 或手动（端口必须是 8001，原因见 env/ports.md）
Start-Process -FilePath 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' `
  -ArgumentList '-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8001' `
  -WorkingDirectory 'h:\agent_scaffold\backend' -WindowStyle Hidden -PassThru
```

> 注意： **`cd` 不持久化** —— 每条命令都要 `Set-Location`，或用 `--app-dir` 这类显式参数。
> 见 [`tooling.md`](tooling.md)。

## 测试

```powershell
Set-Location h:\agent_scaffold\backend
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' -m pytest tests/ --tb=short
```

**全部通过。** 期望几个 → [`../current/status.md`](../current/status.md)（[重要] 数字的唯一归属地，**不要抄在这里**）

> 注意： **不要加 `-q`。**
> `backend/pytest.ini` 里已有 `addopts = -q`，命令行再加一个会变成 `-qq`，
> **汇总行 `N passed in Xs` 会被吞掉**（只剩进度点，看起来像"没有结果"）。
> 用 `--tb=short` 或什么都不加。
>
> 这个坑真实浪费过时间：我一度以为「pytest 不打印结果」。

跑单个文件 / 单个测试：

```powershell
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' -m pytest tests/test_run_retention.py --tb=short
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' -m pytest tests/test_runtime.py::TestLinearRun -v
```

## i18n 校验

```powershell
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' -m scripts.check_i18n
# 期望 RESULT=PASS（数字见 ../current/status.md）
```

## LLM 连通性

```powershell
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' h:\agent_scaffold\scripts\check_llm.py
# 期望 SUCCESS
```

## 端到端冒烟

```powershell
Set-Location h:\agent_scaffold
# 不带 AI（快，不花钱）
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' scripts\smoke.py --base http://127.0.0.1:8001
# 全绿即通过（数字见 ../current/status.md）

# 带真实模型（慢，花 token）
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' scripts\smoke.py --ai --base http://127.0.0.1:8001
# 全绿即通过（数字见 ../current/status.md）
```

> 注意： `smoke.py` 的默认 `BASE` 已经是 **8001**（`scripts/smoke.py:30`），
> `--base` 只在真要换端口时才需要带。上面那两行显式写着只是为了照抄方便。
> 8000→8001 的来龙去脉见 [`ports.md`](ports.md)。

## 演示：Excel → 对话 → 导出 → 独立聊天（2026-11-03）

```powershell
Set-Location h:\agent_scaffold

# 1) 生成样例表格 + 上传工作区 + 保存并运行演示工作流 + 导出独立 agent
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' scripts\demo\excel_chat_demo.py
#    --skip-run / --skip-export 可跳过对应步骤
#    --message "..." 换问题；--base http://127.0.0.1:8001 换后端

# 2) 跑导出的 agent（干净目录，不需要画布/平台数据库）
$pkg = 'h:\agent_scaffold\data\exports\excel-chat-demo'
#    LLM 配置：可以在包里建 config\secrets.env（KEY=VALUE），或先设环境变量
Set-Location $pkg
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' main.py doctor       # 能力包 + 依赖体检
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' main.py selfcheck    # 运行时/定义哈希
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' main.py serve --http --port 8080 --token ''
#    浏览器打开 http://127.0.0.1:8080 —— 内置聊天页：新建会话、上传表格、提问
#    也可以直接调 API：POST /v1/files、POST /v1/sessions、POST /v1/sessions/{id}/messages?stream=1
```

> 注意： **重导出前先停掉旧的 `serve --http` 进程**。导出器会先删目标目录，
> 旧进程占着 `data/sessions.db` 时删除会中途失败（WinError 32），留下半成品。
> 见 [`../current/limitations.md`](../current/limitations.md) §1 第 11 条。

## 反向验证（护栏探针）

```powershell
Set-Location h:\agent_scaffold\backend

# 全跑（约 6 分钟）—— 末行应是 RESULT=PASS（数字见 ../current/status.md）
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' _guardprobe_capability.py

# 只跑某个测试文件的用例（迭代时用）
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' _guardprobe_capability.py test_run_retention.py

# 安全护栏
& 'd:\AI_LLM_Project\venv_d\Scripts\python.exe' _guardprobe_security.py
```

**跑完必须确认 `SOURCES_RESTORED=True`。**

> 注意： **被中断后第一步是恢复，不是跑测试**：
> [`../lessons/process.md`](../lessons/process.md) §探针被中断

## 前端

**源码在 `frontend/`，但实际跑在 `%TEMP%\agent_web`**
（原因见 [`frontend-temp.md`](frontend-temp.md)）。

```powershell
# 首次安装（装到 %TEMP%，并用 npm 而不是 pnpm）
pwsh -NoProfile -File h:\agent_scaffold\scripts\build_frontend.ps1
# 结果在 $env:TEMP\agent_web_report.txt，类型错误在 $env:TEMP\agent_typecheck.log

# 改完源码必须同步（Vite 会自动热更新）
pwsh -NoProfile -File h:\agent_scaffold\scripts\sync_src.ps1

# 起 dev server（代理默认打到 8001；后端换端口时才需要设 BACKEND_PORT，见 ports.md）
Set-Location "$env:TEMP\agent_web"
npm run dev -- --host 127.0.0.1
```

打开 <http://127.0.0.1:5173> —— 界面是中文（语言记在 localStorage，顶栏可切换）。

**类型检查与单元测试**（都在 dev 目录里、用 node 直接跑）：

```powershell
Set-Location $env:TEMP\agent_web
& 'F:\Node.s\node.exe' '.\node_modules\typescript\bin\tsc' --noEmit    # 无输出即通过
& 'F:\Node.s\node.exe' '.\node_modules\vitest\vitest.mjs' run          # 全绿即通过（数字见 ../current/status.md）
```

> 注意： **不要用 `.cmd` 包装器**（`npm.cmd` / `tsc.cmd`）—— 它们经 `cmd.exe` 转发，
> 在这个环境里输出经常丢失。用 `node.exe` 直接跑。见 [`tooling.md`](tooling.md)。

---

## 改完代码后按顺序跑

1. 后端测试
2. i18n 校验
3. 反向验证（只在新增守卫时才需要全跑）
4. 重启后端加载新代码
5. 端到端冒烟（含真实模型）

命令都在上面各小节，期望数字见 [`../current/status.md`](../current/status.md)。

---

## 检查服务是否活着

```powershell
# 端口
netstat -ano | Select-String 'LISTENING' | Select-String ':8001\s|:5173\s'

# Vite 代理打到后端了吗
try { (Invoke-WebRequest 'http://127.0.0.1:5173/api/node-types' -TimeoutSec 5 -UseBasicParsing).StatusCode } catch { "proxy down" }
```

---

## 脚本清单

| 脚本 | 作用 |
|---|---|
| `scripts/restart_backend.ps1` | 释放端口并重启后端（`-Port` 可配，默认 8001；启动后自检两处本地化） |
| `scripts/build_frontend.ps1` | 在 `%TEMP%` 装依赖 + 类型检查 |
| `scripts/sync_src.ps1` | 同步源码到 dev 目录，触发 Vite 热更新 |
| `scripts/dev_run.ps1` | 起前后端并探测（端口按 `-Port` → `$env:BACKEND_PORT` → 8001 解析；2026-10-08 已修） |
| `scripts/check_llm.py` | 单独验证 LLM 连通性 |
| `scripts/smoke.py` | 端到端冒烟（默认 `BASE` 已是 8001） |
| `scripts/deploy/` | 公网构建 / 启动 / 隧道 / 备份 / 验证；步骤见 [`deploy.md`](deploy.md) |
| `backend/scripts/check_i18n.py` | i18n 一致性校验 |
| `backend/_guardprobe_capability.py` | 破坏各域能力层护栏（迁移 / 回收 / 节点策略 / 运行存储 / checkpoint / 遮蔽 / 重启恢复 / 调度与事件触发 / 导出依赖 / SSE 线格式 / 级联删除；数量见 [`../current/status.md`](../current/status.md)） |
| `backend/_guardprobe_security.py` | 破坏安全护栏（数量见 [`../current/status.md`](../current/status.md)） |
| `backend/_restore_guardprobe.py` | **中断恢复** —— 对比 sidecar 备份与真实源码 |

---

## 相关

- 环境红线（不能做什么）→ [`redlines.md`](redlines.md)
- 端口为什么是 8001 → [`ports.md`](ports.md)
- 前端为什么在 `%TEMP%` → [`frontend-temp.md`](frontend-temp.md)
- 命令卡住 / 输出被吞 → [`tooling.md`](tooling.md)
- 当前期望数值 → [`../current/status.md`](../current/status.md)
