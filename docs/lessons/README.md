---
title: 经验教训索引（按症状查）
read_this: [重要] 出错时第一站
---

# 经验教训（按症状查）

> **出错时第一站。** 找到症状 → 跳到对应的坑。
>
> 每一份都回答「**当时发生了什么 / 根因 / 怎么修 / 推广规则**」，
> 不是「有个坑叫 X」。

---

## 按症状查

### 命令类

| 症状 | 去哪 |
|---|---|
| 命令**卡住不返回**（脚本明明跑完了） | [`../env/tooling.md`](../env/tooling.md) §Start-Process 占住 stdout |
| 装依赖**跑到一半就停** | [`process.md`](process.md) §不要轮询终端 |
| 输出被吞 / `Select-Object -Last N` 返回空 | [`../env/tooling.md`](../env/tooling.md) §输出被吞 |
| `.cmd`（npm / tsc）没有输出 | [`../env/tooling.md`](../env/tooling.md) §cmd 包装器 |
| 环境变量影响了后面的命令 | [`../env/tooling.md`](../env/tooling.md) §环境变量泄漏 |
| 删了文件但 grep 还能搜到 | [`../env/tooling.md`](../env/tooling.md) §grep 缓存 |
| `cd` 在下一条命令里丢了 | [`../env/tooling.md`](../env/tooling.md) §cwd 不持久化 |
| 用 here-string 跑 `python -c` 失败 | [`../env/tooling.md`](../env/tooling.md) §here-string |
| `Write-Output` 的进度不实时出现 | [`../env/tooling.md`](../env/tooling.md) §Write-Output |

### 反向验证 / 探针

| 症状 | 去哪 |
|---|---|
| **探针被中断了，现在怎么办？** | [`process.md`](process.md) §探针被中断 [重要] **先恢复再跑测试** |
| 反向验证报 `not caught` | [`process.md`](process.md) §不可证伪的测试 |
| 反向验证报 `RESULT=INCOMPLETE` | [`process.md`](process.md) §sidecar 命名冲突 |
| 不确定「这个守卫真的有在检查吗」 | [`process.md`](process.md) §反向验证 |

### 测试 / 验证

| 症状 | 去哪 |
|---|---|
| 测试全绿但功能是坏的 | [`process.md`](process.md) §控制存在但没接上 |
| 加了守卫但不知道有没有用 | [`process.md`](process.md) §反向验证 |
| 检查脚本全绿但没在检查 | [`process.md`](process.md) §不可证伪的测试 |
| pytest 不打印汇总行 | [`../env/run.md`](../env/run.md) §测试 |

### 前端

| 症状 | 去哪 |
|---|---|
| **无限重渲染** / `Maximum update depth exceeded` | [`frontend.md`](frontend.md) §selector 返回新数组 |
| 撤销按钮是灰的（应用 AI 改动后） | [`frontend.md`](frontend.md) §setGraph 清历史 |
| **改一次却调了两次模型** | [`frontend.md`](frontend.md) §StrictMode |
| 新建工作流后保存**覆盖了上一个** | [`frontend.md`](frontend.md) §emptyGraph 字段名 |
| 类型错误里出现两条超长的 `Plugin<any>` 路径 | [`frontend.md`](frontend.md) §vitest 版本冲突 |
| `vite.config.ts` 的 `test` 是未知属性 | [`frontend.md`](frontend.md) §defineConfig 来源 |

### i18n

| 症状 | 去哪 |
|---|---|
| 中文界面里校验提示是英文 | [`i18n.md`](i18n.md) §坑 1（接口漏 localize） |
| 提示里出现屏幕上不存在的名字（「Prompt」vs「提示词」） | [`i18n.md`](i18n.md) §坑 2（内嵌标签） |
| 界面切了但徽章没切 | [`i18n.md`](i18n.md) §坑 3（不会自动重校验） |
| 守卫的正则永远匹配不到 | [`i18n.md`](i18n.md) §`r"\u300c"` 不是转义 |
| `_zh` 字段漏到客户端 | [`i18n.md`](i18n.md) §检查清单 |

### 后端

