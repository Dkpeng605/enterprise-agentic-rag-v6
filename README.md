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
export DATABASE_URL=postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test
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
- `/api/v1/collections` — demo tenant 集合 CRUD
- `/api/v1/documents` — 流式上传、筛选与 cursor 分页
- `/api/v1/documents/{id}`、`/api/v1/ingestion-jobs/{id}` — 文档和摄取状态
- `POST /api/v1/queries`、`POST /api/v1/queries/stream` — 同步与 SSE 查询契约；未注入 QueryRunner 的当前启动入口会返回 503

stdio MCP Server 使用官方 Python SDK v2，提供 6 个只读知识 Tool 与 4 类租户隔离的 Resource。先在你自己的组合模块中构造 `MCPServer`，再显式配置 factory 启动：

```bash
ENTERPRISE_RAG_MCP_STDIO_FACTORY=your_package.bootstrap:build_mcp_server \
  uv run --project backend enterprise-rag-mcp-stdio
```

factory 必须是无参数函数并返回 `MCPServer`。当前仓库尚未提供默认生产 Provider 组合，因此不会使用测试数据伪装可用服务；`backend/tests/fixtures/mcp_stdio_server.py` 仅用于真实 SDK 子进程契约测试。stdio 的 stdout 专供 JSON-RPC，业务日志必须写 stderr。

Streamable HTTP MCP 由生产组合调用 `build_http_mcp_app(...)` 创建，固定服务路径为 `/mcp`：

```bash
export MCP_TOKEN_PEPPER='replace-with-at-least-32-random-bytes'
uv run --project backend alembic -c backend/alembic.ini upgrade head
uv run --project backend uvicorn your_package.bootstrap:mcp_app
```

公网 `public_base_url` 必须使用 HTTPS；只有本地测试组合可以显式设置 `allow_insecure_http=True`。客户端必须发送 `Authorization: Bearer <token>`。Token 只以 pepper-HMAC 保存，绑定 tenant、actor、Tool scopes 和 collection allowlist；匿名 demo 用户不能签发或管理 Token。M9-03 才会提供系统管理员签发/撤销界面，因此当前需由受信任的部署/bootstrap 流程写入 `api_tokens`，仓库不会提供默认 Token。

默认后端入口把应用日志写成单行 JSON，包含环境、request/trace/span/tenant correlation 和稳定事件字段。HTTP 接受 W3C `traceparent`；Query、Standard RAG 阶段和 Ingestion 阶段已接入 OpenTelemetry。应用只创建 spans，不默认把 Trace 发到外部服务；生产组合可向 `create_app`、`KnowledgeApplication` 和 `IngestionPipeline` 注入 SDK `TracerProvider`，M5-05 会增加 PostgreSQL Trace Exporter。日志/span 不记录 query string、请求体、原始问题、Root 正文、Prompt、Authorization、Cookie 或密钥。

写请求必须先调用 `GET /api/v1/auth/me`，保留响应 Cookie，并把响应中的 `csrf_token` 放入 `X-CSRF-Token` header。开发环境的 HTTP Cookie 不设置 Secure；生产环境或 HTTPS base URL 强制设置 Secure。

在第二个终端启动前端：

```bash
pnpm --dir=frontend dev
```

Vite 开发服务器会输出本地访问地址。当前页面用于确认 Vue 3 和 TypeScript 应用已成功挂载。

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

支持 `DATABASE_URL`、`SESSION_SECRET`、`LLM_API_KEY` 等扁平部署变量。普通配置也可使用嵌套名称覆盖，例如 `ENTERPRISE_RAG__DEEP__LOW_THRESHOLD=0.50`。

生产环境缺少必要密钥、阈值非法、YAML 格式错误或配置了未知 Provider 时，应用会在提供服务前拒绝启动。密钥使用遮蔽类型，校验错误详情中不会包含密钥值。

## 运行本地质量门禁

后端：

```bash
docker compose -f infra/compose/compose.dev.yml up -d postgres
export TEST_DATABASE_URL=postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test
(cd backend && uv run ruff check src tests migrations)
(cd backend && uv run mypy src tests migrations)
(cd backend && uv run pytest -q)
uv build --project backend
```

