"""Immutable, validated settings models."""

from typing import Annotated, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, model_validator

PositiveInt = Annotated[int, Field(gt=0)]
Probability = Annotated[float, Field(ge=0.0, le=1.0)]


class SettingsModel(BaseModel):
    """Common strict and immutable settings behavior."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ApplicationSettings(SettingsModel):
    environment: Literal["development", "test", "production"] = "development"
    public_base_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8000")


class ProviderSettings(SettingsModel):
    llm: str = "openai_compatible"
    embedding: str = "local_multilingual_minilm"
    reranker: str = "local_mmarco"
    vector_store: str = "milvus_lite"
    splitter: str = "structure_aware"
    evaluator: str = "deterministic"
    ocr: str = "tesseract"


class IngestionSettings(SettingsModel):
    max_upload_bytes: PositiveInt = 104_857_600
    allowed_suffixes: tuple[str, ...] = (
        ".pdf",
        ".docx",
        ".xlsx",
        ".xls",
        ".csv",
        ".html",
        ".htm",
        ".txt",
        ".md",
    )
    target_tokens: PositiveInt = 350
    max_tokens: PositiveInt = 480
    overlap_tokens: Annotated[int, Field(ge=0)] = 50
    max_attempts: PositiveInt = 3
    pdf_ocr_min_chars: Annotated[int, Field(ge=0, le=10_000)] = 20
    pdf_render_scale: Annotated[float, Field(ge=1.0, le=4.0)] = 2.5
    pdf_ocr_languages: tuple[str, ...] = ("chi_sim", "eng")

    @model_validator(mode="after")
    def validate_token_window(self) -> "IngestionSettings":
        if self.target_tokens > self.max_tokens:
            raise ValueError("target_tokens must not exceed max_tokens")
        if self.overlap_tokens >= self.target_tokens:
            raise ValueError("overlap_tokens must be lower than target_tokens")
        if not self.allowed_suffixes or any(
            not value.startswith(".") for value in self.allowed_suffixes
        ):
            raise ValueError("allowed_suffixes must contain dot-prefixed suffixes")
        if not self.pdf_ocr_languages or any(
            not value.strip() or "+" in value for value in self.pdf_ocr_languages
        ):
            raise ValueError("pdf_ocr_languages must contain individual language names")
        return self


class RetrievalSettings(SettingsModel):
    dense_candidates: PositiveInt = 40
    sparse_candidates: PositiveInt = 40
    fused_candidates: PositiveInt = 30
    rerank_candidates: PositiveInt = 20
    selected_leaf_k: PositiveInt = 8
    rrf_k: PositiveInt = 60
    max_parent_chars: PositiveInt = 18_000

    @model_validator(mode="after")
    def validate_candidate_funnel(self) -> "RetrievalSettings":
        largest_source = max(self.dense_candidates, self.sparse_candidates)
        if self.fused_candidates > largest_source:
            raise ValueError("fused_candidates must fit within a source candidate set")
        if self.rerank_candidates > self.fused_candidates:
            raise ValueError("rerank_candidates must not exceed fused_candidates")
        if self.selected_leaf_k > self.rerank_candidates:
            raise ValueError("selected_leaf_k must not exceed rerank_candidates")
        return self


class DeepSettings(SettingsModel):
    max_recovery_rounds: Annotated[int, Field(ge=0, le=10)] = 2
    max_answer_repairs: Annotated[int, Field(ge=0, le=5)] = 1
    low_threshold: Probability = 0.45
    high_threshold: Probability = 0.80

    @model_validator(mode="after")
    def validate_threshold_order(self) -> "DeepSettings":
        if self.low_threshold >= self.high_threshold:
            raise ValueError("low_threshold must be lower than high_threshold")
        return self


class SecuritySettings(SettingsModel):
    anonymous_demo_full_access: bool = True
    anonymous_demo_tenant_slug: Annotated[str, Field(min_length=1)] = "demo"
    anonymous_api_requests_per_minute: PositiveInt = 10
    anonymous_queries_per_minute: PositiveInt = 5
    anonymous_daily_llm_calls: PositiveInt = 500
    anonymous_max_file_bytes: PositiveInt = 20_971_520
    anonymous_max_ready_documents: PositiveInt = 20
    session_minutes: PositiveInt = 480


class ObservabilitySettings(SettingsModel):
    capture_query_text: bool = True
    capture_document_text: bool = False
    trace_retention_days: PositiveInt = 30


class CredentialSettings(SettingsModel):
    """Values supplied by environment in production; secrets stay masked."""

    database_url: SecretStr | None = None
    session_secret: SecretStr | None = None
    admin_bootstrap_email: str | None = None
    admin_bootstrap_password: SecretStr | None = None
    llm_base_url: AnyHttpUrl | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    embedding_base_url: AnyHttpUrl | None = None
    embedding_api_key: SecretStr | None = None
    embedding_model: str | None = None
    rerank_base_url: AnyHttpUrl | None = None
    rerank_api_key: SecretStr | None = None
    rerank_model: str | None = None
    mcp_token_pepper: SecretStr | None = None


class AppSettings(SettingsModel):
    """Complete configuration snapshot passed into the composition root."""

    app: ApplicationSettings = ApplicationSettings()
    providers: ProviderSettings = ProviderSettings()
    ingestion: IngestionSettings = IngestionSettings()
    retrieval: RetrievalSettings = RetrievalSettings()
    deep: DeepSettings = DeepSettings()
    security: SecuritySettings = SecuritySettings()
    observability: ObservabilitySettings = ObservabilitySettings()
    credentials: CredentialSettings = CredentialSettings()
