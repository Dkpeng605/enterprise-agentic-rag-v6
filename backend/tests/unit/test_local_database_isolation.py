from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[3]


def test_mac_runtime_and_integration_tests_use_different_databases() -> None:
    compose = (REPOSITORY_ROOT / "infra/compose/compose.dev.yml").read_text(encoding="utf-8")
    mac_environment = (REPOSITORY_ROOT / ".env.mac.example").read_text(encoding="utf-8")
    generic_environment = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")
    backend_script = (REPOSITORY_ROOT / "scripts/mac-backend.sh").read_text(encoding="utf-8")

    assert "POSTGRES_DB: enterprise_rag_dev" in compose
    assert "enterprise_rag_dev" in mac_environment
    assert "enterprise_rag_dev" in generic_environment
    assert "scripts/ensure-local-databases.sh" in backend_script
    assert "enterprise_rag_test" not in mac_environment


def test_local_database_bootstrap_creates_dev_and_test_databases() -> None:
    bootstrap = (REPOSITORY_ROOT / "scripts/ensure-local-databases.sh").read_text(
        encoding="utf-8"
    )

    assert 'database_names=("enterprise_rag_dev" "enterprise_rag_test")' in bootstrap
    assert "DROP DATABASE" not in bootstrap
    assert "createdb" in bootstrap