前端：

```bash
pnpm --dir=frontend test
pnpm --dir=frontend typecheck
pnpm --dir=frontend build
```

每个 GitHub Pull Request 都会执行相同命令。合并前必须通过 `backend-quality` 和 `frontend-quality` 两项检查。

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
- 下一项：M5-05 Trace Persistence

查询应用层现在提供共享 `QueryRunner` 契约上的同步 REST 与流式 SSE 接口。匿名会话可以执行 Standard/Deep 查询，但租户与调用者身份始终由服务端绑定。SSE 使用稳定的 accepted/progress/heartbeat/completed/error 事件协议；断线会取消执行，错误会被净化，未配置 Runner 时会在发送流响应头之前返回 503。

Cost Guard 在 QueryRunner 进入任何 Provider 逻辑前，通过 PostgreSQL 条件 UPSERT 原子预留分钟 Query 名额和最坏调用/token 额度。分钟限额按匿名 session 隔离，UTC 日额度由所有匿名 session 共享；Standard/Deep 使用不同权重。成功后按可信 usage 退回未使用额度，异常或无法验证的 usage 保守扣除预留，429 同时返回 `Retry-After`。LLM 装饰器提供可配置单次超时、仅瞬时错误的有界重试和 retry count。新增数据库表需要先执行 README 上方的 `alembic upgrade head`。

`KnowledgeApplication` 现已成为 HTTP、MCP 与后续 CLI 的唯一查询用例入口，统一负责服务端身份绑定、Query ID、同步执行和 SSE 流。stdio Adapter 基于官方 MCP SDK v2 暴露 6 个只读 Tool，以及 collection/document/section Resource；所有身份均由进程端绑定。Tool 同时返回人类可读内容和结构化结果，错误经过净化。入口强制 stdout 只承载 JSON-RPC，并由真实 SDK 客户端子进程测试覆盖 list/call/read 与缓冲输出隔离。

Streamable HTTP Adapter 在 `/mcp` 强制 Bearer Token，并在协议分发前校验连接 scope。Token 原文不会入库；请求身份从不可变的认证 claims 建立，Tool scope 与 collection allowlist 只能继续收窄。公网组合拒绝 HTTP，并启用 Host/Origin 防护。当前 Token 管理仍属于后续系统管理员里程碑，匿名 demo 全业务权限不包含 Token、密钥和系统配置权限。

可观测性基线使用 task-local correlation context、JSON Lines 日志和 OpenTelemetry spans。HTTP 上游 trace context 会向 Query 与 RAG 阶段传播，异步并发不会串 tenant/query/job；Standard 与 Ingestion 的主要阶段均有独立子 span。遥测采用字段 allow-list，敏感属性会被拒绝，异常消息不会自动写入 span。

PostgreSQL 任务 Repository 已实现入队、独占租约、启动、心跳、重试、取消、成功和超期租约回收。Worker 使用 owner 字符串标识自身并续租限时 lease；过期或错误 owner 的更新会被拒绝。进度只能单调增加，重试不超过 `max_attempts`，并发 Worker 通过 `FOR UPDATE SKIP LOCKED` 确保同一任务只能被一个 Worker 领取。

VectorStore 端口要求每条记录和每次检索都携带 index revision。Milvus Collection 按 revision 隔离，避免混用不同 Embedding 维度。每个检索表达式都会强制注入 `tenant_id` 和 `status == "ready"`；可选 Collection、Document 范围只能收窄该强制过滤。契约测试在真实 Milvus Lite 文件上验证 dense/sparse 向量、标量过滤、幂等 upsert、计数、版本删除、持久化和关闭行为。

ObjectStore 端口接收异步字节流，并使用规范 SHA-256 键发布不可变对象。本地适配器支持上传大小限制、调用方摘要校验、完整内容 `fsync` 和不覆盖已有对象的原子发布。中断或被拒绝的上传会清除 `.part` 文件；路径穿越、绝对路径、格式错误、摘要前缀不匹配和符号链接逃逸都会在文件系统访问前被拒绝。

