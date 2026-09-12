"""Low-cardinality Prometheus metrics with an application-local registry."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest


class ApplicationMetrics:
    """Own metric collectors so tests and application instances never share state."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.http_requests = Counter(
            "http_requests_total",
            "HTTP requests grouped by bounded route and status.",
            ("method", "route", "status"),
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "http_request_duration_seconds",
            "HTTP request duration in seconds.",
            ("method", "route"),
            registry=self.registry,
        )
        self.rag_queries = Counter(
            "rag_queries_total",
            "RAG query outcomes.",
            ("mode", "status"),
            registry=self.registry,
        )
        self.rag_query_duration = Histogram(
            "rag_query_duration_seconds",
            "RAG query duration in seconds.",
            ("mode",),
            registry=self.registry,
        )
        self.rag_retrieval_candidates = Histogram(
            "rag_retrieval_candidates",
            "Candidate counts observed at bounded retrieval stages.",
            ("stage",),
            buckets=(0, 1, 2, 4, 8, 16, 32, 64, 128),
            registry=self.registry,
        )
        self.rag_recovery_rounds = Counter(
            "rag_recovery_rounds_total",
            "Deep recovery rounds by stable route.",
            ("route",),
            registry=self.registry,
        )
        self.provider_requests = Counter(
            "provider_requests_total",
            "Provider request outcomes.",
            ("kind", "provider", "status"),
            registry=self.registry,
        )
        self.provider_duration = Histogram(
            "provider_request_duration_seconds",
            "Provider request duration in seconds.",
            ("kind", "provider"),
            registry=self.registry,
        )
        self.provider_tokens = Counter(
            "provider_tokens_total",
            "Provider tokens by direction.",
            ("direction",),
            registry=self.registry,
        )
        self.ingestion_jobs = Counter(
            "ingestion_jobs_total",
            "Ingestion job outcomes.",
            ("status", "type"),
            registry=self.registry,
        )
        self.ingestion_stage_duration = Histogram(
            "ingestion_stage_duration_seconds",
            "Ingestion stage duration in seconds.",
            ("stage",),
            registry=self.registry,
        )
        self.milvus_operations = Counter(
            "milvus_operations_total",
            "Milvus operation outcomes.",
            ("operation", "status"),
            registry=self.registry,
        )
        self.evaluation_runs = Counter(
            "evaluation_runs_total",
            "Evaluation run outcomes.",
            ("status",),
            registry=self.registry,
        )
        self.rate_limit_rejections = Counter(
            "rate_limit_rejections_total",
            "Rate-limit rejections by stable budget.",
            ("budget",),
            registry=self.registry,
        )

    def observe_http(
        self, *, method: str, route: str, status: int, duration_seconds: float
    ) -> None:
        self.http_requests.labels(method.upper(), route, str(status)).inc()
        self.http_duration.labels(method.upper(), route).observe(max(duration_seconds, 0.0))

    def observe_query(self, *, mode: str, status: str, duration_seconds: float) -> None:
        self.rag_queries.labels(mode, status).inc()
        self.rag_query_duration.labels(mode).observe(max(duration_seconds, 0.0))

    def observe_candidates(self, *, stage: str, count: int) -> None:
        self.rag_retrieval_candidates.labels(stage).observe(max(count, 0))

    def observe_recovery(self, *, route: str) -> None:
        self.rag_recovery_rounds.labels(route).inc()

    def observe_provider(
        self,
        *,
        kind: str,
        provider: str,
        status: str,
        duration_seconds: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        self.provider_requests.labels(kind, provider, status).inc()
        self.provider_duration.labels(kind, provider).observe(max(duration_seconds, 0.0))
        if input_tokens > 0:
            self.provider_tokens.labels("input").inc(input_tokens)
        if output_tokens > 0:
            self.provider_tokens.labels("output").inc(output_tokens)

    def observe_ingestion_job(self, *, status: str, job_type: str) -> None:
        self.ingestion_jobs.labels(status, job_type).inc()

    def observe_ingestion_stage(self, *, stage: str, duration_seconds: float) -> None:
        self.ingestion_stage_duration.labels(stage).observe(max(duration_seconds, 0.0))

    def observe_milvus(self, *, operation: str, status: str) -> None:
        self.milvus_operations.labels(operation, status).inc()

    def observe_evaluation(self, *, status: str) -> None:
        self.evaluation_runs.labels(status).inc()

    def observe_rate_limit(self, *, budget: str) -> None:
        self.rate_limit_rejections.labels(budget).inc()

    def render(self) -> bytes:
        return generate_latest(self.registry)


_METRICS: ContextVar[ApplicationMetrics | None] = ContextVar(
    "enterprise_rag_application_metrics", default=None
)


def current_metrics() -> ApplicationMetrics | None:
    return _METRICS.get()


@contextmanager
def bind_metrics(metrics: ApplicationMetrics | None) -> Iterator[None]:
    if metrics is None:
        yield
        return
    token = _METRICS.set(metrics)
    try:
        yield
    finally:
        _METRICS.reset(token)
