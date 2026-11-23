# Contributing

Thanks for your interest in Agent Scaffold!

## 开发环境

1. 后端：`cd backend && python -m venv .venv && pip install -r requirements.txt`
2. 前端：`cd frontend && npm install`
3. 配置：`cp .env.example .env`，填入你自己的模型 API 信息（不要提交 `.env`）

## 提交 PR

1. Fork 本仓库并从 `main` 切分支（如 `fix/xxx`、`feat/xxx`）
2. 保持改动聚焦：一个 PR 只做一件事
3. 后端改动请补测试（`backend/tests`），前端改动请跑 `npm run typecheck` 与 `npm run test`
4. 提交信息用 Conventional Commits（`feat:` / `fix:` / `docs:` / `refactor:`）
5. 如果是 AI 辅助编写的代码，请在 PR 描述里注明工具与范围

## 代码风格

- Python：黑盒格式化 + 类型注解优先，测试放 `backend/tests`
- TypeScript/React：遵循现有目录结构与命名

## Issue

- Bug 报告请附最小复现步骤、期望/实际结果、日志
- 功能建议请说明使用场景，而不是只给方案
