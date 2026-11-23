---
title: 前端为什么跑在 %TEMP%\agent_web（最大的坑）
read_this: 改前端 / 装前端依赖 / 前端改动不生效时
---

# 前端为什么跑在 `%TEMP%\agent_web`

## 一句话

**`h:\agent_scaffold\frontend\node_modules` 处于 Windows 的
*delete-pending* 状态** —— 目录存在但任何操作都失败。

所以前端**实际运行在 `%TEMP%\agent_web`**（源码从这里同步过去）。

---

## 症状与实测特征

**症状**：`pnpm install` 每次失败于

```
ERR_PNPM_EPERM: operation not permitted, rename '<pkg>_tmp_x' -> '<pkg>'
```

且**失败包每次都不同**（`tldts`、`entities`…）。

**原因**：pnpm 把包装进 `<name>_tmp_<id>` 再改名到位，而改名目标「已存在」——
正是这个卡住的目录造成的。失败包随机，取决于哪个包先撞上并发顺序。

**逐项实测**（不是猜测）：

| 检查 | 结果 |
|---|---|
| `Test-Path node_modules` | `True` |
| `os.lstat` 模式 | `0o40777`（正常目录，**非**符号链接） |
| `os.listdir` | [ ] `PermissionError [WinError 5] 拒绝访问` |
| `os.rename` | [ ] `PermissionError [WinError 5]` |
| `rmdir /s /q` | [ ] 失败 |
| `robocopy /MIR` 空目录镜像 | [ ] 失败 |
| **持有句柄的进程** | **`Code.exe`（VS Code 文件监视器），不是 `node.exe`** |

---

## 当前解法（照做）

### 首次安装

```powershell
pwsh -NoProfile -File h:\agent_scaffold\scripts\build_frontend.ps1
```

该脚本：复制源码 + 配置到 `%TEMP%\agent_web` → **`npm install`** → 跑 `tsc --noEmit`。

> **为什么用 npm 而不是 pnpm**：npm 直接解压到目标位置，
> 不走「整包临时目录改名」那条路，因此绕开了 delete-pending 的冲突。

结果在 `%TEMP%\agent_web_report.txt`，类型错误在 `%TEMP%\agent_typecheck.log`。

### 改完源码后同步

```powershell
pwsh -NoProfile -File h:\agent_scaffold\scripts\sync_src.ps1
```

Vite 会自动热更新。

### 起 dev server

命令见 [`run.md`](run.md) §前端。代理**默认就打到 8001**，不需要额外设 `BACKEND_PORT`；
只有后端换端口时才要设（见 [`ports.md`](ports.md)）。

---

## 注意： 不要做的事

| 不要 | 为什么 |
|---|---|
| **不要再试图删 `frontend/node_modules`** | 试过 4 种方式（`Remove-Item` / `rmdir` / `robocopy` / rename），全部失败 |
| **不要在 `frontend/` 里跑 `pnpm install`** | 必然失败，且会浪费一次完整的安装时间 |
| **不要以为改了 `frontend/` 就生效了** | 跑的是 `%TEMP%\agent_web`，必须 `sync_src.ps1` |

---

## 彻底修复

**重启 VS Code**（或「重载窗口」）即可释放句柄，
之后在 `frontend/` 里正常 `pnpm install && pnpm dev` 就能用。

在那之前走 `%TEMP%` 那条路。

---

## 相关

- 前端命令 → [`run.md`](run.md)
- 环境红线 → [`redlines.md`](redlines.md)
