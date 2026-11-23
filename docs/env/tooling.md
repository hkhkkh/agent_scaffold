---
title: 工具使用坑（这个环境特有）
read_this: 命令卡住 / 输出被吞 / 不知道怎么读结果时
---

# 工具使用坑

> 这些都是**操作层面**的坑 —— 不是代码 bug，是「怎么用这个环境」。
> 每条都真实浪费过时间。

---

## 1. [重要] 长任务会被下一条终端命令杀掉

**同一条命令会被下一条命令杀掉**（表现为「安装跑到一半就停」）——
长任务必须**完全脱离终端**（`Start-Process ... -WindowStyle Hidden`），
之后只读日志文件，**绝不在它运行期间用终端轮询**。

> 机制、正确写法、`-NoNewWindow`+Ctrl+C 的坑，以及「重定向到文件再看对 Python 脚本不成立」
> → [`../lessons/process.md`](../lessons/process.md) §不要轮询终端

---

## 2. [重要] `Start-Process` 启动的服务会占住脚本的 stdout

**症状**：`pwsh -File scripts\restart_backend.ps1 -Port 8001 | Select-Object -Last 8`
**永不返回**。脚本 10 秒就跑完了，shell 一直等。

**根因**：Windows 子进程**继承句柄**。后台服务继承了脚本的 stdout，
而管道要等**所有**写端关闭才能读到 EOF。服务活着 → 管道永不关闭。

> 注意： `-RedirectStandardOutput` / `-RedirectStandardError` **不能解决** ——
> 它重定向的是流对象，不是句柄继承。实测仍挂起。

**四种组合的实测结果**：

| 启动方式 | 管道返回 | 进程存活 |
|---|---|---|
| `Start-Process` + 双重重定向 | [ ] 挂起 | [x] |
| `Win32_Process.Create` + 启动文件 | [x] | [ ] 脚本退出即死 |
| 同上 + `start /b` | [x] | [ ] `/b` 共用 cmd 的控制台，一起被杀 |
| **`Start-Process cmd.exe` + 让 cmd 自己 `>`** | **[x] 1s** | **[x]** |

**最终写法**（`scripts/restart_backend.ps1`）：

```powershell
$child = Start-Process -FilePath 'cmd.exe' `
    -ArgumentList '/c', "`"$launcher`"" `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru
```

两个配套细节：

1. **命令行走启动文件，不写内联字符串**。`cmd /c` 对「以引号开头且含多对引号」的命令
   会做去引号推断，内联版本直接报 `此时不应有 >`（cmd 自己的语法错误）
2. **启动文件用 ASCII，绝不用 UTF-8**。`cmd.exe` 会把 UTF-8 BOM
   当成第一条命令的一部分然后失败

**顺带修的**：把「等固定 7 秒」换成**轮询端口**。固定等待两个方向都错 ——
服务快时白等，服务慢时给出一个假的「连接被拒绝」。

> **推广**：凡是「后台启动 + 立刻自检」的脚本，都要问一句
> **「这个子进程还拿着我的 stdout 吗」**。

---

## 3. `Write-Output` 的进度不会实时出现

`Write-Output` 走 success stream，调用方的 `| Select-Object` 会把它
**缓冲到管道关闭**，所以进度只会在结束时出现 —— 也就是它最没用的时刻。

**要实时显示进度用 `Write-Host`**（直接写控制台，绕过管道）。

文件仍然写：`grep` 比翻终端快，而且脱离终端运行时没有窗口可看。

---

## 4. 终端 cwd 不持久化

`cd` / `Set-Location` 在**下一条命令**里会丢。

**做法**：每条命令都要 `Set-Location`，或用 `--app-dir` 这类显式参数。

---

## 5. 长 / 中文输出会被吞

`Select-Object -Last N` 有时什么都不返回。

**可靠做法**：输出到文件再 `read_file`。

另外：`read_file` 读 PowerShell `Out-File` 写的文件**可能显示为空**（即使文件确实有内容）。
用 Python 读：

```powershell
python -c "print(open(r'path','rb').read().decode('utf-8','replace'))"
```

> 注意：**控制台本身能显示中文**（见 [`redlines.md`](redlines.md)）——
> 这个坑是"长输出被吞"，不是"中文乱码"。

---

## 6. `python -c` 的 here-string：pwsh 7 下没问题

旧笔记说这样「会失败」，其实在 **pwsh 7 下能正常跑**：

```powershell
python -c @'
print("hi")
'@
```

实测（pwsh 7.6.6）：打印 `hi`、退出 0。**只有 Windows PowerShell 5.1** 会剥掉引号
报 `NameError` —— 而本环境不用 5.1（见 [`redlines.md`](redlines.md)）。

---

## 7. VS Code 的 grep 会返回**已删文件**的缓存结果

用终端 `Remove-Item` 删掉一个文件后，`grep_search` 仍然搜到它并给出行号。

**实际文件已经没了。** 搜索索引没跟上终端的删除。

> 这次假警报差点让我去修一个不存在的问题（我以为"有两个 alembic head"，严重问题）。

**规则**：**终端删除文件后，用文件系统核实**（`Get-ChildItem`），
不要信编辑器搜索的缓存结果。

---

## 8. 环境变量会泄漏到同一终端会话的后续命令

我用 `$env:DATA_DIR = <临时目录>` 给 alembic 指定临时库，
**之后**运行 `restart_backend.ps1` 时后端继承了这个变量 ——
日志里 `Data directory: F:\UserData\Temp\scaffold_mig_test`。

**那次「验证」测的是临时库，不是真实库**，而日志看起来完全正常。

> **规则**：用环境变量给某个命令指定临时配置后，**显式清掉**
> （`Remove-Item Env:\DATA_DIR`），否则它会静默影响后面所有命令。
> 尤其影响 `restart_backend.ps1`，因为它启动的进程继承整个环境。

---

## 9. `.cmd` 包装器会吞掉输出

`npm.cmd` / `tsc.cmd` 经 `cmd.exe` 转发，输出经常丢失或不回显。

**改用 node 直接跑**：

```powershell
& 'F:\Node.s\node.exe' '.\node_modules\typescript\bin\tsc' --noEmit
& 'F:\Node.s\node.exe' '.\node_modules\vitest\vitest.mjs' run
```

---

## 10. [重要] `git push` 会失败，但 `gh api` 可以

**症状**：推送必崩，但同一时刻 `gh api` 一切正常。

> **2026-10-09 复测：仍然如此。** `git ls-remote origin` 在 21 秒后报
> `Failed to connect to github.com port 443: Could not connect to server`。
> 这不是快照过期问题 —— 下面这套 `gh api` 走法**当前仍是唯一可用的推送方式**。

```
fatal: unable to access 'https://github.com/hkhkkh/<repo>.git/':
Recv failure: Connection was reset          ← 或等 21 秒后 Could not connect
```

**同一时刻的通道探测**（2026-09-18 实测）：

| 主机 | 端口 | TCP 通 | 说明 |
|---|---|---|---|
| `github.com` | 443 | **True** | TCP 握手成功，**TLS 阶段被重置** → 按 SNI 阻断 |
| `api.github.com` | 443 | True | 可用 |
| `ssh.github.com` | 443 | True | 可用（未验证能否用它推送） |

[重要] **「TCP 能连」不等于「能用」。** `Test-NetConnection github.com -Port 443` 返回
`True`，而 `git push` 四次全崩 —— 因为握手成功之后才按主机名切断。
**别因为端口探测通过就以为网络没问题。**

[重要] 也别把它当成认证问题。「Connection was reset」和「Could not connect」都是
**传输层**的措辞；真正的认证失败会说 `Authentication failed` 或 `403`。
先看措辞，能省一轮排查。

### 解法：走 Git Data API

`gh api` 打的是 `api.github.com`，不受影响。API 没有 "push" 动词，所以要**手工做一次
push 做的事**：

```
1. 读远端 head 及其 tree      GET   /repos/{r}/git/ref/heads/main
                              GET   /repos/{r}/git/commits/{sha}