| 症状 | 去哪 |
|---|---|
| `InvalidRequestError: ... seems to be using a generic class` | [`backend.md`](backend.md) §SQLModel 关系解析 |
| `SettingsError: error parsing value for field "cors_origins"` | [`backend.md`](backend.md) §NoDecode |
| HITL 暂停被当成运行失败 | [`backend.md`](backend.md) §GraphBubbleUp 被吞 |
| **UI 看起来是死的**（事件没到） | [`backend.md`](backend.md) §contextvar 不传播 |
| LangGraph 签名告警 | [`backend.md`](backend.md) §类型注解 |
| 环检测报哪个节点不确定 | [`backend.md`](backend.md) §Tarjan SCC |
| **显式清空**必填字段但校验通过 | [`backend.md`](backend.md) §默认值掩盖 |
| 落库静默失败（`no such table`） | [`backend.md`](backend.md) §测试忘记建表 |
| Code 节点报 `MemoryError:` 后面**什么都没有** | [`../log/phase-1-1-sandbox.md`](../log/phase-1-1-sandbox.md) |
| 容器**启动正常但每个 snippet 都 ModuleNotFoundError** | [`../log/phase-1-1-sandbox.md`](../log/phase-1-1-sandbox.md) |
| **工具下拉框里有一个实际不存在的工具** | [`../log/phase-1-2-tools.md`](../log/phase-1-2-tools.md) |
| 关掉 `fail_on_error` 但「工具未启用」仍然整个 run 失败 | [`../log/phase-1-2-tools.md`](../log/phase-1-2-tools.md) |
| `_runs` 内存一直涨 | [`../log/phase-2-2-retention.md`](../log/phase-2-2-retention.md) |
| transcript 丢了开头 | [`../log/phase-2-2-retention.md`](../log/phase-2-2-retention.md) |
| 已结束的运行 `duration_ms` 一直变长 | 同上 |

### 数据库 / 迁移

| 症状 | 去哪 |
|---|---|
| `alembic` 所有命令死于 `UnicodeDecodeError: 'gbk'` | [`../log/phase-2-1-alembic.md`](../log/phase-2-1-alembic.md) |
| 自动生成的迁移 `NameError: name 'sqlmodel' is not defined` | 同上 |
| `KeyError: 'formatters'` | 同上 |
| `DeprecationWarning` 25 条 | 同上 |
| Windows `PermissionError: [WinError 32]`（删 SQLite 文件） | 同上 |

### 安全

| 症状 | 去哪 |
|---|---|
| **控制逻辑对、测试充分，但产品里没启用** | [`security.md`](security.md) §控制存在但没接上 |
| `http_request` 报 `egress_denied` | [`../env/`](../env/) 排查清单 · [`../current/security.md`](../current/security.md) §4 |
| Code 节点报 `Sandbox unavailable` | [`../current/security.md`](../current/security.md) §3 |

### 环境 / 端口

| 症状 | 去哪 |
|---|---|
| 后端绑定 8000 报 `WSAEADDRINUSE` 但进程不存在 | [`../env/ports.md`](../env/ports.md) |
| 前端所有 API 请求失败 | 后端不在 vite 代理指向的端口（`BACKEND_PORT`，默认 8001） → [`../env/ports.md`](../env/ports.md) |
| 改了前端但没生效 | 忘了 `sync_src.ps1` → [`../env/frontend-temp.md`](../env/frontend-temp.md) |
| `pnpm install` 报 `ERR_PNPM_EPERM: ... rename` | [`../env/frontend-temp.md`](../env/frontend-temp.md) |
| 从前端从别的机器访问不通 | **设计如此**（auth local 只接受回环） → [`../current/security.md`](../current/security.md) §2 |

---

## 领域分册

| 文件 | 内容 |
|---|---|
| [`process.md`](process.md) | [重要] **方法论** —— 反向验证、探针、猜 vs 测、前提验证 |
| [`frontend.md`](frontend.md) | 前端 · store · React · 构建工具 |
| [`i18n.md`](i18n.md) | 国际化的三个陷阱 + 那个静默失效的正则 |
| [`backend.md`](backend.md) | 后端 · SQLModel · LangGraph · 编译器 · 沙盒 |
| [`security.md`](security.md) | 安全的教训（最危险的一类盲区） |

---

## 相关

- 每一刀的完整记录（含真 bug） → [`../log/`](../log/)
- 已知缺陷（还没修的） → [`../current/limitations.md`](../current/limitations.md)
- 环境红线 → [`../env/redlines.md`](../env/redlines.md)
