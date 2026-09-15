# Enterprise Agentic RAG v6

简体中文 | [English](README.en.md)

一个由评测驱动、全链路可插拔的企业级 Agentic RAG 平台。

项目从零开始，通过小粒度、经评审的 Pull Request 持续构建。完整架构、验收标准和 64 个 PR 的开发路线见 [DEV_SPEC.md](DEV_SPEC.md)。

## 仓库结构

```text
backend/   FastAPI 后端与 Python 测试
frontend/  Vue 3 + TypeScript 前端
evals/     版本化评测数据集与 fixture
infra/     本地及生产基础设施
docs/      架构决策与运维文档
```

## 环境要求

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Node.js 22 或更高版本
- pnpm 11
- Tesseract 5，并安装 `chi_sim` 与 `eng` 语言数据

## 启动当前项目

在仓库根目录安装所有锁定依赖：

```bash
uv sync --project backend --locked
pnpm install --frozen-lockfile
```

### macOS 完整语义演示（推荐）

这个入口运行可切换的本地/远程多语 Embedding 与 CrossEncoder Reranker、Milvus Lite、后台文档
解析/摄取 Worker，并通过 OpenAI-compatible Chat Completions 调用 LLM。默认本地 Embedding/Rerank
不会把文档和查询发送给 Embedding/Rerank 外部服务；选择 SiliconFlow 远程 profile 后，相应的
Leaf/查询或候选文本会发送给 SiliconFlow。OpenAI-compatible LLM Planner 会接收当前问题、最多 12 条
会话历史与服务端约束的 Scope；回答阶段只接收经过租户权限复核后恢复的有限 Root 证据。

先安装系统依赖并准备仅本机使用的配置：

```bash
brew install tesseract tesseract-lang
cp .env.mac.example .env
```

编辑 `.env`，把 `LLM_API_KEY` 改为自己的 TokenHub Token；如需使用远程 BGE profile，再填写
`SILICONFLOW_API_KEY`。TokenHub 当前返回的模型 ID 是大小写敏感的 `MiniMax-M3`；`.env` 已被 Git
忽略，不能提交、复制进 Issue 或写入日志。然后在第一个终端
启动 PostgreSQL、迁移和完整 FastAPI+Worker 组合：

```bash
./scripts/mac-backend.sh
```

脚本会创建并迁移专供应用使用的 `enterprise_rag_dev`，同时保留独立的
`enterprise_rag_test` 给集成测试。测试夹具只允许清理 test 库，不会再级联删除 Mac 工作区数据；两者
继续共用一个持久化 PostgreSQL 容器，但不共用 database。

Mac 组合同时在 `http://127.0.0.1:8000/mcp` 挂载官方 SDK v2 Streamable HTTP MCP。开发环境未显式
配置 `MCP_TOKEN_PEPPER` 时，会在已忽略的 `data/runtime/mac/mcp-token-pepper` 原子生成独立的
0600 secret；它不复用 Session、LLM 或 Provider 密钥，也不会输出到日志。生产环境禁止自动生成，必须
分别配置 HTTPS `MCP_PUBLIC_BASE_URL` 与至少 32 bytes 的 `MCP_TOKEN_PEPPER`。

首次上传或查询会把以下 ONNX 模型下载到已忽略的 `data/runtime/mac/model-cache/`，之后复用缓存：

- Embedding：`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`（384 维）；
- Reranker：`jinaai/jina-reranker-v2-base-multilingual`；
- LLM：`MiniMax-M3`，通过 `.env` 中的 `LLM_BASE_URL` 调用。

图片 Caption 默认关闭（`vision=none`）：Loader 提取的图片仍会保存到本地 ObjectStore，但不离开本机。
如需演示真实图片理解，在 `.env` 中配置 `ENTERPRISE_RAG__PROVIDERS__VISION=openai_compatible`、
`VISION_BASE_URL`、`VISION_API_KEY` 和 `VISION_MODEL`；适配器会以 OpenAI-compatible
`/chat/completions` 的 text + base64 data URI 调用模型。远程 Vision 只发送图片，不发送未授权文档正文，
并对 429、5xx 和传输错误执行有界重试。管理员也可以在 `/admin/providers` 选择关闭或启用该 profile；
选择写入本机的 restart-bound 文件，重启后才生效，密钥永不返回前端。

默认 MiniLM 的 registry 描述是 512 input tokens，但本机 FastEmbed tokenizer 实测上限是 128；页面和
Splitter 以运行时实测值为准。Provider 管理页会同时显示 profile 声明值和当前进程探测到的有效值。
需要 512 输入 token/512 维向量时，可在 `.env` 中改为
`EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5`，并设置
`ENTERPRISE_RAG__INGESTION__EMBEDDING_DIMENSION=512`。这是可插拔的本地模型 profile，首次运行会下载
对应模型；切换模型或维度会创建新的 index revision，旧向量不会与新向量混用。

管理员可在 `/admin/providers` 选择 SiliconFlow 的 `BAAI/bge-m3`（1024 维、官方输入上限 8192
tokens）和 `BAAI/bge-reranker-v2-m3`。两者共用 `SILICONFLOW_API_KEY`，也可分别用
`EMBEDDING_API_KEY`/`RERANK_API_KEY` 覆盖；endpoint 默认是 `https://api.siliconflow.cn/v1`，可用
`EMBEDDING_BASE_URL`/`RERANK_BASE_URL` 单独覆盖。未配置密钥时远程选项会显示“未配置”且不可选择，
密钥永远不会返回前端。BGE-M3 的远程 API 不提供本地 tokenizer 对象，因此页面明确把切分 token
计数标为估算；当前 Leaf 上限远低于 8192，不会把估算冒充精确 tokenizer 结果。

在第二个终端启动 Vue 3 前端：

```bash
pnpm --dir=frontend dev
```

打开 `http://127.0.0.1:5173`。匿名用户会自动获得 Demo Tenant 全部业务权限，可依次新建集合、
上传 PDF/DOCX/XLSX/XLS/CSV/HTML/TXT/Markdown、查看真实解析与摄取进度，再到“知识问答”观察
Dense/Sparse 检索、RRF、CrossEncoder 重排、Root 恢复、LLM 回答与引用。Provider 状态可在
“租户总览”或 `http://127.0.0.1:8000/health/doctor` 查看。管理员登录后，Provider 状态卡片的“管理与选择”
会打开 `/admin/providers`：页面读取当前注册表，展示可用 Embedding/Reranker/Vision/Sparse profile、维度、有效 token
上限、语言说明和本地/远程属性，并可保存下一次启动配置。选择不是热切换；重启 backend 后生效，Embedding
变更还必须重新摄取/重建文档。

Mac 完整语义组合默认使用 `milvus_builtin_bm25`。文档投影把 Leaf 的 `retrieval_text` 写入 Milvus
启用 `jieba` Analyzer 的 VARCHAR 字段，由原生 `FunctionType.BM25` 维护 TF/IDF，并在
`SPARSE_INVERTED_INDEX` 上以 `metric_type=BM25` 查询；应用层不会伪造 IDF 或返回一个实际未使用的
sparse vector。它支持中文术语、英文编号和中英混合查询，租户、集合、文档和 `ready` 条件仍在同一次
Milvus 请求中强制过滤。离线 Compose 示例继续使用 `hashing_lexical`，因为公开零成本评测需要稳定的
预计算词法向量；两种模式都可插拔，不能共用同一个 revision。Provider 管理页可选择 Sparse 模式，
重启 backend 后执行“重建不兼容文档”，系统会创建新 revision，成功交换 PostgreSQL 事实后才定向清理旧
revision。

首次打开页面并创建 Demo Tenant 后，可从受信任的本机终端签发一个绑定当前全部 active collection 的
MCP Token。raw token 只写 stdout 一次，元数据写 stderr；不要把 token 复制进 Git、Issue 或终端日志：

```bash
umask 077
uv run --project backend --env-file .env enterprise-rag-mcp-token issue \
  > /tmp/enterprise-rag-mcp-token
```

