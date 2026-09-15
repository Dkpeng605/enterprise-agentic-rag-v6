from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_main_workflow_publishes_only_immutable_commit_tags_to_ghcr() -> None:
    workflow_path = REPOSITORY_ROOT / ".github" / "workflows" / "images.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    trigger = workflow.get("on")
    if trigger is None:
        trigger = workflow[True]
    assert trigger["push"]["branches"] == ["main"]
    assert workflow["permissions"] == {"contents": "read", "packages": "write"}
    steps = workflow["jobs"]["publish"]["steps"]
    workflow_text = workflow_path.read_text(encoding="utf-8")
    assert "docker/login-action@v3" in workflow_text
    assert "docker/build-push-action@v6" in workflow_text
    assert "platforms: linux/amd64" in workflow_text
    assert "push: true" in workflow_text
    assert "${{ github.sha }}" in workflow_text
    assert ":latest" not in workflow_text
    assert len([step for step in steps if step.get("name", "").startswith("Build and push")]) == 2


def test_runtime_images_carry_revision_and_version_labels() -> None:
    for filename, image_name in (
        ("backend.Dockerfile", "enterprise-agentic-rag-backend"),
        ("frontend.Dockerfile", "enterprise-agentic-rag-frontend"),
    ):
        dockerfile = (
            REPOSITORY_ROOT / "infra" / "production" / filename
        ).read_text(encoding="utf-8")
        assert "ARG VCS_REF=unknown" in dockerfile
        assert "ARG IMAGE_VERSION=0.1.0" in dockerfile
        assert f'org.opencontainers.image.title="{image_name}"' in dockerfile
        assert 'org.opencontainers.image.version="$IMAGE_VERSION"' in dockerfile
        assert 'org.opencontainers.image.revision="$VCS_REF"' in dockerfile