文档注册先将字节流写入 ObjectStore，再开启 PostgreSQL 工作单元。去重身份为 `(tenant_id, collection_id, sha256)`：相同范围的重复内容返回已有 document/version；不同 Collection 或租户拥有独立逻辑资源，同时安全复用不可变物理对象。同一逻辑名出现新 hash 时创建新版本。PostgreSQL transaction advisory lock 会串行化内容键和逻辑名竞争，复合主键与唯一约束作为最终完整性防线。该能力已由 `POST /api/v1/documents` 暴露，并在同一事务创建或复用摄取任务。

应用错误的显式详情保持深度不可变；异常对象本身不使用 frozen dataclass，因为 Python 在异常穿过异步事务上下文时必须写入 traceback 状态。

删除请求会立即让租户所属文档退出 `ready`、清空 active version、取消摄取任务，并创建或复用一个 delete job。Worker 通过可重入 Saga 依次清理 Milvus、PostgreSQL 内容和无引用对象文件，最后保存 document/version tombstone 并完成任务。只要仍有非 deleted version 引用，共享内容寻址文件就会保留。

Reconcile 将 Milvus version projection 和本地对象键与 PostgreSQL 事实源比较，同时发现超期 Worker lease。默认模式只读；apply 模式仅删除已确认的孤儿向量/文件并回收 lease。缺失文件和向量数量不一致会保留为未解决项，因为当前存储阶段尚无 Loader 或 Embedding 可用于重建。对应 HTTP 和 CLI 入口会在后续 API/CLI Slice 中实现。

M1、M2、M3 和 M4 已完成。仓库目前提供经过测试的工程基座、完整多格式摄取链路、匿名 demo tenant 的集合/文档 HTTP API、Hybrid Retrieval/Agentic RAG 服务、同步/SSE 查询契约、PostgreSQL Cost Guard、生命周期状态、Milvus Lite Projection、崩溃安全本地对象、幂等删除和跨存储 Reconcile。具体生产 QueryRunner 与 Provider 的组合入口尚未接入，因此当前默认启动入口不会伪装成可用的完整查询产品。

PDF Loader 会流式落盘临时输入，先按页提取文本，低于 `pdf_ocr_min_chars` 时使用 Tesseract `chi_sim+eng` OCR。输出保留 1-based 页码、提取方式和内嵌图片的媒体类型、尺寸、内容 hash 与字节数据，供图片增强阶段使用。空白页不会生成空 Root；全空、加密、损坏、类型不匹配和 OCR 语言缺失均返回稳定错误，成功和失败路径都会清理临时文件。Loader 已接入后台摄取 Pipeline；HTTP 上传接口在 M3-10 交付。

文本类 Loader 支持 DOCX、HTML、TXT 和 Markdown。DOCX 按标题生成 Section Root，将表格规范化为 Markdown，并保留内嵌图片原始内容；HTML 移除脚本、样式、导航和嵌入对象，将正文结构转为 Markdown，同时只记录外部图片地址而不发起网络请求；TXT/Markdown 严格接受 UTF-8。这些 Loader 已接入 Cleaner、Splitter 和持久化流水线。

表格类 Loader 分别解析 XLSX、旧 XLS 和 CSV。每个 worksheet 单独生成带表头的行块，续块重复表头并保留源行号；空白外围被裁剪，公式缓存值和公式表达式均可追踪。CSV 默认只接受 UTF-8/UTF-8-SIG，遗留编码必须通过 `csv_fallback_encoding` 显式指定。这些 Loader 已接入完整后台流水线。

确定性 Cleaner 同时保留原文和清洗文本，并为每项实际变更记录规则、次数及前后内容 hash。它处理不可见控制字符、常见 OCR 异常和空白，并通过批量 Root 统计移除重复页眉页脚。相同文本重复清洗不会继续变化，默认不会使用 LLM 改写文档。