把 `/tmp/enterprise-rag-mcp-token` 的单行值配置到 MCP Client，endpoint 使用
`http://127.0.0.1:8000/mcp`，Authorization 使用 Bearer。默认 Token 具有四个 MCP scope，并且只能访问
签发时 Demo Tenant 已存在的集合；Tool 输入不能扩大 allowlist。查看不含 secret 的元数据或撤销 Token：

```bash
uv run --project backend --env-file .env enterprise-rag-mcp-token list
uv run --project backend --env-file .env enterprise-rag-mcp-token revoke <TOKEN_UUID>
rm -f /tmp/enterprise-rag-mcp-token
```

可在撤销前用官方 SDK Client 做不输出正文/密钥的 live smoke；加 `--run-query` 才会额外调用当前 LLM：

```bash
uv run --project backend --env-file .env python scripts/mac-mcp-smoke.py \
  --token-file /tmp/enterprise-rag-mcp-token
```

该 CLI 是本机 operator 边界，不是匿名 HTTP API。匿名用户仍不能从前端签发、读取或撤销 Token。

租户总览的“加载演示数据”按钮会调用受 CSRF 保护的 `/api/v1/demo/seed`，将仓库内两份非敏感 Markdown
样例通过同一个上传注册、PostgreSQL 任务、解析、清洗、Root/Leaf 切分和向量投影流水线提交。它不是前端
静态 fixture；重复点击按内容摘要幂等，返回已有文档和任务。摄取完成后，这些真实持久化记录可在文档透视、
Ingestion Trace、Query Trace、问答和评测页面查看。

文档进入 `ready` 后，在“文档管理”打开详情并选择“查看解析、清洗与切分”，可检查实际 Parser、
确定性 Cleaner、Splitter 参数、每个 Root 的原文/清洗后对照、规则 audit，以及每个 Leaf 的完整
文本、token 数、offset 和 Leaf 边界（新摄取默认不重叠）。该页面读取 PostgreSQL 事实源，不根据前端猜测切分结果；
升级前摄取且没有 audit 元数据的旧文档会明确标为“旧数据未记录”，重新上传后即可生成完整记录。

同一页面还提供默认关闭的“一次远程 LLM 清洗”。预检会显示 Provider/Model、Root 数、发送字符数、
一次调用预算和数据离开本机的风险；只有勾选确认后，当前版本的 `clean_text`（不是原文件）才会发送。
单次最多 20 Roots、12,000 输入字符和 8,000 output tokens；超限、非 ready、版本变化或已经执行过的
版本会拒绝。适配器会去除 MiniMax 常见的 `<think>...</think>` 推理外壳和 JSON code fence，但不会把推理内容送入清洗校验。
响应必须保持 Root ordinal，并通过完整词法序列、数字、URL、邮箱、引号值、标题、表头和 fenced code
锚点校验，成功后才重切分和重建 Dense/Sparse 索引。LLM 只能修复 PDF/OCR 常见的空白、段落换行、标题/表格间距，
以及单词内部的跨行断字符；不得合并两个不同单词，不得改变词法顺序、数字、事实或代码。唯一允许删除的是跨 Root
重复且位于原始 Root 首/尾的完整噪声行。页面显示 Root 变化、Leaf 前后数量、token usage、
重试次数和持久化 hash audit；失败会尝试恢复原向量与 ready 状态。这个同步、进程内互斥实现仅适合
当前单进程 Mac 演示，多副本生产协调仍属于 M8。

当前 Mac 组合的 Standard 与 Deep 都走真实模型链路。Deep 已装配 M4 的 Evidence Ledger 与最多两轮
Recovery Controller：有证据时由当前 OpenAI-compatible LLM 逐项判断 requirements 覆盖、缺口与冲突；
缺口会实际执行 Rewrite Hybrid、HyDE Dense-only 或 Exact-term Sparse-only，再次经过 RRF、PostgreSQL
权限回源、Rerank 和 Root 恢复。Scope repair 只能移除 Planner 新增且调用方未显式指定的条件，不能放宽
用户选择的 Collection/Document。评估失败会明确标记降级并继续有界恢复，仍无法验证则拒答。

Standard 与 Deep 的回答都不再直接信任自由文本：LLM 必须返回段落、引用 ID、Root/Leaf ID、Root 原文
连续 quote 和覆盖 requirements 的结构化草稿。后端确定性核验每个事实段落、引用归属、quote 与覆盖率；
JSON/schema 错误最多原证据重生成一次；结构有效但核验失败时，再使用完全相同的授权证据修复一次，
重新核验失败就返回空引用拒答。

本轮结构化调用会在 OpenAI-compatible 请求中显式发送 `response_format: {"type":"json_object"}`，而不是
只依赖 Prompt 约束。Query Planner、Evidence Assessor、Answer Author/Repair、可选 LLM Judge 和人工 LLM
清洗均通过同一 `CompletionRequest.json_mode` 开关；普通自由文本调用不会携带该字段。Provider 能力列表会
声明 `json-mode`。如果上游不支持该 OpenAI-compatible 扩展，服务会保留脱敏的稳定错误并按既有边界拒答，
不会把模型返回的 Markdown 或异常文本当成结构化事实。

Mac QueryRunner 会先调用同一个受 timeout/retry 保护的 OpenAI-compatible LLM 生成严格 JSON
QueryPlan：把依赖会话的问题改写为独立检索问题，并按复杂度生成 1～4 条不重复子查询；简单事实问题保留
1 条精确子查询，比较、多条件和多跳问题才拆成多条，不能为了展示而无意义扩增。后端继续严格校验字段、
数量、UUID 与 Scope；坏 JSON、越权 Scope 或 Provider 故障会整体降级为确定性改写/拆分。
完成问答后到“Query Trace”可查看本次改写、子查询、Planner Provider/降级、Planner token、每个分支的
Dense/Sparse 返回量、交集、RRF 去重与淘汰、权限过滤、Rerank、Root 恢复、Deep 证据评估/恢复轮次、
回答生成、引用核验/修复及各自 token。Planner、Assessor、回答与 Repair 调用都会计入查询 usage；即使
没有召回结果，已发生的 Planner 调用仍会如实计费。这里的运行计数
不是 Recall@K；带 gold 的质量指标只在“评测中心”计算。

若旧版本曾让测试库与应用共用，先停止后端，再做只读检查；确认后才应用删除。命令只会删除 PostgreSQL
中已经不存在 tenant/version 的 Milvus 投影，不会删除对象文件、文档或任务：

```bash
uv run --project backend --env-file .env python scripts/mac-reconcile-vectors.py
uv run --project backend --env-file .env python scripts/mac-reconcile-vectors.py --apply
```

Milvus Lite 只允许单进程打开；运行以上命令时后端必须处于停止状态。`vector_count_mismatch` 不会自动
删除，因为它也可能表示有效版本缺失向量，需要重新摄取或人工核对。

停止后端/前端用 `Ctrl+C`；保留 PostgreSQL 和模型缓存便于下次启动。只停止 PostgreSQL：

```bash
docker compose -f infra/compose/compose.dev.yml stop postgres
```

### 离线浏览器验收

若要直接启动当前可交互的离线演示全链路（PostgreSQL、迁移、FastAPI+Worker、Vue），无需模型
密钥：

```bash
docker compose -f infra/compose/compose.e2e.yml up --build postgres backend frontend
```

若 macOS Docker Desktop 在包含中文的仓库路径下报
`x-docker-expose-session-sharedkey ... non-printable ASCII`，这是 BuildKit 尚未读取 Dockerfile 前的
路径兼容错误；可先用经典构建器构建，再启动服务，或把仓库克隆到纯 ASCII 路径：

```bash
DOCKER_BUILDKIT=0 docker compose -f infra/compose/compose.e2e.yml build
docker compose -f infra/compose/compose.e2e.yml up postgres backend frontend
```

本仓库已验证上述兼容命令可以从当前中文路径完成镜像构建和浏览器全旅程。

浏览器打开 `http://127.0.0.1:4173`。这个组合使用确定性 Hashing Dense/Sparse 检索和摘录式回答，
用于本地演示与验收，不代表生产语义模型质量。匿名会话拥有 Demo Tenant 的全部业务权限；如需测试
隔离的管理端，请使用仅供该 Compose 验收环境使用的 `admin` / `admin`。
停止并清除演示数据：

