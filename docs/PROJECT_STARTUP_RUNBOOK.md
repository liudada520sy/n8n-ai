# 整车数据版本比对项目：启动、验证与故障恢复手册

更新日期：2026-08-05  
项目目录：`D:\n8n_AI`  
Conda 环境：`E:\conda_envs\n8n-ai`

## 1. 当前项目组成

```text
飞书用户
  ↓ 文件事件
cloudflared 临时公网地址
  ↓ /webhooks/feishu
飞书中间件 FastAPI（Windows 主机 8080）
  ├─ 下载飞书文件
  ├─ 调用 Tika（9998）提取文本
  └─ POST 到 n8n Webhook（5678）
       ↓
n8n：Webhook → Edit Fields → HTTP Request
       ↓ host.docker.internal:8090
report_pipeline_api（Windows 主机 8090）
  ├─ 配对旧版和新版
  ├─ DocumentComparator → Embedding（8001）+ Qdrant（6333）
  ├─ Qwen 工程变更总结
  └─ 飞书交互卡片回复
```

## 2. 端口清单

| 服务 | 地址 | 运行位置 | 用途 |
| --- | --- | --- | --- |
| n8n | `http://127.0.0.1:5678` | Docker | 工作流编排 |
| 飞书中间件 | `http://127.0.0.1:8080` | Windows/Conda | 飞书事件、文件下载、Tika 调用 |
| 分析 API | `http://127.0.0.1:8090` | Windows/Conda | 新旧版本配对、Diff、Qwen、卡片 |
| Embedding | `http://127.0.0.1:8001` | Docker | 文本向量化；容器内端口为 8000 |
| Qdrant HTTP | `http://127.0.0.1:6333` | Docker | 向量检索 |
| Qdrant gRPC | `127.0.0.1:6334` | Docker | Qdrant gRPC |
| Apache Tika | `http://127.0.0.1:9998` | Docker | 文档纯文本提取 |
| Postgres | 容器内 `postgres:5432` | Docker | n8n 数据库，不暴露到主机 |

注意：浏览器不能使用 `0.0.0.0`。服务可以监听 `0.0.0.0`，但本机访问仍使用 `127.0.0.1`。

## 3. 两个环境文件的分工

### `D:\n8n_AI\.env`

供 Docker Compose 自动读取，主要包含：

- Postgres 配置
- `N8N_ENCRYPTION_KEY`
- Docker 对外端口
- `EMBEDDING_PORT=8001`
- `N8N_PORT=5678`
- `TIKA_PORT=9998`
- `QDRANT_HTTP_PORT=6333`

### `D:\n8n_AI\.env.local`

供 Uvicorn/FastAPI 和本地 Python 程序通过 `--env-file` 读取，主要包含：

- 飞书应用凭证
- Tika、n8n、Embedding、Qdrant 地址
- Qwen 地址、模型及密钥
- `PIPELINE_MOCK_MODE`
- `REPORT_PIPELINE_PATH`
- 新旧版本测试文件路径

不要把真实 App Secret、Token 或 Qwen 密钥复制到日志、截图或 Git 仓库。

## 4. 启动方式一：最小恢复测试（建议现在先执行）

这个模式只验证 `report_pipeline_api` 的新旧版本配对逻辑。它不需要飞书、cloudflared、n8n、Tika、Embedding 或 Qwen。

### 4.1 中断残留测试

如果终端仍停在 `Sending the old version...`，按：

```text
Ctrl + C
```

同时关闭之前重复启动、已经失去响应的 8090 Uvicorn 窗口。

### 4.2 启动 8090 分析 API

打开 PowerShell 窗口 A：

```powershell
conda activate E:\conda_envs\n8n-ai
Set-Location D:\n8n_AI

$env:PIPELINE_MOCK_MODE = "true"

python -m uvicorn report_pipeline_api:app `
    --host 0.0.0.0 `
    --port 8090 `
    --env-file "D:\n8n_AI\.env.local"
```

窗口 A 必须保持打开，并出现：

```text
Application startup complete.
Uvicorn running on http://0.0.0.0:8090
```

如果一直没有出现 `Application startup complete`，优先检查系统内存和分页文件，参见第 10 节。

### 4.3 检查 8090

打开 PowerShell 窗口 B：

```powershell
curl.exe --max-time 5 http://127.0.0.1:8090/health
```

预期：

```json
{"status":"ok"}
```

### 4.4 运行配对测试

在窗口 B 运行：

```powershell
powershell -ExecutionPolicy Bypass -File `
    "C:\Users\22213\Documents\Codex\2026-07-29\ai-devops-n8n-mvp-ai-ai\outputs\n8n-integration\test_report_api.ps1"
```

更新后的脚本会先执行健康检查，并为每个请求设置 10 秒超时。

