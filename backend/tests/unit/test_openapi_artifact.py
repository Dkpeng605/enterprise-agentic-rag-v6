import json
from pathlib import Path

from enterprise_rag.api import create_app

REPOSITORY_ROOT = Path(__file__).parents[3]


def test_committed_frontend_openapi_artifact_matches_fastapi() -> None:
    expected = json.dumps(create_app().openapi(), ensure_ascii=False, indent=2) + "\n"

    assert (REPOSITORY_ROOT / "frontend" / "openapi.json").read_text(
        encoding="utf-8"
    ) == expected
