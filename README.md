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
DATABASE_URL=postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test \
  uv run --project backend alembic -c backend/alembic.ini upgrade head
```

在第一个终端启动后端：

```bash
uv run --project backend uvicorn enterprise_rag.main:app --reload
```

开发 API 位于 `http://127.0.0.1:8000`。当前骨架提供：

- `GET /` — 服务名称、版本、配置状态和当前环境
- `GET /docs` — 交互式 OpenAPI 文档
- `GET /openapi.json` — OpenAPI Schema

在第二个终端启动前端：

```bash
pnpm --dir frontend dev
```

Vite 开发服务器会输出本地访问地址。当前页面用于确认 Vue 3 和 TypeScript 应用已成功挂载。

数据库迁移和集成测试需要 PostgreSQL。当前 HTTP 骨架尚未访问数据库；模型 API、鉴权及完整 RAG 行为会在各自验收 PR 中逐步加入。

Milvus Lite 通过 PyMilvus 嵌入运行，无需启动独立服务。契约测试会创建隔离的临时 `.db` 文件；运行数据应放在已忽略的 `data/runtime/` 下，不得提交到 Git。同一个 Milvus Lite 文件只能由一个应用进程打开。

本地对象存储同样不需要独立服务。应用代码应使用 `data/runtime/objects` 下的目录；内容先流式写入私有临时文件，仅在大小和 SHA-256 校验通过后可见。用户文件名只作为元数据，绝不作为文件系统路径。

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
pnpm --dir frontend test
pnpm --dir frontend typecheck
pnpm --dir frontend build
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
- 下一项：M3-02 DOCX/HTML/TXT/Markdown Loader

PostgreSQL 任务 Repository 已实现入队、独占租约、启动、心跳、重试、取消、成功和超期租约回收。Worker 使用 owner 字符串标识自身并续租限时 lease；过期或错误 owner 的更新会被拒绝。进度只能单调增加，重试不超过 `max_attempts`，并发 Worker 通过 `FOR UPDATE SKIP LOCKED` 确保同一任务只能被一个 Worker 领取。

VectorStore 端口要求每条记录和每次检索都携带 index revision。Milvus Collection 按 revision 隔离，避免混用不同 Embedding 维度。每个检索表达式都会强制注入 `tenant_id` 和 `status == "ready"`；可选 Collection、Document 范围只能收窄该强制过滤。契约测试在真实 Milvus Lite 文件上验证 dense/sparse 向量、标量过滤、幂等 upsert、计数、版本删除、持久化和关闭行为。

ObjectStore 端口接收异步字节流，并使用规范 SHA-256 键发布不可变对象。本地适配器支持上传大小限制、调用方摘要校验、完整内容 `fsync` 和不覆盖已有对象的原子发布。中断或被拒绝的上传会清除 `.part` 文件；路径穿越、绝对路径、格式错误、摘要前缀不匹配和符号链接逃逸都会在文件系统访问前被拒绝。

文档注册先将字节流写入 ObjectStore，再开启 PostgreSQL 工作单元。去重身份为 `(tenant_id, collection_id, sha256)`：相同范围的重复内容返回已有 document/version；不同 Collection 或租户拥有独立逻辑资源，同时安全复用不可变物理对象。同一逻辑名出现新 hash 时创建新版本。PostgreSQL transaction advisory lock 会串行化内容键和逻辑名竞争，复合主键与唯一约束作为最终完整性防线。该能力目前位于应用服务层，HTTP 上传端点会在对应 API Slice 中实现。

应用错误的显式详情保持深度不可变；异常对象本身不使用 frozen dataclass，因为 Python 在异常穿过异步事务上下文时必须写入 traceback 状态。

删除请求会立即让租户所属文档退出 `ready`、清空 active version、取消摄取任务，并创建或复用一个 delete job。Worker 通过可重入 Saga 依次清理 Milvus、PostgreSQL 内容和无引用对象文件，最后保存 document/version tombstone 并完成任务。只要仍有非 deleted version 引用，共享内容寻址文件就会保留。

Reconcile 将 Milvus version projection 和本地对象键与 PostgreSQL 事实源比较，同时发现超期 Worker lease。默认模式只读；apply 模式仅删除已确认的孤儿向量/文件并回收 lease。缺失文件和向量数量不一致会保留为未解决项，因为当前存储阶段尚无 Loader 或 Embedding 可用于重建。对应 HTTP 和 CLI 入口会在后续 API/CLI Slice 中实现。

M1 和 M2 已完成。产品级摄取与查询行为尚未实现。仓库目前提供经过测试的工程基座，以及 PostgreSQL 生命周期状态、并发安全任务与文档注册、Milvus Lite Projection、崩溃安全本地对象、幂等删除和跨存储 Reconcile。

PDF Loader 会流式落盘临时输入，先按页提取文本，低于 `pdf_ocr_min_chars` 时使用 Tesseract `chi_sim+eng` OCR。输出保留 1-based 页码、提取方式和内嵌图片的媒体类型、尺寸、内容 hash 与字节数据，供后续图片存储 Slice 使用。空白页不会生成空 Root；全空、加密、损坏、类型不匹配和 OCR 语言缺失均返回稳定错误，成功和失败路径都会清理临时文件。当前能力位于 Loader Adapter，尚未接入完整摄取 Pipeline 或 HTTP 上传接口。

所有后续适配器都实现通用 `Provider` 生命周期契约，并由应用级注册表统一持有。Provider 键为 `(kind, name)`；重复注册、未知名称、能力缺失和资源关闭失败都会产生稳定且已净化的错误。

Root 和 Leaf ID 由不可变身份字段与内容 hash 派生。使用同一 index revision 重新处理同一版本会产生相同 ID；内容、序号、类型或 index revision 改变时 ID 也会改变。领域时间必须是带时区的 UTC，metadata 会被复制为深度不可变结构，`to_dict()` 输出与 JSON 兼容的 API 值。

M1 通过独立检查的 PR 交付：[开发规格 #1](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/1)、[匿名演示边界 #2](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/2)、[Monorepo #3](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/3)、[CI 与分支保护 #4](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/4)、[配置 #5](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/5)、[插件注册表 #6](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/6) 和 [领域类型 #7](https://github.com/Dkpeng605/enterprise-agentic-rag-v6/pull/7)。
