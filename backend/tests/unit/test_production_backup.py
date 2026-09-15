from pathlib import Path
import subprocess
from typing import Any

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_ROOT = REPOSITORY_ROOT / "infra" / "production"


def test_backup_script_defines_encrypted_snapshot_and_empty_target_restore() -> None:
    script = REPOSITORY_ROOT / "scripts" / "production-backup.sh"
    result = subprocess.run(
        ["bash", "-n", str(script)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    text = script.read_text(encoding="utf-8")
    for required in (
        "set -Eeuo pipefail",
        "BACKUP_AGE_RECIPIENT",
        "BACKUP_AGE_IDENTITY",
        "MILVUS_BACKUP_HOOK",
        "pg_dump",
        "pg_restore",
        "sha256sum",
        "enterprise-rag-object-archive create",
        "enterprise-rag-object-archive restore",
        "enterprise-rag-reconcile",
        "--no-deps",
        "restore-drill",
    ):
        assert required in text
    assert "docker compose down --volumes" not in text
    assert "DROP DATABASE" not in text


def test_backup_workflow_is_manual_approved_and_cannot_restore_live_root() -> None:
    path = REPOSITORY_ROOT / ".github" / "workflows" / "backup.yml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    workflow = dict(workflow)
    trigger: dict[str, Any] = workflow.get("on") or workflow[True]
    inputs = trigger["workflow_dispatch"]["inputs"]
    assert inputs["action"]["options"] == ["backup", "restore-drill"]
    assert inputs["confirmation"]["required"] is True
    job = workflow["jobs"]["backup"]
    assert job["environment"]["name"] == "production"
    assert workflow["concurrency"]["cancel-in-progress"] is False

    text = path.read_text(encoding="utf-8")
    for required in (
        "DEPLOY_SSH_PRIVATE_KEY",
        "DEPLOY_KNOWN_HOSTS",
        "StrictHostKeyChecking=yes",
        "BACKUP_RESTORE_PATH",
        "RESTORE_PATH must differ from DEPLOY_PATH",
        "production-backup.sh",
    ):
        assert required in text


def test_production_object_archive_and_reconcile_are_packaged_in_backend_image() -> None:
    pyproject = (REPOSITORY_ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'enterprise-rag-object-archive = "enterprise_rag.cli.object_archive:main"' in pyproject
    assert 'enterprise-rag-reconcile = "enterprise_rag.cli.reconcile:main"' in pyproject

    dockerfile = (PRODUCTION_ROOT / "backend.Dockerfile").read_text(encoding="utf-8")
    assert "COPY --from=build /build/backend/src /app/backend/src" in dockerfile
