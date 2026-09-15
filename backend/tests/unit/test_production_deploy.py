import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_ROOT = REPOSITORY_ROOT / "infra" / "production"


def _workflow() -> dict[Any, Any]:
    path = REPOSITORY_ROOT / ".github" / "workflows" / "deploy.yml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_deploy_workflow_requires_manual_production_approval_and_immutable_release() -> None:
    path = REPOSITORY_ROOT / ".github" / "workflows" / "deploy.yml"
    workflow = _workflow()
    trigger = workflow.get("on")
    if trigger is None:
        trigger = workflow[True]

    assert set(trigger) == {"workflow_dispatch"}
    inputs = trigger["workflow_dispatch"]["inputs"]
    assert inputs["action"]["type"] == "choice"
    assert inputs["action"]["options"] == ["deploy", "rollback"]
    assert inputs["confirmation"]["required"] is True
    assert workflow["permissions"] == {"contents": "read", "packages": "read"}

    job = workflow["jobs"]["deploy"]
    assert job["environment"]["name"] == "production"
    assert workflow["concurrency"]["cancel-in-progress"] is False

    text = path.read_text(encoding="utf-8")
    assert "docker buildx imagetools inspect" in text
    assert "StrictHostKeyChecking=yes" in text
    assert "UserKnownHostsFile" in text
    assert "DEPLOY_SSH_PRIVATE_KEY" in text
    assert "DEPLOY_REGISTRY_TOKEN" in text
    assert "DEPLOY_HOST contains unsupported characters" in text
    assert "DEPLOY_USER contains unsupported characters" in text
    assert "production-deploy.sh" in text
    assert ":latest" not in text


def test_remote_deploy_script_has_backup_migration_smoke_and_safe_rollback_contract() -> None:
    script = REPOSITORY_ROOT / "scripts" / "production-deploy.sh"
    result = subprocess.run(
        ["bash", "-n", str(script)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    text = script.read_text(encoding="utf-8")
    assert "set -Eeuo pipefail" in text
    assert "pg_dump" in text
    assert "--format=custom" in text
    assert "run --rm migrate" in text
    assert "/health/live" in text
    assert "/api/v1/auth/me" in text
    assert "/api/v1/workspace/overview" in text
    assert "/api/v1/auth/login" in text
    assert "previous_backend_image" in text
    assert "rollback" in text
    assert "service_completed_successfully" not in text
    assert "docker compose down" not in text


def test_backend_runtime_image_allows_compose_to_run_migration_and_worker_commands() -> None:
    dockerfile = (PRODUCTION_ROOT / "backend.Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["uvicorn", "enterprise_rag.main:app"' in dockerfile
    assert "ENTRYPOINT" not in dockerfile

    compose = (PRODUCTION_ROOT / "compose.yml").read_text(encoding="utf-8")
    assert 'command: ["alembic", "-c", "/app/backend/alembic.ini", "upgrade", "head"]' in compose
    assert 'command: ["enterprise-rag-worker"]' in compose


def test_production_deploy_documentation_declares_required_environment_contract() -> None:
    for filename in ("README.md", "README.en.md", "infra/README.md"):
        text = (REPOSITORY_ROOT / filename).read_text(encoding="utf-8")
        assert "M8-04" in text
        assert "DEPLOY_HOST" in text
        assert "DEPLOY_SSH_PRIVATE_KEY" in text
        assert "DEPLOY_KNOWN_HOSTS" in text
        assert "rollback" in text.lower()

    spec = (REPOSITORY_ROOT / "DEV_SPEC.md").read_text(encoding="utf-8")
    section = spec[
        spec.index("#### M8-04 Deploy/Rollback") : spec.index("#### M8-05 Backup/Restore")
    ]
    for term in ("Environment", "SSH", "migration", "smoke", "回滚", "PR"):
        assert term in section


def test_remote_deploy_script_records_state_and_swaps_previous_release_on_rollback(
    tmp_path: Path,
) -> None:
    root = tmp_path / "release-root"
    compose_dir = root / "infra" / "production"
    compose_dir.mkdir(parents=True)
    (compose_dir / "compose.yml").write_text(
        (PRODUCTION_ROOT / "compose.yml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (compose_dir / ".env.production").write_text(
        "\n".join(
            [
                "PUBLIC_DOMAIN=staging.example.test",
                "APP_COMMIT_SHA=" + "a" * 40,
                "BACKEND_IMAGE=ghcr.io/example/enterprise-agentic-rag-backend:" + "a" * 40,
                "FRONTEND_IMAGE=ghcr.io/example/enterprise-agentic-rag-frontend:" + "a" * 40,
                "ADMIN_BOOTSTRAP_EMAIL=admin@example.test",
                "ADMIN_BOOTSTRAP_PASSWORD=not-a-real-secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
if [[ " $* " == *" exec -T postgres "* ]]; then
  printf 'deterministic-pg-dump'
fi
if [[ "${FAKE_DOCKER_FAIL_MIGRATE:-0}" == 1 && " $* " == *" run --rm migrate "* ]]; then
  exit 9
fi
""",
        encoding="utf-8",
    )
    (bin_dir / "curl").write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
args=("$@")
for ((index = 0; index < ${#args[@]}; index++)); do
  if [[ "${args[index]}" == --cookie-jar ]]; then
    : > "${args[index + 1]}"
  fi
done
""",
        encoding="utf-8",
    )
    for executable in (bin_dir / "docker", bin_dir / "curl"):
        executable.chmod(0o700)

    release = "b" * 40
    environment = os.environ.copy()
    environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
    environment["DEPLOY_PATH"] = str(root)
    script = REPOSITORY_ROOT / "scripts" / "production-deploy.sh"
    backend = f"ghcr.io/example/enterprise-agentic-rag-backend:{release}"
    frontend = f"ghcr.io/example/enterprise-agentic-rag-frontend:{release}"
    deployed = subprocess.run(
        ["bash", str(script), "deploy", release, backend, frontend],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert deployed.returncode == 0, deployed.stderr
    env_text = (compose_dir / ".env.production").read_text(encoding="utf-8")
    assert f"APP_COMMIT_SHA={release}" in env_text
    assert (root / ".deploy-state").is_file()
    assert list((root / "backups" / "deploy").glob("pre-deploy-*.dump"))

    rolled_back = subprocess.run(
        ["bash", str(script), "rollback"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert rolled_back.returncode == 0, rolled_back.stderr
    env_text = (compose_dir / ".env.production").read_text(encoding="utf-8")
    assert f"APP_COMMIT_SHA={'a' * 40}" in env_text
    assert f"BACKEND_IMAGE=ghcr.io/example/enterprise-agentic-rag-backend:{'a' * 40}" in env_text

    failed_environment = environment.copy()
    failed_environment["FAKE_DOCKER_FAIL_MIGRATE"] = "1"
    failed = subprocess.run(
        [
            "bash",
            str(script),
            "deploy",
            "c" * 40,
            f"ghcr.io/example/enterprise-agentic-rag-backend:{'c' * 40}",
            f"ghcr.io/example/enterprise-agentic-rag-frontend:{'c' * 40}",
        ],
        env=failed_environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert failed.returncode != 0
    env_text = (compose_dir / ".env.production").read_text(encoding="utf-8")
    assert f"APP_COMMIT_SHA={'a' * 40}" in env_text