2. 每个新增/改动文件传 blob    POST  /repos/{r}/git/blobs            （base64）
3. 在远端 tree 上建新 tree     POST  /repos/{r}/git/trees            （base_tree + entries）
   删除用 {"path": p, "mode": "100644", "type": "blob", "sha": null}
4. 建 commit                  POST  /repos/{r}/git/commits
5. 移动 ref                   PATCH /repos/{r}/git/refs/heads/main
```

### [重要] 让远端 commit SHA 与本地一致的四个坑（都踩过）

**前两个是 `gh api` 的，后两个是提交对象的。**

#### 坑 1：`gh api` 默认是 GET，写操作要显式指定

```python
["gh", "api", path]                                   # 404 Not Found
["gh", "api", path, "--method", "POST", "--input", "-"]  # 对
```

404 看起来像「端点写错了」，其实是**动词错了**。

#### 坑 2：`--input -` 不能省

只把 JSON 塞进 stdin、不加 `--input -`，gh 仍发空 body，API 报
`422 content: missing_field` —— 又像 schema 错误，其实是 **body 根本没送到**。

#### 坑 3：[重要] 提交信息末尾的换行**是内容的一部分**

```python
message = run(["git","log","-1","--format=%B"]).stdout.rstrip("\n")   # ✗ SHA 会变
```

commit 对象里 message 后面那个换行参与哈希，`rstrip` 删掉它 → SHA 不同 →
`git status` 永远显示 `ahead 1`，下次推送被当成分叉。

**正确做法：读原始对象，按空行切分，逐字节精确**：

```python
raw = run_raw(["git", "cat-file", "commit", sha]).decode("utf-8")
header, _, message = raw.partition("\n\n")
```

#### 坑 4：原始对象的日期是 Unix 时间戳，API 要 ISO 8601

```
raw header :  author Name <email> 1789698069 +0800
API 需要的  :  2026-09-18T10:21:09+08:00
```

直接传 → `422 ... is not a valid date-time`。

#### 附：`git commit-tree` 不给 `-m`/`-F` 会**永久阻塞**

它从 stdin 读提交信息。后台进程 stdin 是关的 → **挂住且不报错**。
（2026-09-18 实测：第一次跑就挂在这里，输出停在元数据之后，看不出原因。）

### 改完必做的核对

- [ ] 远端 `refs/heads/main` 的 sha == 本地 `git rev-parse HEAD`
- [ ] 远端 commit 的 `tree.sha` == 本地 `git rev-parse HEAD^{tree}`
- [ ] `git update-ref refs/remotes/origin/main <sha>` ——
      **不做的话 `git status` 一直说 `ahead 1`，下次推送会被当成分叉**
- [ ] `git status -sb` 显示 `## main...origin/main`，无 ahead/behind

> **为什么非要对齐 SHA**：内容一致（tree 相同）不等于状态一致。
> 只要 SHA 不同，git 就认为两边分叉了，之后每一次推送都要处理这个假分叉。

---

## 相关

- 环境红线 → [`redlines.md`](redlines.md)
- 启动命令 → [`run.md`](run.md)
- 方法论教训（怎么不再重犯）→ [`../lessons/process.md`](../lessons/process.md)