```bash
docker compose -f infra/compose/compose.e2e.yml down --volumes --remove-orphans
```

安装 PDF/OCR 所需系统依赖：

```bash
# macOS（Homebrew）
brew install tesseract tesseract-lang

# Ubuntu/Debian
sudo apt-get update
sudo apt-get install --yes tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim
```

启动开发用 PostgreSQL 并执行数据库迁移：

```bash
docker compose -f infra/compose/compose.dev.yml up -d postgres
./scripts/ensure-local-databases.sh
export DATABASE_URL=postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_dev
export SESSION_SECRET=development-only-change-me-32-bytes-minimum
uv run --project backend alembic -c backend/alembic.ini upgrade head
```

在第一个终端启动后端：

```bash
ENTERPRISE_RAG_CONFIG_FILE=config/development.example.yaml \
  uv run --project backend uvicorn enterprise_rag.main:app --reload
```

开发 API 位于 `http://127.0.0.1:8000`。当前后端提供：

- `GET /` — 服务名称、版本、配置状态和当前环境
- `GET /docs` — 交互式 OpenAPI 文档
- `GET /openapi.json` — OpenAPI Schema
- `GET /api/v1/auth/me` — 创建匿名 demo session 并取得 CSRF token
- `POST /api/v1/auth/login`、`POST /api/v1/auth/logout` — Argon2id 管理员登录与 CSRF 会话撤销
- `GET /api/v1/system/status` — 服务端验证系统管理员边界；匿名身份固定返回 403
- `GET /api/v1/workspace/overview` — 当前租户的业务指标与最近任务聚合
- `/api/v1/collections` — demo tenant 集合 CRUD
- `/api/v1/documents` — 流式上传、筛选与 cursor 分页
- `/api/v1/documents/{id}` — 文档详情与幂等删除
- `GET /api/v1/documents/{id}/pipeline`、`/pipeline/roots/{root_id}` — 租户隔离的处理链路、Root/Leaf 与清洗 audit
- `GET /api/v1/documents/{id}/llm-cleaning/preflight`、`POST /api/v1/documents/{id}/llm-cleaning` — 一次远程清洗预检、明确确认、重切分与索引重建
- `GET /api/v1/ingestion-jobs`、`GET /api/v1/ingestion-jobs/{id}` — 摄取任务 cursor 列表、筛选与详情
- `POST /api/v1/queries`、`POST /api/v1/queries/stream` — 同步与 SSE 查询契约；未注入 QueryRunner 的当前启动入口会返回 503
- `GET /api/v1/traces`、`/api/v1/traces/query`、`/api/v1/traces/ingestion` — 租户内 Trace 筛选与 cursor 分页；Query 列表支持 mode/status/degraded
- `GET /api/v1/traces/query/{trace_id}` — 已净化的 Query 瀑布、排名变化、Recovery 和降级投影
- `/api/v1/evaluations/catalog`、`/api/v1/evaluations/runs`、`/api/v1/evaluations/compare` — 预算预检、租户评测历史、报告与受控比较
- `GET /api/v1/traces/{trace_id}` — 阶段耗时、候选排名、分数和降级详情
- `GET /health/live`、`GET /health/ready`、`GET /health/doctor` — 存活、就绪和已净化 Provider 诊断
- `GET /api/v1/admin/providers`、`POST /api/v1/admin/providers/select` — 系统管理员读取当前 Provider 注册表、可选 Embedding/Reranker/Vision/Sparse profile，并保存重启生效的选择
- `GET /api/v1/workspace/mcp` — 当前组合根提供的 MCP Server、6 个只读 Tool、4 类 Resource 和 stdio/HTTP 传输状态
- `GET /metrics` — Prometheus text exposition；生产环境必须使用独立 Bearer token

stdio MCP Server 使用官方 Python SDK v2，提供 6 个只读知识 Tool 与 4 类租户隔离的 Resource。先在你自己的组合模块中构造 `MCPServer`，再显式配置 factory 启动：

```bash
ENTERPRISE_RAG_MCP_STDIO_FACTORY=your_package.bootstrap:build_mcp_server \
  uv run --project backend enterprise-rag-mcp-stdio
```

factory 必须是无参数函数并返回 `MCPServer`。当前仓库尚未提供默认生产 Provider 组合，因此不会使用测试数据伪装可用服务；`backend/tests/fixtures/mcp_stdio_server.py` 仅用于真实 SDK 子进程契约测试。stdio 的 stdout 专供 JSON-RPC，业务日志必须写 stderr。

Streamable HTTP MCP 由组合根调用 `build_http_mcp_app(...)` 创建，固定服务路径为 `/mcp`。Mac runtime
已经复用 REST 查询的同一个 `KnowledgeApplication`，并连接真实 PostgreSQL Catalog、当前 Dense/Sparse
Provider 和 Milvus revision；不会用 fixture 冒充服务：

```bash
export MCP_PUBLIC_BASE_URL='https://rag.example.com'
export MCP_TOKEN_PEPPER='replace-with-at-least-32-random-bytes'
uv run --project backend alembic -c backend/alembic.ini upgrade head
uv run --project backend uvicorn enterprise_rag.mac_runtime:app
```

公网 `MCP_PUBLIC_BASE_URL` 必须使用 HTTPS；只有 development/test 组合允许明文 loopback HTTP。客户端必须
发送 `Authorization: Bearer <token>`。Token 只以 pepper-HMAC 保存，绑定 active tenant、active actor、
Tool scopes 和非空 collection allowlist；数据库从不保存 raw token。当前由上述可信 CLI 完成签发、列表和
撤销，匿名 demo 用户不能调用该 operator 边界；M9-03 才会增加系统管理员界面。

默认后端入口把应用日志写成单行 JSON，包含环境、request/trace/span/tenant correlation 和稳定事件字段。HTTP 接受 W3C `traceparent`；Query、Standard RAG 阶段和 Ingestion 阶段已接入 OpenTelemetry。配置 PostgreSQL 时，FastAPI 默认组合有界内存 Exporter 和 PostgreSQL Trace Store；自定义部署仍可向 `create_app`、`KnowledgeApplication` 和 `IngestionPipeline` 注入 SDK `TracerProvider`/`TraceService`。日志和 Trace 不记录 query string、请求体、原始问题、Root 正文、Prompt、Authorization、Cookie 或密钥。Trace 写入失败不会改变业务结果。

写请求必须先调用 `GET /api/v1/auth/me`，保留响应 Cookie，并把响应中的 `csrf_token` 放入 `X-CSRF-Token` header。开发环境的 HTTP Cookie 不设置 Secure；生产环境或 HTTPS base URL 强制设置 Secure。

在第二个终端启动前端：

```bash
pnpm --dir=frontend dev
```

Vite 会把 `/api` 与 `/health` 同源代理到 `127.0.0.1:8000`。当前前端包含响应式 Shell、
完整路由表、匿名 session、管理员登录、system route guard、公共 SSE 问答、租户总览、
Collection/Document 管理、摄取任务监控、Query Trace 瀑布/排名/Recovery 检查器、Ingestion
Trace 阶段/批次/稳定错误检查器、MCP 能力目录，以及预算评测中心。
匿名用户无需登录即可进入 `/workspace/*`；`/workspace/overview` 会读取当前 tenant 的集合、文档、
索引、24 小时 Query 与最近任务聚合，并并列显示 `/health/doctor` 的 Provider 状态；
`/workspace/documents` 支持集合 CRUD、筛选、上传、详情和安全删除，`/workspace/ingestion` 展示
后台任务真实进度；`/admin/providers` 展示实时 Provider 注册表并提供 Embedding/Reranker/Vision/Sparse 选择，其他 `/admin/*`
仍需系统管理员身份。Embedding 切换属于 restart-bound 的索引契约：重启 Mac backend 后，管理员在
`/admin/providers` 的“Embedding 索引兼容状态”面板中可以看到当前 revision、每个文档的 Root/Leaf/vector
数量与旧 revision，并点击“重建不兼容文档”。重建先在新 Milvus revision 投影向量，成功写入 PostgreSQL
Root/Leaf 后才删除旧 revision；投影或数据库交换失败时保留旧索引。Reranker 切换不需要重建向量。
兼容判断同时核对 PostgreSQL Root revision、Leaf 数量与当前 revision 的 Milvus 向量数量；即使 Root
已经标记为当前 revision，只要向量缺失或数量不一致也会重建，不会错误跳过。Provider 选择重启生效后，
前端只显示当前运行实例，不再把已生效的同一模型继续标成 pending。
对应接口为 `GET /api/v1/admin/providers/index-status` 与 `POST /api/v1/admin/providers/reindex`。
`/workspace/mcp` 使用后端共享的 SDK 注册定义展示 Tool 名称、只读标记、所需 Scope、Resource URI/template
和当前传输状态。Mac API 现在会显示 Streamable HTTP 为 `mounted` 并给出
`http://127.0.0.1:8000/mcp`；其他未注入 HTTP factory 的组合仍如实显示“需要外部组合”。该页面不返回
Token、Prompt、Authorization 或文档正文。
若需要重新生成锁定的 OpenAPI 类型：

