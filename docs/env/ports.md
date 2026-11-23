---
title: 为什么端口是 8001（8000 被僵尸 socket 占用）
read_this: 后端绑定失败 / 启动前端时
---

# 8000 端口被僵尸 socket 占用

## 现象

后端绑定 8000 时报 `[Errno 10048] WSAEADDRINUSE`，但：

```
netstat -ano | findstr :8000   →  LISTENING  20368
tasklist /FI "PID eq 20368"    →  没有运行的任务匹配指定标准
taskkill /F /PID 20368         →  错误: 没有找到进程（退出码 128）
```

**即：进程已不存在，端口仍被占用。**

---

## 原因

Windows 唤醒 NAT（**winnat**）保留端口的已知问题。
端口在某个时刻被一个进程占用，进程退出后 winnat 没有释放保留。

---

## 当前解法（照做就行）

**后端跑 8001。** `vite.config.ts` 的代理已经改成读变量，**默认值也是 8001**：

```ts
target: `http://127.0.0.1:${process.env.BACKEND_PORT ?? '8001'}`
```

> 默认值曾经是 `8000`，而所有脚本用 `8001` —— 于是 dev server 代理到一个没人监听的端口，
> 每个调用都回 500。2026-10-08 改为 `8001`，与 `dev_run.ps1` / `restart_backend.ps1` 一致。

因此**正常启动前端不需要额外设 `BACKEND_PORT`** —— 不设时代理也默认打到 8001；
只有后端换了端口（或要覆盖）时才 `$env:BACKEND_PORT=<端口>`。启动命令见 [`run.md`](run.md) §前端。

> **公网进程固定用 8002**，不占用 8001。`start_public_backend.ps1`、`quick_tunnel.ps1`
> 和 `install_cloudflared.ps1` 的默认端口都是 8002；本机 5173 的代理默认仍是 8001。
> 不要把公网后端改回 8001，否则本机入口会重新经过门禁并读到公开数据。

---

## 彻底恢复（需要管理员权限，或重启机器）

```powershell
net stop winnat
net start winnat
```

> 注意： 这需要**管理员权限**。不要在终端里尝试提权
> （会阻塞在一个无法回答的 UAC 提示上）—— 需要时告诉用户自己执行。

---

## 相关

- 启动命令 → [`run.md`](run.md)
- `restart_backend.ps1` / `dev_run.ps1` / `smoke.py` 的端口修记录（2026-10-08，现都走
  `-Port` → `$env:BACKEND_PORT` → `8001`）→ [`../log/phase-2-11-small-defects.md`](../log/phase-2-11-small-defects.md) ·
  [`../code/known-defects.md`](../code/known-defects.md)
