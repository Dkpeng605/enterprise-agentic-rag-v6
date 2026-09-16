"""Backward-compatible import surface for the standalone Worker runtime.

The production Worker composition lives in :mod:`enterprise_rag.production_worker`.
This module remains as a compatibility import for local operators and integrations
that used the earlier module name; it deliberately does not define a second
pipeline or vector-store composition.
"""

from enterprise_rag.production_worker import (
    ProductionWorkerRuntime,
    build_production_worker,
    run_worker,
)

__all__ = [
    "ProductionWorkerRuntime",
    "build_production_worker",
    "run_worker",
]
