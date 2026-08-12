# n8n AI 整车版本比对 MVP

本项目在本机完成飞书文件接收、文本提取、n8n 编排、新旧版本配对、文档差异分析和结果回传。当前 M1 验收使用 Mock 模式；真实 Embedding、Qdrant、Qwen 与飞书卡片联调属于后续里程碑。

## 架构

```text
飞书文件消息
  -> cloudflared
  -> 飞书中间件（Windows/Conda，8080）
       -> Apache Tika（Docker，9998）
       -> n8n Webhook（Docker，5678）
            -> Edit Fields
            -> HTTP Request
                 -> report_pipeline_api（Windows/Conda，8090）
                      -> DocumentComparator
                      -> Embedding（Docker，主机端口 8001）
                      -> Qdrant（Docker，6333）
                      -> Qwen API
                      -> 飞书交互卡片
```

Docker Compose 管理 Postgres、Qdrant、Tika、Embedding 和 n8n。飞书中间件与分析 API 当前由 Windows 上的 Conda 环境单独启动。

## 前置条件

- Docker Desktop 已启动。
- Conda 环境位于 `E:\conda_envs\n8n-ai`。
- 项目目录为 `D:\n8n_AI`。
- 根目录已有本机专用的 `.env` 和 `.env.local`。这两个文件包含敏感配置，不得提交到 Git。

## 启动

### 1. Docker 基础服务

```powershell
Set-Location D:\n8n_AI
docker compose config
docker compose up -d
docker compose ps
```

首次构建或 Embedding 依赖变化时使用：

```powershell
docker compose up -d --build
```

### 2. 飞书中间件（8080）

在独立 PowerShell 窗口执行并保持运行：

```powershell
conda activate E:\conda_envs\n8n-ai
Set-Location D:\n8n_AI
python -m uvicorn app:app `
    --app-dir "D:\n8n_AI\feishu-middleware" `
    --host 127.0.0.1 `
    --port 8080 `
    --env-file "D:\n8n_AI\.env.local"
```

检查：

```powershell
curl.exe --max-time 5 http://127.0.0.1:8080/openapi.json
```

### 3. cloudflared 隧道

在独立 PowerShell 窗口执行：

```powershell
& "D:\n8n_AI\tools\cloudflared\cloudflared.exe" tunnel `
    --url http://127.0.0.1:8080
```

将生成的 HTTPS 地址加上 `/webhooks/feishu`，配置为飞书事件订阅回调地址。

### 4. 分析 API（8090）

在独立 PowerShell 窗口执行并保持运行：

```powershell
conda activate E:\conda_envs\n8n-ai
Set-Location D:\n8n_AI
python -m uvicorn report_pipeline_api:app `
    --host 0.0.0.0 `
    --port 8090 `
    --env-file "D:\n8n_AI\.env.local"
```

检查：

```powershell
curl.exe --max-time 5 http://127.0.0.1:8090/health
```

n8n 容器内的 HTTP Request 节点必须使用：

```text
POST http://host.docker.internal:8090/n8n/version-file
```

## 测试

全部单元测试只使用一个入口：

```powershell
Set-Location D:\n8n_AI
E:\conda_envs\n8n-ai\python.exe -m unittest discover -s tests -v
```

若已激活 Conda 环境，也可执行：

```powershell
python -m unittest discover -s tests -v
```

8090 Mock 配对 Smoke Test（要求 8090 已启动且 `PIPELINE_MOCK_MODE=true`）：

```powershell
powershell -ExecutionPolicy Bypass -File `
    "D:\n8n_AI\tests\smoke\test_report_api.ps1"
```

预期第一份文件返回 `waiting_for_pair`，第二份返回 `completed`。

## 环境变量

只在 `.env` 或 `.env.local` 中配置实际值。不要把 Secret、Token、API Key 或完整工程文档写入 README、日志或 Git。

Docker/Compose：

- `POSTGRES_DB`、`POSTGRES_USER`、`POSTGRES_PASSWORD`
- `N8N_VERSION`、`N8N_HOST`、`N8N_PORT`、`N8N_PROTOCOL`
- `N8N_ENCRYPTION_KEY`、`WEBHOOK_URL`
- `GENERIC_TIMEZONE`、`TZ`
- `QDRANT_HTTP_PORT`、`QDRANT_GRPC_PORT`
- `TIKA_PORT`
- `EMBEDDING_PORT`、`EMBEDDING_MODEL_NAME`、`EMBEDDING_DEVICE`

飞书中间件：

- `FEISHU_BASE_URL`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET`
- `FEISHU_VERIFICATION_TOKEN`、`FEISHU_ENCRYPT_KEY`
- `TIKA_BASE_URL`、`N8N_WEBHOOK_URL`
- `REQUEST_TIMEOUT_SECONDS`、`MAX_FILE_BYTES`
- `EVENT_DEDUP_TTL_SECONDS`、`LOG_LEVEL`

分析流水线：

- `PIPELINE_MOCK_MODE`、`REPORT_PIPELINE_PATH`
- `OLD_FILE_PATH`、`NEW_FILE_PATH`、`FEISHU_MESSAGE_ID`
- `TIKA_URL`、`EMBEDDING_URL`、`QDRANT_URL`、`SIMILARITY_THRESHOLD`
- `QWEN_BASE_URL`、`QWEN_API_KEY`、`QWEN_MODEL`
- `QWEN_TIMEOUT_SECONDS`、`QWEN_MAX_TOKENS`、`QWEN_MAX_INPUT_CHARS`
- `FEISHU_TENANT_ACCESS_TOKEN`、`FEISHU_TIMEOUT_SECONDS`
- `CARD_SUMMARY_MAX_CHARS`、`CARD_DETAIL_MAX_CHARS`
- `EXCEL_KEY_COLUMN`、`VERSION_PAIR_TTL_SECONDS`

## 停止与排查

停止 Docker 服务但保留数据卷：

```powershell
Set-Location D:\n8n_AI
docker compose down
```

不要使用 `docker compose down -v`，除非明确要删除 n8n、Postgres、Qdrant 和模型缓存数据。

详细启动、健康检查及故障恢复见 `docs/PROJECT_STARTUP_RUNBOOK.md`。