结构化 Splitter 使用版本化的确定性多语 tokenizer，在 Root 内优先尊重标题、段落、列表、代码围栏和表格行边界，再应用 target/max/overlap 限制。表格续块会重复表头且计入 token 上限；Root/Leaf ID 对相同 version、index revision、内容和顺序保持稳定。该轻量 tokenizer 不等同于任何远程模型 tokenizer，后续替换时必须创建新索引 revision。

图片增强服务先把 Loader 提取的原始图片写入内容寻址 ObjectStore，再调用可插拔 Vision 端口。默认 `vision: none` 会保留图片并跳过 caption；Vision 异常只将 caption 标记为降级，不会丢弃已存图片或泄露供应商错误。ObjectStore 写入失败仍会中止摄取，因为图片持久化不是可选数据。

Embedding 端口提供本地多语和 OpenAI-compatible 两种实现。本地默认使用 FastEmbed ONNX 的 `paraphrase-multilingual-MiniLM-L12-v2`（384 维、mean pooling），首次调用会下载约 0.22GB 模型；远程实现具有条数/token 双重批处理、超时、限流和 5xx 有界重试。两者都严格校验数量、顺序、维度及有限数，并输出 L2 归一化向量。真实模型可用以下命令单独验证：

```bash
(cd backend && RUN_MODEL_TESTS=1 uv run pytest -q \
  tests/contract/test_embedding_providers.py -m model)
```

Sparse Encoder 使用稳定的多语词法 hash、log-TF 权重和 L2 归一化生成 Milvus 稀疏向量；它不等同于 BM25。Projection Service 将 Dense/Sparse 结果分批写为 `processing`，核对数量后激活为 `ready` 并再次核对。重复运行覆盖相同 Leaf ID；部分写入或核验失败会删除该版本全部向量，并对删除执行有界重试。

摄取 Pipeline 在文档注册事务内创建或复用 Job，按 Loader → 图片增强 → Cleaner → Splitter → PostgreSQL → Milvus → 最终提交的顺序运行。每个 checkpoint 同时续租、更新单调进度并确认取消；确定性输入错误直接失败，瞬时错误按上限重试。失败或取消会补偿该版本的 PostgreSQL 内容和 Milvus 投影，只有双存储核验完成后文档才进入 `ready`。当前通过服务层 `run_once(owner=...)` 驱动；常驻 Worker 入口将在部署阶段补齐。

匿名工作区 API 使用服务端 session 将所有请求强制绑定到固定 demo tenant。匿名 `demo_operator` 拥有该租户内的集合和文档管理权限，但不能进入系统管理面；写操作需要轮换的 CSRF token。集合 CRUD、流式上传、文档 cursor 分页、详情、任务查询和幂等异步删除均使用统一错误模型与 request ID，跨租户 ID 一律表现为 404。

双路 Search Service 对每个 query 分别生成 Dense 与 Sparse 向量，并并行调用两条独立检索路径。tenant 与授权 collection/document scope 在调用 VectorStore 前写入两路请求，Milvus 再强制追加 `status=ready`；任何 scope 都不能在召回后补过滤。两路原始分数保持独立并附带最小诊断，融合由 M4-02 负责。

RRF Fusion 按每个 query 的 Dense/Sparse 排名列表计算 `Σ 1/(k+rank)`，不直接混加不可比较的原始分数。同一 Leaf 跨分路去重，同一 Root 默认最多保留 3 个 Leaf，全局默认保留 30 个；完全同分使用 Leaf ID 稳定排序，并报告各类配额丢弃数量。

Reranker 端口提供本地 FastEmbed CrossEncoder、HTTP 和显式 Noop 三种实现。默认 `local_cross_encoder` 使用约 0.08GB 的 `Xenova/ms-marco-MiniLM-L-6-v2`，该默认模型只按英文能力声明；中文或多语场景必须显式选择对应模型，2GB 生产服务器应使用远程 Reranker。服务默认重排前 20 个 RRF 候选并选择 8 个，严格按候选 ID 对齐；超时、坏响应、重复/未知 ID 和非有限分数都会净化诊断并降级为稳定的 RRF 选择。真实本地模型可单独验证：