```bash
pnpm --dir=frontend generate:api
```

管理员首次登录使用 `ADMIN_BOOTSTRAP_EMAIL` 和 `ADMIN_BOOTSTRAP_PASSWORD`。本地开发默认是
账号 `admin`、密码 `admin`；只有尚未存在系统管理员时才会在 PostgreSQL 中创建 bootstrap 账号，密码以
Argon2id 保存，创建后可从环境中移除 bootstrap password。生产环境会拒绝这组弱凭据，必须显式配置强凭据。

数据库迁移、匿名 session、集合/文档 API、查询预算和集成测试需要 PostgreSQL；若未配置数据库、对象目录或 session secret，静态 OpenAPI 仍完整可用，但业务路由返回稳定 503。M4 已完成 QueryRunner、同步/SSE、检索和 Agentic RAG 的可组合契约与服务；当前 `enterprise_rag.main:app` 尚未注入具体 QueryRunner，所以查询端点会稳定返回 503，生产 Provider 组合与进程入口会在后续里程碑接入。

Milvus Lite 通过 PyMilvus 嵌入运行，无需启动独立服务。契约测试会创建隔离的临时 `.db` 文件；运行数据应放在已忽略的 `data/runtime/` 下，不得提交到 Git。同一个 Milvus Lite 文件只能由一个应用进程打开。

本地对象存储同样不需要独立服务。默认目录为 `data/runtime/object-store`；内容先流式写入私有临时文件，仅在大小和 SHA-256 校验通过后可见。用户文件名只作为元数据，绝不作为文件系统路径。

## 配置后端

配置使用以下确定性优先级，由低到高：

```text
代码默认值 < YAML 文件 < 环境变量 < 测试或启动时显式覆盖
```

默认开发配置无需文件或密钥即可启动。如需载入仓库中的示例 YAML：

```bash
ENTERPRISE_RAG_CONFIG_FILE=config/development.example.yaml \
  uv run --project backend uvicorn enterprise_rag.main:app --reload
```

`.env.example` 列出了所有支持的部署变量，其中的值均为不可用占位符。仅在本地复制编辑，生成的 `.env` 必须保持未跟踪，并显式载入：

```bash
cp .env.example .env
uv run --project backend uvicorn enterprise_rag.main:app --reload --env-file .env
```

支持 `DATABASE_URL`、`SESSION_SECRET`、`METRICS_TOKEN`、`LLM_API_KEY` 等扁平部署变量。普通配置也可使用嵌套名称覆盖，例如 `ENTERPRISE_RAG__DEEP__LOW_THRESHOLD=0.50`。

生产环境缺少必要密钥、阈值非法、YAML 格式错误或配置了未知 Provider 时，应用会在提供服务前拒绝启动。密钥使用遮蔽类型，校验错误详情中不会包含密钥值。

## 运行本地质量门禁

后端：

```bash
docker compose -f infra/compose/compose.dev.yml up -d postgres
export TEST_DATABASE_URL=postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test
(cd backend && uv run --no-env-file ruff check src tests migrations)
(cd backend && uv run --no-env-file mypy src tests migrations)
(cd backend && uv run --no-env-file pytest -q)
uv build --project backend
```

`--no-env-file` 很重要：`uv run` 默认会自动读取仓库根目录的 `.env`；质量门禁必须避免本地 LLM
密钥和开发数据库配置改变“未配置应用”测试的语义。PostgreSQL 集成测试只通过显式
`TEST_DATABASE_URL` 获取测试连接。

前端：

```bash
pnpm --dir=frontend test
pnpm --dir=frontend check:api
pnpm --dir=frontend typecheck
pnpm --dir=frontend build
```

完整浏览器验收（首次运行会下载固定 Playwright 镜像）：

```bash
docker compose -p enterprise-rag-browser-e2e -f infra/compose/compose.e2e.yml \
  up --build --abort-on-container-exit --exit-code-from e2e
docker compose -p enterprise-rag-browser-e2e -f infra/compose/compose.e2e.yml \
  down --volumes --remove-orphans
```

每个 GitHub Pull Request 都会执行相同命令。合并前必须通过 `backend-quality`、`frontend-quality`
和 `browser-e2e` 检查。

## 贡献流程

1. 每个验收 Slice 创建一个独立分支。
2. 在同一 PR 中增加或更新验收测试、实现和必要文档。
3. 每个 PR 都检查并同步维护中英文 README，确保启动命令与里程碑状态准确。
4. 推送分支并使用仓库模板创建 PR。
5. 所有必需检查通过后，只使用 Squash Merge 合并。
6. 删除已合并分支，从最新 `main` 开始下一个 Slice。

分支保护禁止直接推送或强制推送到 `main`。

## 当前里程碑

- M1-01 详细开发规格：已完成
- M1-02 可运行 Monorepo 骨架：已完成
- M1-03 CI 与 main 分支保护：已完成
- M1-04 配置与密钥校验：已完成
- M1-05 通用插件契约与注册表：已完成
- M1-06 不可变领域类型与统一错误：已完成
- M2-01 PostgreSQL Schema、Alembic 与异步 Repository 基线：已完成
- M2-02 并发安全的摄取任务状态机：已完成
- M2-03 Milvus Lite 向量存储适配器：已完成
- M2-04 崩溃安全的本地对象存储：已完成
- M2-05 文档注册与并发安全去重：已完成
- M2-06 异步删除 Saga 与跨存储 Reconcile：已完成
- M2 存储与生命周期里程碑：已完成
- M3-01 PDF/OCR Loader：已完成
- M3-02 DOCX/HTML/TXT/Markdown Loader：已完成
- M3-03 XLSX/XLS/CSV Loader：已完成
- M3-04 确定性 Cleaner 与审计报告：已完成
- M3-05 Root/Leaf 结构化 Splitter：已完成
- M3-06 图片存储、Vision 端口与 caption 降级：已完成
- M3-07 本地多语与 OpenAI-compatible Embedding Providers：已完成
- M3-08 Sparse 编码与可补偿 Projection：已完成
- M3-09 可恢复摄取 Pipeline：已完成
- M3-10 匿名工作区与文档 API：已完成
- M3 多格式摄取流水线里程碑：已完成
- M4-01 Dense/Sparse 双路 Search：已完成
- M4-02 多路/多 query RRF：已完成
- M4-03 本地/HTTP/Noop Reranker 与安全降级：已完成
- M4-04 Scope/Root 权限过滤与恢复：已完成
- M4-05 结构化 QueryPlan 与确定性降级：已完成
- M4-06 Standard 显式状态图：已完成
- M4-07 Deep Evidence Ledger 与 Recovery：已完成
- M4-08 Answer Verify/Repair/Abstain：已完成
- M4-09 Query REST/SSE API：已完成
- M4-10 PostgreSQL Cost Guard 与 Provider timeout/retry：已完成
- M4 Hybrid Retrieval 与 Agentic RAG 里程碑：已完成
- M5-01 MCP Application Layer：已完成
- M5-02 stdio MCP：已完成
- M5-03 HTTP MCP：已完成
- M5-04 Trace/Logging：已完成
- M5-05 Trace Persistence：已完成
- M5-06 Metrics/Health：已完成
- M5 MCP 与全链路可观测性里程碑：已完成
- M6-01 Evaluator Contracts：已完成
- M6-02 Golden Set：已完成
- M6-03 Eval Runner：已完成
- M6-04 LLM Judge：已完成
- M6-05 CI Quality Gate：已完成
- M6-06 Public Benchmark Adapter：已完成
- M6 评测闭环与公开 Benchmark Adapter 里程碑：已完成
- M7-01 Shell/Auth：已完成
- M7-02 Public Chat：已完成
- M7-03 Overview：已完成
- M7-04 Documents/Ingestion：已完成
- M7-05 Query Trace：已完成
- M7-06 Ingestion Trace：已完成
- M7-07 Evaluation UI：已完成
- M7-08 Browser E2E：已完成
- M7 Vue3/TypeScript 公共端与管理端里程碑：已完成
- M7-R1 macOS 真实 Provider 开发组合：已完成
- M7-R2A 文档处理透视：已完成
- M7-R2B 查询计划与逐阶段召回指标：已完成
- M7-R2C 人工触发的一次 LLM 清洗：已完成
- M7-R3 开发/测试数据库隔离、向量修复工具与 LLM Query Planner：已完成
- M7-R4 真实 Deep Recovery 与引用核验/修复：已完成
- M7-R5 Provider 切换后的索引兼容状态与安全重建：已完成
- M7-R6 MCP 能力目录与传输状态可视化：已完成
- M7-R7 Provider 重建一致性与失败证明：已完成
- M7-R8 Mac Streamable HTTP MCP 真实组合：已完成
- M7-R9 Milvus 原生 BM25 Sparse：已完成
- M7-R10 Provider 失败路径与重建投影完整性：已完成
- M7-R11 真实 OpenAI-compatible Vision Provider 与目录选择：已完成
- 下一项：M8 公网发布

