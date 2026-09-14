#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="${repository_root}/infra/compose/compose.dev.yml"
database_names=("enterprise_rag_dev" "enterprise_rag_test")

for attempt in {1..30}; do
  if docker compose -f "${compose_file}" exec -T postgres \
    psql -U enterprise_rag -d postgres -Atc "SELECT 1" >/dev/null 2>&1; then
    break
  fi
  if [[ "${attempt}" == "30" ]]; then
    echo "PostgreSQL did not become available." >&2
    exit 1
  fi
  sleep 1
done

for database_name in "${database_names[@]}"; do
  exists="$(docker compose -f "${compose_file}" exec -T postgres \
    psql -U enterprise_rag -d postgres -Atc \
    "SELECT 1 FROM pg_database WHERE datname = '${database_name}'")"
  if [[ "${exists}" != "1" ]]; then
    docker compose -f "${compose_file}" exec -T postgres \
      createdb -U enterprise_rag -O enterprise_rag "${database_name}"
    echo "Created local PostgreSQL database: ${database_name}"
  fi
done