```bash
(cd backend && RUN_MODEL_TESTS=1 uv run pytest -q \
  tests/contract/test_reranker_providers.py -m model)
```

Scope/Root 服务把服务端授权边界与用户的 metadata 条件解析为 PostgreSQL 中当前明确的 ready document ID 集合。匿名用户仍拥有 demo tenant 全部业务权限，但不能通过请求覆盖 tenant；受限身份按获准 Collection/Document 取并集。title、organization、media type、active version UUID 和 section 均在事实源中校验，显式矛盾返回不泄露资源存在性的 `QUERY_SCOPE_CONFLICT`。召回 Leaf 在进入 Reranker 前、selected Root 在进入上下文前都会再次联表检查 tenant、active collection、ready document 和 indexed active version，因此旧向量、删除中或未授权内容会被丢弃。恢复内容默认严格限制为 18,000 字符，同 Root 合并 Leaf 引用并记录确定性截断。

Query Planning Service 将结构化 Planner 输出视为不可信输入，严格校验字段、intent、子查询/需求数量、UUID 和 Scope 收窄关系。模型不能改变 Standard/Deep mode，不能凭空加入 Collection/Document ID，也不能覆盖调用方显式 metadata。任何坏响应或 Provider 故障都会整体降级为确定性计划：保留原 Scope，识别中英文比较、流程和总结意图，拆分多条件，并使用最近一条 user 历史补足指代。Planner 的供应商异常不会进入 QueryPlan。

Standard Query Graph 使用显式状态机串联 Plan → Search → RRF → PostgreSQL Authorize → Rerank → Root Recover → Answer。每次运行返回真实状态转移；RRF、授权 Leaf 或二次校验 Root 为空都会进入 NoResults 并跳过答案模型。Standard 固定把 Planner 尝试计为第 1 次 LLM 调用、最终答案计为第 2 次并执行硬上限；Planner 降级不触发额外调用。未分类故障进入带净化错误码的 Failed 状态，不把异常文本交给客户端。

Deep Recovery 使用按 Leaf ID 跨轮去重的 Evidence Ledger，并给 Recovery 新证据预留最终名额。确定性证据分数在 0.80 及以上直接回答、低于 0.45 直接恢复，中间区间才调用 Evidence Assessor；默认最多两轮，仍有缺口则 Abstain。四条恢复路径为 Rewrite Hybrid、HyDE Dense-only、Exact-term Sparse-only 和仅放宽已证明错误字段的 Scope repair。当前 Sparse 实现是 hashing lexical，不是 BM25，因此代码和文档都不会把精确术语路径虚称为 BM25；未来可替换真正 BM25 Provider。

Answer Verification 要求每个事实段落绑定引用；引用 Root 必须来自本轮授权上下文、Leaf 必须属于该 Root，quote 必须是 Root clean text 中真实存在的连续片段，同时覆盖 QueryPlan 的全部 requirements。结构或覆盖错误最多 Repair 一次，并使用完全相同的证据集合再次校验；Evidence conflict 不会通过改写掩盖，而是直接 Abstain。最终只有验证通过的答案会生成带 document/root/Leaf、page/section、quote 和 score 的领域 Citation；其余返回有边界拒答和空引用，不泄露供应商错误。

所有后续适配器都实现通用 `Provider` 生命周期契约，并由应用级注册表统一持有。Provider 键为 `(kind, name)`；重复注册、未知名称、能力缺失和资源关闭失败都会产生稳定且已净化的错误。

Root 和 Leaf ID 由不可变身份字段与内容 hash 派生。使用同一 index revision 重新处理同一版本会产生相同 ID；内容、序号、类型或 index revision 改变时 ID 也会改变。领域时间必须是带时区的 UTC，metadata 会被复制为深度不可变结构，`to_dict()` 输出与 JSON 兼容的 API 值。

M1 通过独立检查的 PR 交付：[开发规格 #1](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/1)、[匿名演示边界 #2](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/2)、[Monorepo #3](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/3)、[CI 与分支保护 #4](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/4)、[配置 #5](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/5)、[插件注册表 #6](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/6) 和 [领域类型 #7](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/7)。