查询应用层现在提供共享 `QueryRunner` 契约上的同步 REST 与流式 SSE 接口。匿名会话可以执行 Standard/Deep 查询，但租户与调用者身份始终由服务端绑定。SSE 使用稳定的 accepted/progress/heartbeat/completed/error 事件协议；断线会取消执行，错误会被净化，未配置 Runner 时会在发送流响应头之前返回 503。

`/chat` 公共问答页已接入该 SSE 契约，支持 Standard/Deep、Collection 范围、公开阶段状态、
可展开引用、主动停止、429 `Retry-After` 和有边界拒答。断线会保留 Query ID，不会自动无限
重连；公共页面不展示内部 diagnostics 或隐藏推理。默认 `enterprise_rag.main:app` 仍未组合生产
QueryRunner；M7-08 的 `enterprise_rag.local_runtime:app` 提供离线确定性验收，M7-R1 的
`enterprise_rag.mac_runtime:app` 则组合本地语义 Embedding、多语 Reranker 和 OpenAI-compatible
LLM，供 Mac 完整开发演示。两者都不是 M8 生产入口。

`/workspace/overview` 通过 tenant-scoped 聚合接口展示 Collection、文档状态、Root/Leaf、过去
24 小时 Query/P95/错误率与最近摄取/评测任务，并同时读取 doctor 的六类 Provider 能力位。
空工作区、未注册 Provider 和 null 指标均保持真实语义；loading、empty、degraded、error/retry
拥有独立状态，错误可携带 Request ID。匿名用户看不到 VPS 资源或跨租户运行数据。

`/workspace/documents` 现在支持 Collection 新建/编辑/名称确认删除、seed 保护、Document cursor
筛选、multipart 上传、详情和幂等删除；`/workspace/ingestion` 提供任务状态筛选、阶段、进度、
attempt、heartbeat 与稳定错误，只在存在活跃任务时轮询。匿名 `demo_operator` 可完成单租户业务
旅程，但系统路由和跨租户资源仍由前后端双重拒绝。离线 Compose 组合包含实际摄取 Worker；默认
组合根仍要求部署方显式提供 Worker 进程。

`/workspace/documents/{document_id}/pipeline` 是 PostgreSQL 事实驱动的文档处理检查器。它展示版本实际
使用的 Parser/Cleaner/Splitter 及切分参数，以分页 Root 列表和按需详情呈现 raw/clean 全文对照、每条
确定性清洗规则的发生次数与前后 hash、Leaf 文本、检索增强文本、token、offset 和边界。新摄取的 Leaf 不重叠，Root
负责检索后的完整上下文恢复。
接口和页面都由当前 session tenant 限定；旧版本缺少新增 metadata 时只显示“未记录”，不会伪造默认值。

`/workspace/traces/queries` 展示当前 tenant 的持久化 Query Trace，可按 Standard/Deep、结果和降级
状态筛选。详情使用后端净化投影显示原查询/改写/intent/子查询、每个分支的 Dense/Sparse 请求与返回、
两路交集、RRF 输入/去重/Root 配额/Top-K、权限过滤、Rerank、Root 恢复、LLM usage、全链路耗时瀑布、
Dense/Sparse→RRF→Rerank 排名变化、Deep Recovery 轮次与稳定降级组件。多分支候选表显示各方法最佳
名次；缺失的旧遥测保持空值，不由浏览器推断。查询正文仅通过 tenant-scoped 专用 Trace 投影返回；
通用日志、Prometheus、Prompt、证据正文、异常堆栈和隐藏推理仍不包含该数据。单次运行指标不得解释为
Recall@K/MRR/NDCG，真正质量指标需使用评测中心带 gold 的 Dataset Run。

`/workspace/traces/ingestion` 展示当前 tenant 的持久化 Ingestion Trace，可按成功、失败、等待重试
和取消筛选。详情使用后端净化投影展示 Worker 实际执行的阶段瀑布、Root/Leaf 与投影校验数量，以及
staging/activation 的真实 VectorStore 批次；失败任务只展示稳定错误码并可返回对应任务。接口和页面
不返回文件正文、对象路径、异常消息/堆栈或 lease owner，旧 Trace 缺失批次时保持诚实空状态。

`/workspace/evaluations` 提供服务端目录、执行前预算、逐 Case 进度、持久化历史、完整报告和受控比较。
当前公开 Profile 使用 30 条版本化 Golden Case、真实本地 Hashing Sparse Encoder 与确定性指标，
估算 LLM 调用为 0，并明确标为 smoke 而非在线生成质量结论。同一 tenant 只允许一个活跃 Run；只有
Dataset、Mode、Case 集、Index、Prompt 与 Provider 快照完整且一致的成功 Run 才计算 Candidate −
Base，否则页面展示后端返回的具体不可比较原因。报告可导出 JSON 或 Markdown。

Cost Guard 在 QueryRunner 进入任何 Provider 逻辑前，通过 PostgreSQL 条件 UPSERT 原子预留分钟 Query 名额和最坏调用/token 额度。分钟限额按匿名 session 隔离，UTC 日额度由所有匿名 session 共享；Standard/Deep 使用不同权重。成功后按可信 usage 退回未使用额度，异常或无法验证的 usage 保守扣除预留，429 同时返回 `Retry-After`。LLM 装饰器提供可配置单次超时、仅瞬时错误的有界重试和 retry count。新增数据库表需要先执行 README 上方的 `alembic upgrade head`。

`KnowledgeApplication` 现已成为 HTTP、MCP 与后续 CLI 的唯一查询用例入口，统一负责服务端身份绑定、Query ID、同步执行和 SSE 流。stdio Adapter 基于官方 MCP SDK v2 暴露 6 个只读 Tool，以及 collection/document/section Resource；所有身份均由进程端绑定。Tool 同时返回人类可读内容和结构化结果，错误经过净化。入口强制 stdout 只承载 JSON-RPC，并由真实 SDK 客户端子进程测试覆盖 list/call/read 与缓冲输出隔离。