预期结果：

```text
Sending the old version...
status: waiting_for_pair

Sending the new version...
status: completed

report_pipeline_api pairing and analysis smoke test passed.
```

窗口 A 应出现两条：

```text
POST /n8n/version-file HTTP/1.1 200 OK
POST /n8n/version-file HTTP/1.1 200 OK
```

## 5. 启动方式二：完整本地基础设施

### 5.1 启动 Docker Desktop

先确认 Docker Desktop 已完全启动，然后打开 PowerShell：

```powershell
conda activate E:\conda_envs\n8n-ai
Set-Location D:\n8n_AI

docker compose config
docker compose up -d

docker compose ps
```

第一次构建或依赖变更时使用：

```powershell
docker compose up -d --build
```

日常启动已经创建过的容器时，`docker compose up -d` 即可。

### 5.2 Docker 健康检查
# 根因是 Docker Desktop 中 8001 和 6333 的宿主机端口转发状态失效；容器内部服务一直正常。通过保留数据卷、强制重建受影响容器刷新了端口映射：
健康检查时出现问题：
docker compose up -d --force-recreate qdrant
docker compose up -d --force-recreate local-embedding-service
# 如果 5678 出现“连接被意外关闭”或 Empty reply
docker compose up -d --force-recreate n8n

之后重新运行：docker compose ps

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
Invoke-RestMethod http://127.0.0.1:9998/version
Invoke-RestMethod http://127.0.0.1:6333/healthz
```

浏览器打开：

```text
http://127.0.0.1:5678
```

如果 n8n 拒绝连接：

```powershell
docker compose ps
docker compose logs --tail 100 n8n
```

## 6. 启动飞书中间件（8080）

打开 PowerShell 窗口 C：

```powershell
conda activate E:\conda_envs\n8n-ai
Set-Location D:\n8n_AI

python -m uvicorn app:app `
    --app-dir "D:\n8n_AI\feishu-middleware" `
    --host 127.0.0.1 `
    --port 8080 `
    --env-file "D:\n8n_AI\.env.local"
```

保持窗口 C 打开。检查：

```powershell
curl.exe --max-time 5 http://127.0.0.1:8080/openapi.json
```

浏览器文档地址：

```text
http://127.0.0.1:8080/docs
```

访问 `/` 返回 `404 Not Found` 是正常现象，因为当前代码没有定义首页路由。

## 7. 启动 cloudflared 临时公网隧道

打开 PowerShell 窗口 D：

```powershell
& "D:\n8n_AI\tools\cloudflared\cloudflared.exe" tunnel `
    --url http://127.0.0.1:8080
```

终端会生成类似地址：

```text
https://随机字符串.trycloudflare.com
```

飞书事件订阅回调地址应填写：

```text
https://随机字符串.trycloudflare.com/webhooks/feishu
```

临时隧道每次重启都可能生成新的域名；域名变化后必须同步更新飞书后台的事件订阅地址。

## 8. 启动分析 API（8090）并接入 n8n

打开 PowerShell 窗口 E：

```powershell
conda activate E:\conda_envs\n8n-ai
Set-Location D:\n8n_AI

python -m uvicorn report_pipeline_api:app `
    --host 0.0.0.0 `
    --port 8090 `
    --env-file "D:\n8n_AI\.env.local"
```

健康检查：

```powershell
curl.exe --max-time 5 http://127.0.0.1:8090/health
```

n8n 工作流应为：

```text
Webhook → Edit Fields → HTTP Request
```

HTTP Request 节点关键设置：

- Method：`POST`
- URL：`http://host.docker.internal:8090/n8n/version-file`
- Body Content Type：`JSON`
- Timeout：`180000` ms
- Retry On Fail：开启，Max Tries 为 `2`

不能在 n8n 容器中填写 `127.0.0.1:8090`，因为该地址指向 n8n 容器自身。

## 9. 完整联调顺序

### 9.1 Mock 模式

确认 `D:\n8n_AI\.env.local` 包含：

```dotenv
PIPELINE_MOCK_MODE=true
```

修改环境文件后必须重启 8090。

依次向飞书机器人发送：

1. `D:\n8n_AI\test-data\vehicle_spec_v1.txt`
2. `D:\n8n_AI\test-data\vehicle_spec_v2.txt`

预期：

- 中间件分别回复“收到文件”和“文本提取成功”。
- n8n 产生两次成功执行。
- 第一次 8090 返回 `waiting_for_pair`。
- 第二次 8090 返回 `completed`。
- Mock 模式不调用 Qwen，也不会发送最终真实分析卡片。

### 9.2 真实模式

把 `.env.local` 修改为：

```dotenv
PIPELINE_MOCK_MODE=false
EMBEDDING_URL=http://127.0.0.1:8001/v1/embeddings
QDRANT_URL=http://127.0.0.1:6333
```

