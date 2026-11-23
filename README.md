# Agent Scaffold

[![CI](https://github.com/hkhkkh/agent_scaffold/actions/workflows/ci.yml/badge.svg)](https://github.com/hkhkkh/agent_scaffold/actions/workflows/ci.yml)
![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)

> **Visual AI-agent workflow platform** — 在画布上搭 AI Agent 工作流，编译成可执行图。
> 并发、预算、故障隔离、记忆、调度各自独立成层。

## 这是什么

Agent Scaffold 把「画布上的 AI Agent 编排」变成「可编译、可复现、可观测的执行图」：

- **画布 → 编译**：节点图一次编译，多次运行；图哈希 + 版本锁定保证可复现
- **执行策略独立**：并发、超时、重试、预算（token/费用）都是独立策略层，不和业务逻辑耦合
- **故障隔离**：节点级超时/重试/熔断，单节点失败不拖垮整张图
- **记忆与调度解耦**：memory / scheduling 是可替换的层，而不是散落在节点代码里
- **运行可观测**：每次 run 有历史、事件流、日志与用量统计

## 架构

```
前端画布 (React + Vite)
        │  REST / SSE
        ▼
FastAPI 后端 ──► 编译器 (canvas → graph)
        │
        ▼
RunManager ──► 调度器 ──► 节点执行器 (LLM / 工具 / 代码节点)
        │            ├─ 策略层：并发 · 超时 · 重试 · 预算
        │            └─ 记忆层：memory store
        ▼
运行历史 / 事件 / 日志 / 用量
```

详细设计见 [docs/ARCHITECTURE.zh.md](docs/ARCHITECTURE.zh.md)。

## 快速开始

### 后端

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate  /  macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # 填入 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
uvicorn app.main:app --reload --port 8000
```

### 前端

```bash
cd frontend
npm install
npm run dev        # 默认 http://localhost:5173
```

### 测试

```bash
cd backend && pytest -q
cd frontend && npm run test
```

## 目录结构

```
backend/    FastAPI 服务：app/（API、runtime、策略、记忆）、tests/
frontend/   React + TypeScript 画布与运行界面
docs/       架构与设计文档
scripts/    开发/运维脚本
```

## 配置

复制 `.env.example` 为 `.env` 后填写。所有密钥只放在 `.env`，**不要提交到仓库**（`.gitignore` 已覆盖）。

## 状态

- `v0.1.0` — 首个公开版本：画布编排、编译运行、运行历史、预算与重试策略。
- 路线图见 [docs/target/](docs/target/)。

## 贡献

欢迎 issue 和 PR，见 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题请按 [SECURITY.md](SECURITY.md) 报告。

## 许可证

[MIT](LICENSE)