Streamable HTTP Adapter 在 `/mcp` 强制 Bearer Token，并在协议分发前校验连接 scope。Token 原文不会入库；请求身份从不可变的认证 claims 建立，Tool scope 与 collection allowlist 只能继续收窄。公网组合拒绝 HTTP，并启用 Host/Origin 防护。当前 Token 管理仍属于后续系统管理员里程碑，匿名 demo 全业务权限不包含 Token、密钥和系统配置权限。

可观测性基线使用 task-local correlation context、JSON Lines 日志和 OpenTelemetry spans。HTTP 上游 trace context 会向 Query 与 RAG 阶段传播，异步并发不会串 tenant/query/job；Standard 与 Ingestion 的主要阶段均有独立子 span。遥测采用字段 allow-list，敏感属性会被拒绝，异常消息不会自动写入 span。

Trace 现在以 `trace_runs`/`trace_spans` 按 tenant 持久化，同步 Query、SSE Query 和 Ingestion 在根 span 结束后以幂等 upsert 落库。Dense/Sparse、RRF 和 Rerank 候选以有界 event 保存，可重建 Leaf/Root 排名与分数变化；降级摘要只由稳定 `*_degraded` 字段派生。匿名用户可读取 demo tenant 全部 Trace，跨租户 trace ID 统一返回 404。新环境或旧环境更新后都需先执行上方 `alembic upgrade head`。

Prometheus 指标使用应用内独立 Registry，覆盖 HTTP、Query、Retrieval/Recovery、Provider/Token、Ingestion、Milvus、Evaluation 和 Rate Limit。HTTP label 只记录路由模板，不使用原始 path 或 tenant/user/document/query ID。`live` 不依赖外部服务；`ready` 的 required 配置、PostgreSQL 或 Provider 失败时返回 503；`doctor` 只返回稳定状态码和公开 Provider 元数据。

评测领域现在拥有与传输层、数据库和具体检索实现解耦的不可变 Case、运行观测、指标结果与可插拔 Evaluator 契约。内置确定性评测器计算 Document/Root Recall@5、Root MRR@10、引用覆盖率、严格原文引用有效率和拒答准确率，并为无 Gold、无引用回答与正确拒答定义稳定的 `null`/`0` 语义。评测器同时声明支持指标与预计 LLM 成本，后续 Runner 可在运行前执行预算控制。

首版 Golden Set 位于 `evals/golden/v1`：30 条人工改写 Case 按中文/英文各 15 条组织，并精确覆盖关键词、语义改写、比较、metadata scope、表格、OCR 和不可回答问题。版本化 manifest、JSON Schema 与严格 Loader 会校验未知字段、schema revision、重复或悬空 ID、collection scope、期望事实原文及分类/语言数量，防止评测输入静默漂移。

Eval Runner 会在运行前按 Case 数和组件能力声明估算最坏 LLM 调用，超过预算时零 Provider 调用直接拒绝。配置、数据 revision、commit、Subject/Evaluator 版本共同形成稳定 hash；只有完整成功结果才按 request hash 缓存。以下零成本命令生成 Runner 机制验收报告；输出会明确标记 `golden-fixture-oracle`，不能作为产品检索准确率：

```bash
cd backend
uv run enterprise-rag-eval \
  --manifest ../evals/golden/v1/manifest.yaml \
  --report ../artifacts/evals/local-smoke.json \
  --commit-sha "$(git rev-parse HEAD)"
```

LLM Judge 是默认关闭的可选 Adapter，单独计算 faithfulness/relevancy，不覆盖确定性指标。启用时其最坏调用会进入 Eval Runner 预算；缺少凭证时状态为 unavailable、不会注册 Judge，零成本 deterministic eval 仍可完整运行。Judge 只接受严格的两字段 JSON 分数，异常输出会使该评测失败而不是被伪装成低分。开发配置示例：

```yaml
evaluation:
  max_cases: 30
  max_llm_calls: 30
  llm_judge_enabled: true
  llm_judge_max_output_tokens: 128
```

Required CI 中的零成本质量门禁会用真实 `HashingSparseEncoder` 跑完 30 条 Golden Case，并同时检查绝对阈值与相对基线最大 0.02 回归。它属于 required `backend-quality`，失败会阻止 main 合并。可在本地执行同一命令：

```bash
cd backend
uv run enterprise-rag-quality-gate \
  --manifest ../evals/golden/v1/manifest.yaml \
  --policy ../evals/quality-gate-v1.yaml \
  --report ../artifacts/evals/ci-smoke.json \
  --commit-sha "$(git rev-parse HEAD)"
```

MultiDoc2Dial Adapter 与产品 Domain/摄取链路隔离。官方下载会校验 HTTPS host、8MB 上限和固定 SHA-256；归档及输出只进入 Git 忽略的 `artifacts/`。先运行 sample 验证环境，确认数据集使用条款后再运行 full：

```bash
cd backend
uv run enterprise-rag-benchmark download \
  --output ../artifacts/benchmarks/multidoc2dial.zip

uv run enterprise-rag-benchmark convert \
  --archive ../artifacts/benchmarks/multidoc2dial.zip \
  --mode sample --max-cases 100 \
  --commit-sha "$(git rev-parse HEAD)" \
  --checkpoint ../artifacts/benchmarks/sample.checkpoint.json \
  --output ../artifacts/benchmarks/sample.json \
  --report ../artifacts/benchmarks/sample-report.json
```

全量转换将 `--mode sample --max-cases 100` 替换为 `--mode full`，并使用独立的 checkpoint/output/report 路径。只有 full 报告会标记 `is_full_dataset=true`；转换报告本身不含模型或索引结果，因此不能作为产品 Benchmark 成绩。

本地可直接访问 `http://127.0.0.1:8000/metrics`。生产环境必须配置 `METRICS_TOKEN`，抓取时发送：

```bash
curl -H "Authorization: Bearer $METRICS_TOKEN" https://your-host.example/metrics
```

PostgreSQL 任务 Repository 已实现入队、独占租约、启动、心跳、重试、取消、成功和超期租约回收。Worker 使用 owner 字符串标识自身并续租限时 lease；过期或错误 owner 的更新会被拒绝。进度只能单调增加，重试不超过 `max_attempts`，并发 Worker 通过 `FOR UPDATE SKIP LOCKED` 确保同一任务只能被一个 Worker 领取。

VectorStore 端口要求每条记录和每次检索都携带 index revision。Milvus Collection 按 revision 隔离，避免混用不同 Embedding 维度。每个检索表达式都会强制注入 `tenant_id` 和 `status == "ready"`；可选 Collection、Document 范围只能收窄该强制过滤。契约测试在真实 Milvus Lite 文件上验证 dense/sparse 向量、标量过滤、幂等 upsert、计数、版本删除、持久化和关闭行为。

ObjectStore 端口接收异步字节流，并使用规范 SHA-256 键发布不可变对象。本地适配器支持上传大小限制、调用方摘要校验、完整内容 `fsync` 和不覆盖已有对象的原子发布。中断或被拒绝的上传会清除 `.part` 文件；路径穿越、绝对路径、格式错误、摘要前缀不匹配和符号链接逃逸都会在文件系统访问前被拒绝。

文档注册先将字节流写入 ObjectStore，再开启 PostgreSQL 工作单元。去重身份为 `(tenant_id, collection_id, sha256)`：相同范围的重复内容返回已有 document/version；不同 Collection 或租户拥有独立逻辑资源，同时安全复用不可变物理对象。同一逻辑名出现新 hash 时创建新版本。PostgreSQL transaction advisory lock 会串行化内容键和逻辑名竞争，复合主键与唯一约束作为最终完整性防线。该能力已由 `POST /api/v1/documents` 暴露，并在同一事务创建或复用摄取任务。

应用错误的显式详情保持深度不可变；异常对象本身不使用 frozen dataclass，因为 Python 在异常穿过异步事务上下文时必须写入 traceback 状态。

删除请求会立即让租户所属文档退出 `ready`、清空 active version、取消摄取任务，并创建或复用一个 delete job。Worker 通过可重入 Saga 依次清理 Milvus、PostgreSQL 内容和无引用对象文件，最后保存 document/version tombstone 并完成任务。只要仍有非 deleted version 引用，共享内容寻址文件就会保留。