同时确认：

- `QWEN_BASE_URL` 可以从 Windows 主机访问。
- `QWEN_MODEL` 与内网服务实际模型名一致。
- 若 Qwen 要求鉴权，`QWEN_API_KEY` 已填写。
- `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET` 正确。
- Docker 中 Embedding 和 Qdrant 正常。

停止并重启 8090 后，再次按 v1、v2 顺序发送文件。第二份完成后，应收到 Qwen 工程摘要飞书卡片。

## 10. 昨晚卡住问题的处理

### 10.1 现象

测试脚本只显示：

```text
Sending the old version...
```

随后长期没有输出。

### 10.2 判断

第一份文件只进入内存配对缓存，不会调用 Qwen、Embedding、Qdrant 或飞书。因此该现象说明：

- 8090 没有启动；或
- 8090 端口被其他失去响应的进程占用；或
- Windows 内存/分页文件不足，导致 PowerShell 或 Uvicorn 无法响应。

诊断过程中已出现 Windows 错误：

```text
The paging file is too small for this operation to complete.
HRESULT: 0x800705AF
```

### 10.3 立即恢复步骤

1. 在卡住的测试终端按 `Ctrl + C`。
2. 停止旧的 8090 Uvicorn，重新启动以清空内存配对状态。
3. 在任务管理器查看内存占用，关闭重复的 Python、PowerShell、cloudflared 进程。
4. 按第 4.2 节启动 Mock 8090。
5. 先运行 `/health`，成功后再运行测试脚本。
6. 如果 8090 被占用，使用 8091，并同步修改测试参数及 n8n URL。

查看端口：

```powershell
netstat -ano | findstr :8090
```

使用 8091：

```powershell
python -m uvicorn report_pipeline_api:app `
    --host 0.0.0.0 `
    --port 8091 `
    --env-file "D:\n8n_AI\.env.local"

powershell -ExecutionPolicy Bypass -File `
    "C:\Users\22213\Documents\Codex\2026-07-29\ai-devops-n8n-mvp-ai-ai\outputs\n8n-integration\test_report_api.ps1" `
    -ApiBaseUrl "http://127.0.0.1:8091"
```

对应的 n8n URL 改为：

```text
http://host.docker.internal:8091/n8n/version-file
```

### 10.4 内存不足时的最小化处理

Mock 适配器测试不需要 Embedding，可以临时停止占用内存较多的容器：

```powershell
Set-Location D:\n8n_AI
docker compose stop local-embedding-service
```

测试完成后恢复：

```powershell
docker compose start local-embedding-service
```

如果 Windows 继续出现分页文件不足：

1. 打开“系统属性”。
2. 进入“高级 → 性能设置 → 高级 → 虚拟内存”。
3. 勾选“自动管理所有驱动器的分页文件大小”。
4. 保存并重启 Windows。

## 11. VSCode 启动方法

用 VSCode 打开：

```text
D:\n8n_AI
```

选择 Python 解释器：

```text
E:\conda_envs\n8n-ai\python.exe
```

当前 `.vscode\launch.json` 已包含：

1. 第三步 Mock 文档比对。
2. 飞书 FastAPI 中间件 8080。
3. 第四步完整分析流水线。

8090 分析 API 当前建议使用 PowerShell 命令启动，以便明确控制 `PIPELINE_MOCK_MODE` 和观察请求日志。

## 12. 停止与重新启动

### 停止 Python 服务和隧道

分别在对应终端按：

```text
Ctrl + C
```

### 停止 Docker，但保留数据

```powershell
Set-Location D:\n8n_AI
docker compose stop
```

### 恢复 Docker

```powershell
docker compose start
docker compose ps
```

### 删除容器但保留持久化卷

```powershell
docker compose down
```

除非明确需要清空全部 n8n/Postgres/Qdrant 数据，否则不要执行：

```text
docker compose down -v
```

## 13. 每次启动后的快速检查清单

- [ ] Docker Desktop 已启动。
- [ ] `docker compose ps` 中 Postgres、Embedding 为 healthy。
- [ ] `127.0.0.1:5678` 能打开 n8n。
- [ ] Tika 9998 正常。
- [ ] Qdrant 6333 正常。
- [ ] Embedding 8001 正常。
- [ ] 飞书中间件 8080 正常。
- [ ] cloudflared 当前公网地址已填写到飞书后台。
- [ ] 分析 API 8090 `/health` 返回 `ok`。
- [ ] n8n HTTP Request 使用 `host.docker.internal:8090`。
- [ ] 当前 `PIPELINE_MOCK_MODE` 与测试阶段一致。
- [ ] 修改 `.env.local` 后相关 Uvicorn 服务已重启。

