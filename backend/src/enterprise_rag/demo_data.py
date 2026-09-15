"""Small, non-secret demo documents used by the public demo workspace."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DemoDocument:
    title: str
    source_name: str
    media_type: str
    content: str


DEMO_DOCUMENTS: tuple[DemoDocument, ...] = (
    DemoDocument(
        "Atlas 发布与回滚手册",
        "atlas-release-handbook.md",
        "text/markdown",
        """# Atlas 发布与回滚手册

## 发布流程

发布前必须确认变更已经通过代码质量门禁，并在 staging 环境完成冒烟测试。
发布负责人创建版本记录，记录提交号、配置 revision 和数据库迁移版本。

生产发布按以下顺序执行：先应用数据库迁移，再发布 API，最后启动后台 Worker。
发布完成后等待所有摄取任务进入 `succeeded`，然后执行一条带引用的知识问答，
并检查 Query Trace 与 Ingestion Trace。

## 回滚条件

如果健康检查连续失败、数据库迁移无法完成、或带引用问答无法通过 Citation Verify，
应停止继续发布。回滚时先停止 Worker，再恢复上一版本 API 和配置，
最后按照备份策略恢复数据库；不得直接删除 Milvus 数据。

## 验收口令

本手册中的演示验收口令是“蓝鲸-7429”，只用于自动化验收数据，不是任何真实系统的凭据。
""",
    ),
    DemoDocument(
        "Atlas 知识库使用与数据规范",
        "atlas-knowledge-policy.md",
        "text/markdown",
        """# Atlas 知识库使用与数据规范

## 支持的文档格式

系统支持 PDF、DOCX、XLSX、XLS、CSV、HTML、TXT 和 Markdown。
PDF 优先提取文本，文本不足时使用中英文 OCR；表格文档会按行块形成 Root，
上下文恢复不会改变原始权限范围。

## 检索流程

查询先生成结构化 QueryPlan，可包含查询改写、Scope 和由 LLM 显式启用的替代检索路径。
每个分支并行执行 Dense 与 Sparse 召回，再经过 RRF 融合、PostgreSQL 权限回源、
Rerank 和 Root 恢复。证据不足时系统应返回有边界的拒答。

## 内容处理

确定性清洗负责统一空白和格式；Leaf 默认不重叠切分，并尊重段落与完整句子。
管理员可以在确认远程处理后请求一次 LLM 清洗，清洗结果必须保留词法顺序、
数字和代码围栏，并重新执行切分与索引。
""",
    ),
)