Reconcile 将 Milvus version projection 和本地对象键与 PostgreSQL 事实源比较，同时发现超期 Worker lease。默认模式只读；apply 模式仅删除已确认的孤儿向量/文件并回收 lease。缺失文件和向量数量不一致会保留为未解决项，因为当前存储阶段尚无 Loader 或 Embedding 可用于重建。对应 HTTP 和 CLI 入口会在后续 API/CLI Slice 中实现。

M7-R7～R11 已补齐 Provider 重建一致性、真实 Mac Streamable HTTP MCP、Milvus 原生 BM25、远程 Provider 失败路径以及真实 OpenAI-compatible Vision。重启不会自动清除 Milvus 持久化 collection；若 PostgreSQL 与 Milvus 的事实边界不一致，先停止 API，再使用按 tenant/version 定向的 reconcile 或安全重建，禁止删除整个 Milvus 文件。

M1～M7 已完成（52/64 Slice）。仓库目前提供经过测试的工程基座、完整多格式摄取链路、匿名 demo tenant 的集合/文档 HTTP API、Hybrid Retrieval/Agentic RAG 服务、MCP、Trace/Metrics/Health、EDD 评测闭环、公开 Benchmark Adapter、完整 Vue3/TypeScript 工作区，以及 Compose 中可复现的浏览器全旅程。M7-R1 另提供不计入 64 个发布 Slice 的 Mac 真实 Provider 开发组合；生产镜像、进程和公网部署仍属于 M8，因此默认入口不会把离线验收或 Mac 开发组合冒充生产服务。

PDF Loader 会流式落盘临时输入，先按页提取文本，低于 `pdf_ocr_min_chars` 时使用 Tesseract `chi_sim+eng` OCR。输出保留 1-based 页码、提取方式和内嵌图片的媒体类型、尺寸、内容 hash 与字节数据，供图片增强阶段使用。空白页不会生成空 Root；全空、加密、损坏、类型不匹配和 OCR 语言缺失均返回稳定错误，成功和失败路径都会清理临时文件。Loader 已接入后台摄取 Pipeline；HTTP 上传接口在 M3-10 交付。

文本类 Loader 支持 DOCX、HTML、TXT 和 Markdown。DOCX 按标题生成 Section Root，将表格规范化为 Markdown，并保留内嵌图片原始内容；HTML 移除脚本、样式、导航和嵌入对象，将正文结构转为 Markdown，同时只记录外部图片地址而不发起网络请求；TXT/Markdown 严格接受 UTF-8。这些 Loader 已接入 Cleaner、Splitter 和持久化流水线。

表格类 Loader 分别解析 XLSX、旧 XLS 和 CSV。每个 worksheet 单独生成带表头的行块，续块重复表头并保留源行号；空白外围被裁剪，公式缓存值和公式表达式均可追踪。CSV 默认只接受 UTF-8/UTF-8-SIG，遗留编码必须通过 `csv_fallback_encoding` 显式指定。这些 Loader 已接入完整后台流水线。

确定性 Cleaner 同时保留原文和清洗文本，并为每项实际变更记录规则、次数及前后内容 hash。它处理不可见控制字符、常见 OCR 异常和空白，并通过批量 Root 统计移除重复页眉页脚；清洗会隔离 fenced code，不改代码缩进、空行和跨行内容。相同文本重复清洗不会继续变化，默认不会使用 LLM 改写文档。人工触发的 LLM 清洗只作为版面修复补充：允许修复 PDF/OCR 的空白、段落换行、标题/表格间距和跨行断词，但受后端词法、顺序、数字、事实及代码围栏校验保护。

结构化 Splitter 使用版本化的段落/句子感知策略：在 Root 内优先保留标题、段落、列表、代码围栏、表格行和完整句子，再应用 target/max 限制；新摄取的 Leaf 不重叠，Root 在检索后负责恢复完整上下文。只有单个结构单元本身超过预算时才降级为 token 硬切，并在 Leaf metadata 标记 `boundary=token_limit_hard_cut`、`hard_cut=true`。macOS 真实运行时直接复用 FastEmbed tokenizer 与模型输入上限，实际安全预算为配置上限和 `model_input_limit - 1` 的较小值；每个 Root/Leaf 都保存实际 tokenizer、预算、边界和硬切计数，前端可逐块核对。表格续块会重复表头且计入 token 上限；Root/Leaf ID 对相同 version、index revision、内容和顺序保持稳定。

图片增强服务先把 Loader 提取的原始图片写入内容寻址 ObjectStore，再调用可插拔 Vision 端口。默认 `vision: none` 会保留图片并跳过 caption；`openai_compatible` 通过 `/chat/completions` 发送 text + `data:image/*;base64,...` 多模态请求，严格要求一个非空字符串 caption，并对 429/5xx/传输错误执行最多配置次数的退避重试。Vision 异常只将 caption 标记为降级，不会丢弃已存图片或泄露供应商错误；Root metadata 记录图片尺寸、MIME、hash、对象键、caption 状态和错误码，供 Pipeline Inspector 核对实际结果。ObjectStore 写入失败仍会中止摄取，因为图片持久化不是可选数据。

Pipeline Inspector 的 `IMAGE ENRICHMENT` 面板直接读取当前 Root 的持久化事实，展示图片数量、页码/序号/名称、MIME、尺寸、SHA-256、ObjectStore key、Caption、`created/skipped/degraded` 状态、实际 Vision Provider/Model 及状态计数。它还逐一用已保存 Leaf 的 `retrieval_text` 核对 Caption 是否真的进入检索，并明确标出没有进入的情况；页面不预览原始图片，也不根据当前配置补造历史结果。

Embedding 端口提供本地多语和 OpenAI-compatible 两种实现，并通过 `EMBEDDING_MODEL` 选择 FastEmbed profile。本地默认使用 `paraphrase-multilingual-MiniLM-L12-v2`（384 维、mean pooling、registry 描述为 512 input tokens），首次调用会下载约 0.22GB 模型；运行时不会盲信 registry：优先读取实际 truncation limit；如果 FastEmbed ONNX wrapper 不暴露该字段，则用超出 registry 上限的探测文本调用 `token_count`，识别被 tokenizer 截断后的真实上限（当前缓存模型实际报告 128）。可选 `BAAI/bge-small-zh-v1.5` profile 实测为 512 维、512 input tokens，适合中文本地部署；切换时同时设置 `ENTERPRISE_RAG__INGESTION__EMBEDDING_DIMENSION=512`，并通过新 index revision 隔离旧向量。Provider 暴露真实 tokenizer 的 `count_tokens` 与输入上限，拒绝达到模型上限的单项输入，Splitter 与 Embedding 使用同一个计数器。远程实现具有条数/token 双重批处理、超时、限流和 5xx 有界重试；SiliconFlow `BAAI/bge-m3` profile 固定校验 1024 维并采用官方 8192 token 上限，但因为 HTTP API 不暴露 tokenizer，本地计数明确标记为 deterministic estimate。两者都严格校验数量、顺序、维度及有限数，并输出 L2 归一化向量。真实模型可用以下命令单独验证：

```bash
(cd backend && RUN_MODEL_TESTS=1 uv run pytest -q \
  tests/contract/test_embedding_providers.py -m model)
```

Sparse 端口有两种明确模式：离线/评测的 `hashing_lexical` 用稳定多语词法 hash、log-TF 和 L2 归一化
生成预计算向量；Mac 完整语义组合的 `milvus_builtin_bm25` 只传递文本，由 Milvus 原生 BM25 Function
和 `jieba` Analyzer 维护语料 TF/IDF。两者都不把一种模式伪装成另一种；`IndexSchema` 将 sparse mode
纳入 revision，旧 Hashing collection 与 BM25 collection 不能混用。Projection Service 按模式分批写入
`processing`，核对数量后激活为 `ready` 并再次核对。重复运行覆盖相同 Leaf ID；部分写入或核验失败只清理
本次目标 revision，不会误删同一版本仍在工作的旧 revision。文档删除/全量 reconcile 仍可按版本清理所有
revision。

