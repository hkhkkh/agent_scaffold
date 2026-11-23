---
title: 环境红线
read_this: 跑任何命令之前
---

# 环境红线

> 违反这些会**浪费时间**（不是报错，是"看起来在工作其实没有"）。

---

## 硬性要求

| 项 | 必须 | 为什么 |
|---|---|---|
| **Python** | `d:\AI_LLM_Project\venv_d\Scripts\python.exe` | **绝不用系统 Python** —— 依赖只装在这个 venv 里 |
| **pip** | `d:\AI_LLM_Project\venv_d\Scripts\pip.exe` | 同上 |
| **终端** | **pwsh 7.6.6** | powershell 5.1 按 ANSI 解码 UTF-8，中文会乱码 |
| **node / npm** | `F:\Node.s\`（`npm.cmd` 需全路径） | 不在 PATH 里 |
| **端口** | 后端 **8001**，前端 **5173** | 8000 是僵尸 socket，见 [`ports.md`](ports.md) |

---

## 三条最容易违反的

### 1. 长任务不要用终端等

**同一条命令会被下一条终端命令杀掉**（表现为「跑到一半就停」）——
长任务必须**完全脱离终端**（`Start-Process ... -WindowStyle Hidden`），
之后只读日志文件，**绝不在它运行期间用终端轮询**。

> 机制、写法与实测 → [`../lessons/process.md`](../lessons/process.md) §不要轮询终端

### 2. 前端必须同步才能生效

**改的是 `frontend/`，跑的是 `%TEMP%\agent_web`。**
不跑 `sync_src.ps1` 就看不到改动 —— 而且**看起来像"改了没效果"**。

→ [`frontend-temp.md`](frontend-temp.md)

### 3. 改源码前先检查 Git 状态

本仓库使用 Git。先运行 `git status --short`，辨认并保留已有改动；
不要用 `reset` / `checkout` 覆盖工作区。验证脚本若会临时变异源码，仍须使用
sidecar 备份和恢复流程（照 `backend/_guardprobe_capability.py` 的做法）。

---

## 凭据

已在 `h:\agent_scaffold\.env`（**已 gitignore，不要提交**）：

```
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=...
LLM_MODEL=deepseek-flash
```

**已验证可用**（实测真实调用返回）。模板在 `.env.example`。

> [2026-10-10] 模型名是 `deepseek-flash`，不是 `deepseek-chat`。
> `deepseek-chat` / `deepseek-reasoner` 已于 2026-07-24 退役，只剩别名，
> 而**别名仍然返回 200**——响应里报的是它真正服务的 `deepseek-flash`，
> 所以旧名能一直用下去、同时掩盖实际跑的是哪个模型。实测三个名字都通：
> `deepseek-chat`→`deepseek-flash`、`deepseek-flash`→`deepseek-flash`、
> `deepseek-v4-pro`→`deepseek-v4-pro`。别再改回旧名。

> `conftest.py` 强制把 `LLM_API_KEY` 置空 → 测试永远走 mock 模型，
> **不会意外产生真实调用或花费**。

---

## 中文输出的坑

**控制台其实能正常显示中文**（pwsh 7.6.6，`[Console]::OutputEncoding` = gb2312）——
实测两个都正常：

```powershell
Write-Host "PWSH 中文：开始 / 大模型 / 提示词"
python -c "print('PYTHON 中文：开始 / 大模型 / 提示词')"
```

但**复杂/长输出仍然可能被吞**（`Select-Object -Last N` 有时什么都不返回）。
所以：

| 场景 | 做法 |
|---|---|
| 要看长输出 | 写文件再 `read_file` |
| 要看后端返回的中文 JSON | 用 Python 写文件再读（PowerShell 解码会乱码） |
| 要在终端看进度 | 用 `Write-Host`（**不是** `Write-Output`，后者被管道缓冲到结束才出现） |

> 一段历史：`restart_backend.ps1` 曾经把结果只写文件，理由是脚本头注释里那句
> 「the console here mangles UTF-8」。**那个前提是错的**，代价是脚本完全看不到进度。
> 教训：**环境限制的"印象"要用一条命令验证再写进注释** ——
> 写错的前提会自我复制成更宽的规则。
> → [`../lessons/process.md`](../lessons/process.md) §未验证的前提会自我复制

---

## 相关

- 启动 / 测试 / 冒烟命令 → [`run.md`](run.md)
- 工具使用的细节坑 → [`tooling.md`](tooling.md)
- 端口 → [`ports.md`](ports.md)