Provider Reindex Service 将旧 PostgreSQL Root/Leaf 和旧 Milvus projection 视为可回滚事实：先按当前
Embedding tokenizer 重新切分并投影新 revision，再通过 ProjectionResult 和独立的
`count_by_version_revision(tenant, version, target_revision)` 双重核验，之后才在事务中交换 Root/Leaf，最后
按旧 revision 定向清理。即使投影适配器静默返回成功但没有写满向量，也只会清理目标 revision，旧 Root/Leaf
与旧 revision 的真实查询保持不变；数据库交换失败同样不会破坏旧索引。系统管理员状态页因此能区分“模型已
切换但索引尚未重建”和“当前 revision 已完整可检索”，不会把旧向量伪装成新模型结果。远程 Embedding 与
Reranker 的 transport 异常使用有界重试并返回净化错误，Reranker 身份校验失败时回退到有限的 RRF 顺序。

摄取 Pipeline 在文档注册事务内创建或复用 Job，按 Loader → 图片增强 → Cleaner → Splitter → PostgreSQL → Milvus → 最终提交的顺序运行。每个 checkpoint 同时续租、更新单调进度并确认取消；确定性输入错误直接失败，瞬时错误按上限重试。失败或取消会补偿该版本的 PostgreSQL 内容和 Milvus 投影，只有双存储核验完成后文档才进入 `ready`。服务层通过 `run_once(owner=...)` 驱动；M7-08 离线组合已提供单进程轮询 Worker，M8 仍需交付生产进程与资源约束。

匿名工作区 API 使用服务端 session 将所有请求强制绑定到固定 demo tenant。匿名 `demo_operator` 拥有该租户内的集合和文档管理权限，但不能进入系统管理面；写操作需要轮换的 CSRF token。管理员使用独立数据库 session、Argon2id 密码与系统角色，前端路由守卫只改善体验，后端仍会对每个系统请求鉴权。集合 CRUD、流式上传、文档 cursor 分页、详情、任务查询和幂等异步删除均使用统一错误模型与 request ID，跨租户 ID 一律表现为 404。

双路 Search Service 对每个 query 分别生成 Dense 与 Sparse 输入；Hashing 产生 query vector，BM25 产生
query text，并并行调用两条独立检索路径。tenant 与授权 collection/document scope 在调用 VectorStore
前写入两路请求，Milvus 再强制追加 `status=ready`；任何 scope 都不能在召回后补过滤。两路原始分数保持
独立并附带最小诊断，融合由 M4-02 负责；Query Trace 会显示每个分支实际使用的 Sparse 算法。

RRF Fusion 按每个 query 的 Dense/Sparse 排名列表计算 `Σ 1/(k+rank)`，不直接混加不可比较的原始分数。同一 Leaf 跨分路去重，同一 Root 默认最多保留 3 个 Leaf，全局默认保留 30 个；完全同分使用 Leaf ID 稳定排序，并报告各类配额丢弃数量。

Reranker 端口提供本地 FastEmbed CrossEncoder、HTTP 和显式 Noop 三种实现。默认 `local_cross_encoder` 使用约 0.08GB 的 `Xenova/ms-marco-MiniLM-L-6-v2`，该默认模型只按英文能力声明；中文或多语场景必须显式选择对应模型，2GB 生产服务器可选择 SiliconFlow `BAAI/bge-reranker-v2-m3`，通过官方 `/v1/rerank` 请求 `model/query/documents/top_n`。服务默认重排前 20 个 RRF 候选并选择 8 个，严格按候选 ID 对齐；超时、坏响应、重复/未知 ID 和非有限分数都会净化诊断并降级为稳定的 RRF 选择。真实本地模型可单独验证：

```bash
(cd backend && RUN_MODEL_TESTS=1 uv run pytest -q \
  tests/contract/test_reranker_providers.py -m model)
```

Scope/Root 服务把服务端授权边界与用户的 metadata 条件解析为 PostgreSQL 中当前明确的 ready document ID 集合。匿名用户仍拥有 demo tenant 全部业务权限，但不能通过请求覆盖 tenant；受限身份按获准 Collection/Document 取并集。title、organization、media type、active version UUID 和 section 均在事实源中校验，显式矛盾返回不泄露资源存在性的 `QUERY_SCOPE_CONFLICT`。召回 Leaf 在进入 Reranker 前、selected Root 在进入上下文前都会再次联表检查 tenant、active collection、ready document 和 indexed active version，因此旧向量、删除中或未授权内容会被丢弃。恢复内容默认严格限制为 18,000 字符，同 Root 合并 Leaf 引用并记录确定性截断。

Query Planning Service 将结构化 Planner 输出视为不可信输入，严格校验字段、intent、子查询/需求数量、UUID 和 Scope 收窄关系。模型不能改变 Standard/Deep mode，不能凭空加入 Collection/Document ID，也不能覆盖调用方显式 metadata。Mac 组合通过当前 OpenAI-compatible LLM 执行查询改写与 1～4 路分解，并独立记录 Planner token/call；任何坏响应或 Provider 故障都会整体降级为确定性计划：保留原 Scope，识别中英文比较、流程和总结意图，拆分多条件，并使用最近一条 user 历史补足指代。Planner 的供应商异常不会进入 QueryPlan。

Standard Query Graph 使用显式状态机串联 Plan → Search → RRF → PostgreSQL Authorize → Rerank → Root Recover → Answer。每次运行返回真实状态转移；RRF、授权 Leaf 或二次校验 Root 为空都会进入 NoResults 并跳过答案模型。Standard 固定把 Planner 尝试计为第 1 次 LLM 调用、最终答案计为第 2 次并执行硬上限；Planner 降级不触发额外调用。未分类故障进入带净化错误码的 Failed 状态，不把异常文本交给客户端。

Deep Recovery 使用按 Leaf ID 跨轮去重的 Evidence Ledger，并给 Recovery 新证据预留最终名额。通用 Controller 默认以 0.45/0.80 双阈值决定直接恢复、调用 Assessor 或直接回答；Mac 真实组合采用更严格的 `always_assess` 策略，对每次有证据的 Deep 决策调用当前 LLM，模型只判断 requirement 覆盖、冲突和决策，最终分数仍由覆盖率与检索置信度计算。默认最多两轮，仍有缺口则 Abstain。四条恢复路径为 Rewrite Hybrid、HyDE Dense-only、Exact-term Sparse-only 和仅放宽 Planner 新增且调用方未指定字段的 Scope repair；每条路径都重新执行检索、RRF、权限校验、Rerank 与 Root 恢复。Mac 完整组合的精确术语路径使用真实 Milvus BM25 Provider；离线公开评测仍使用 Hashing Lexical 获得零成本确定性分数，两种模式均不会互相冒充。

Answer Verification 要求每个事实段落绑定引用；引用 Root 必须来自本轮授权上下文、Leaf 必须属于该 Root，quote 必须是 Root clean text 中真实存在的连续片段，同时覆盖 QueryPlan 的全部 requirements。结构或覆盖错误最多 Repair 一次，并使用完全相同的证据集合再次校验；Evidence conflict 不会通过改写掩盖，而是直接 Abstain。最终只有验证通过的答案会生成带 document/root/Leaf、page/section、quote 和 score 的领域 Citation；其余返回有边界拒答和空引用，不泄露供应商错误。

所有后续适配器都实现通用 `Provider` 生命周期契约，并由应用级注册表统一持有。Provider 键为 `(kind, name)`；重复注册、未知名称、能力缺失和资源关闭失败都会产生稳定且已净化的错误。

Root 和 Leaf ID 由不可变身份字段与内容 hash 派生。使用同一 index revision 重新处理同一版本会产生相同 ID；内容、序号、类型或 index revision 改变时 ID 也会改变。领域时间必须是带时区的 UTC，metadata 会被复制为深度不可变结构，`to_dict()` 输出与 JSON 兼容的 API 值。

M1 通过独立检查的 PR 交付：[开发规格 #1](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/1)、[匿名演示边界 #2](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/2)、[Monorepo #3](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/3)、[CI 与分支保护 #4](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/4)、[配置 #5](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/5)、[插件注册表 #6](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/6) 和 [领域类型 #7](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/7)。
