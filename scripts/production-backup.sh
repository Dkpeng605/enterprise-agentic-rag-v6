#!/usr/bin/env bash

# Create or restore a verified production backup. The backup contains no .env file.
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  production-backup.sh backup <output-directory>
  production-backup.sh restore <backup-directory> <restore-root>
  production-backup.sh restore-drill <backup-directory> <restore-root>

backup requires MILVUS_BACKUP_HOOK and BACKUP_AGE_RECIPIENT in production.
restore never accepts the live DEPLOY_PATH as its target.
restore-drill is an explicit alias for the isolated restore workflow; it uses
the same checksum, encryption, revision, and query-smoke checks as restore.
EOF
}

die() {
  printf 'production backup failed: %s\n' "$*" >&2
  exit 1
}

env_value() {
  local key="$1"
  local file="$2"
  awk -v key="$key" '
    index($0, key "=") == 1 {
      print substr($0, length(key) + 2)
      found = 1
      exit
    }
    END { if (!found) exit 1 }
  ' "$file"
}

require_env_value() {
  local key="$1"
  local file="$2"
  local value
  value="$(env_value "$key" "$file")" || die "missing ${key} in ${file}"
  [[ -n "$value" ]] || die "empty ${key} in ${file}"
  printf '%s' "$value"
}

metadata_value() {
  local key="$1"
  local file="$2"
  awk -v key="$key" '
    index($0, "  \"" key "\": \"") == 1 {
      value = substr($0, length(key) + 8)
      sub(/\",?$/, "", value)
      print value
      found = 1
      exit
    }
    END { if (!found) exit 1 }
  ' "$file"
}

set_env_value() {
  local key="$1"
  local value="$2"
  local file="$3"
  local temporary
  temporary="$(mktemp "${file}.tmp.XXXXXX")"
  if ! awk -v key="$key" -v value="$value" '
    index($0, key "=") == 1 {
      print key "=" value
      found = 1
      next
    }
    { print }
    END { if (!found) exit 1 }
  ' "$file" > "$temporary"; then
    rm -f -- "$temporary"
    die "missing ${key} in ${file}"
  fi
  chmod 600 "$temporary"
  mv -- "$temporary" "$file"
}

valid_path() {
  [[ "$1" == /* && "$1" != *[!a-zA-Z0-9_./-]* ]]
}

valid_sha() {
  [[ "$1" =~ ^[0-9a-f]{40}$ ]]
}

valid_image_for_sha() {
  local image="$1"
  local sha="$2"
  [[ "$image" =~ ^ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+:${sha}$ ]]
}

compose() {
  docker compose --env-file "$env_file" -f "$compose_file" "$@"
}

require_runtime() {
  local root="$1"
  valid_path "$root" || die "runtime root must be an absolute path without shell metacharacters"
  command -v docker >/dev/null 2>&1 || die "docker is not installed"
  command -v sha256sum >/dev/null 2>&1 || die "sha256sum is not installed"
  compose_file="${root}/infra/production/compose.yml"
  env_file="${root}/infra/production/.env.production"
  [[ -f "$compose_file" ]] || die "missing ${compose_file}"
  [[ -f "$env_file" ]] || die "missing ${env_file}"
  docker compose version >/dev/null 2>&1 || die "docker compose is not available"
  compose config --quiet || die "production compose configuration is invalid"
}

cleanup() {
  local status=$?
  if [[ "$status" != 0 && -n "${backup_dir:-}" && -d "$backup_dir" ]]; then
    rm -rf -- "$backup_dir"
  fi
  if [[ -n "${restore_tmp:-}" && -d "$restore_tmp" ]]; then
    rm -rf -- "$restore_tmp"
  fi
  exit "$status"
}

encrypt_file() {
  local source="$1"
  local recipient="${BACKUP_AGE_RECIPIENT:-}"
  if [[ -n "$recipient" ]]; then
    command -v age >/dev/null 2>&1 || die "age is required when BACKUP_AGE_RECIPIENT is set"
    age -r "$recipient" -o "${source}.age" "$source" >/dev/null 2>&1 \
      || die "backup encryption failed"
    rm -f -- "$source"
    printf '%s' "$(basename "${source}.age")"
    return
  fi
  [[ "${BACKUP_ALLOW_PLAINTEXT:-0}" == 1 ]] || die "production backups require BACKUP_AGE_RECIPIENT"
  printf '%s' "$(basename "$source")"
}

payload_path() {
  local directory="$1"
  local name="$2"
  if [[ -f "${directory}/${name}.age" ]]; then
    printf '%s' "${directory}/${name}.age"
  elif [[ -f "${directory}/${name}" ]]; then
    [[ "${BACKUP_ALLOW_PLAINTEXT:-0}" == 1 ]] \
      || die "plaintext backup payload requires explicit BACKUP_ALLOW_PLAINTEXT=1"
    printf '%s' "${directory}/${name}"
  else
    die "missing backup payload ${name}"
  fi
}

materialize_payload() {
  local source="$1"
  local destination="$2"
  if [[ "$source" == *.age ]]; then
    [[ -n "${BACKUP_AGE_IDENTITY:-}" && -f "$BACKUP_AGE_IDENTITY" ]] \
      || die "BACKUP_AGE_IDENTITY must point to the restore identity file"
    command -v age >/dev/null 2>&1 || die "age is required to decrypt this backup"
    age -d -i "$BACKUP_AGE_IDENTITY" -o "$destination" "$source" >/dev/null 2>&1 \
      || die "backup decryption failed"
  else
    cp -- "$source" "$destination"
  fi
}

smoke_query() {
  command -v curl >/dev/null 2>&1 || die "curl is not installed"
  local port="${SMOKE_HTTPS_PORT:-443}"
  [[ "$port" =~ ^[0-9]{1,5}$ ]] && (( port >= 1 && port <= 65535 )) \
    || die "SMOKE_HTTPS_PORT must be between 1 and 65535"
  local smoke_dir cookie_file auth_file
  smoke_dir="$(mktemp -d "${TMPDIR:-/tmp}/enterprise-rag-restore-smoke.XXXXXX")"
  cookie_file="${smoke_dir}/cookies.txt"
  auth_file="${smoke_dir}/auth.json"
  local base_url="https://${public_domain}:${port}"
  local -a curl_options=(
    --fail --silent --show-error --insecure --max-time 15
    --retry 12 --retry-delay 2 --retry-max-time 60 --retry-connrefused
    --resolve "${public_domain}:${port}:127.0.0.1"
  )

  curl "${curl_options[@]}" "${base_url}/health/live" -o /dev/null
  curl "${curl_options[@]}" --cookie-jar "$cookie_file" \
    "${base_url}/api/v1/auth/me" -o "$auth_file"
  curl "${curl_options[@]}" --cookie "$cookie_file" \
    "${base_url}/api/v1/workspace/overview" -o /dev/null
  curl "${curl_options[@]}" --cookie "$cookie_file" \
    -H 'Content-Type: application/json' \
    -d '{"query":"backup restore smoke","mode":"standard"}' \
    "${base_url}/api/v1/queries" -o /dev/null
  rm -rf -- "$smoke_dir"
}

backup() {
  local root="$1"
  local output_dir="$2"
  valid_path "$output_dir" || die "backup output must be an absolute path without shell metacharacters"
  require_runtime "$root"

  local app_sha backend_image frontend_image
  app_sha="$(require_env_value APP_COMMIT_SHA "$env_file")"
  backend_image="$(require_env_value BACKEND_IMAGE "$env_file")"
  frontend_image="$(require_env_value FRONTEND_IMAGE "$env_file")"
  valid_sha "$app_sha" || die "APP_COMMIT_SHA is not an immutable commit SHA"
  valid_image_for_sha "$backend_image" "$app_sha" || die "BACKEND_IMAGE does not match APP_COMMIT_SHA"
  valid_image_for_sha "$frontend_image" "$app_sha" || die "FRONTEND_IMAGE does not match APP_COMMIT_SHA"
  [[ -x "${MILVUS_BACKUP_HOOK:-}" ]] || die "MILVUS_BACKUP_HOOK must point to an executable hook"

  mkdir -p -- "$output_dir"
  local created_at backup_id
  created_at="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_id="release-${app_sha:0:12}-${created_at}"
  backup_dir="${output_dir}/${backup_id}"
  mkdir -- "$backup_dir"
  mkdir -- "${backup_dir}/milvus"
  cp -- "$compose_file" "${backup_dir}/compose.yml"
  cp -- "${compose_file%/*}/gateway.Caddyfile" "${backup_dir}/gateway.Caddyfile"

  compose up -d postgres
  compose exec -T postgres sh -c \
    'pg_dump --format=custom --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    > "${backup_dir}/postgres.dump"
  [[ -s "${backup_dir}/postgres.dump" ]] || die "PostgreSQL backup is empty"

  compose run --rm --no-deps -T api enterprise-rag-reconcile --fingerprint --json \
    > "${backup_dir}/runtime-fingerprint.json"
  [[ -s "${backup_dir}/runtime-fingerprint.json" ]] || die "runtime fingerprint is empty"
  compose run --rm --no-deps -T api enterprise-rag-object-archive create \
    > "${backup_dir}/objects.tar.gz"
  [[ -s "${backup_dir}/objects.tar.gz" ]] || die "ObjectStore archive is empty"

  "$MILVUS_BACKUP_HOOK" create "$backup_id" "${backup_dir}/milvus" >/dev/null 2>&1 \
    || die "Milvus backup hook failed"
  [[ -s "${backup_dir}/milvus/reference.json" ]] || die "Milvus hook did not write reference.json"

  local postgres_name objects_name encrypted
  postgres_name="$(encrypt_file "${backup_dir}/postgres.dump")"
  objects_name="$(encrypt_file "${backup_dir}/objects.tar.gz")"
  encrypted=false
  [[ -n "${BACKUP_AGE_RECIPIENT:-}" ]] && encrypted=true

  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "backup_id": "%s",\n' "$backup_id"
    printf '  "created_at": "%s",\n' "$created_at"
    printf '  "app_commit_sha": "%s",\n' "$app_sha"
    printf '  "backend_image": "%s",\n' "$backend_image"
    printf '  "frontend_image": "%s",\n' "$frontend_image"
    printf '  "postgres_dump": "%s",\n' "$postgres_name"
    printf '  "objects_archive": "%s",\n' "$objects_name"
    printf '  "runtime_fingerprint": "runtime-fingerprint.json",\n'
    printf '  "milvus_reference": "milvus/reference.json",\n'
    printf '  "encrypted_payloads": %s\n' "$encrypted"
    printf '}\n'
  } > "${backup_dir}/metadata.json"
  (
    cd "$backup_dir"
    sha256sum "$postgres_name" "$objects_name" runtime-fingerprint.json \
      milvus/reference.json compose.yml gateway.Caddyfile metadata.json > checksums.sha256
    sha256sum --check --strict checksums.sha256 >/dev/null
  ) || die "backup checksum verification failed"
  printf '{"backup_id":"%s","directory":"%s","encrypted_payloads":%s}\n' \
    "$backup_id" "$backup_dir" "$encrypted"
}

restore() {
  local source_dir="$1"
  local target_root="$2"
  valid_path "$source_dir" || die "backup directory must be an absolute path without shell metacharacters"
  valid_path "$target_root" || die "restore root must be an absolute path without shell metacharacters"
  [[ -d "$source_dir" ]] || die "backup directory does not exist"
  local live_root="${LIVE_DEPLOY_PATH:-/opt/enterprise-agentic-rag-v6}"
  valid_path "$live_root" || die "LIVE_DEPLOY_PATH is invalid"
  case "$target_root" in
    "$live_root"|"$live_root"/*) die "RESTORE_PATH must differ from DEPLOY_PATH" ;;
  esac
  local backup_id="$(basename "$source_dir")"
  [[ "$backup_id" =~ ^release-[0-9a-f]{12}-[0-9]{8}T[0-9]{6}Z$ ]] \
    || die "backup directory name is invalid"
  [[ -f "${source_dir}/metadata.json" && -f "${source_dir}/checksums.sha256" ]] \
    || die "backup metadata is incomplete"
  (
    cd "$source_dir"
    sha256sum --check --strict checksums.sha256 >/dev/null
  ) || die "backup checksum verification failed"

  mkdir -p -- "${target_root}/infra/production"
  cp -- "${source_dir}/compose.yml" "${target_root}/infra/production/compose.yml"
  cp -- "${source_dir}/gateway.Caddyfile" "${target_root}/infra/production/gateway.Caddyfile"
  local restore_sha restore_backend restore_frontend
  restore_sha="$(metadata_value app_commit_sha "${source_dir}/metadata.json")" \
    || die "backup metadata has no app commit SHA"
  restore_backend="$(metadata_value backend_image "${source_dir}/metadata.json")" \
    || die "backup metadata has no backend image"
  restore_frontend="$(metadata_value frontend_image "${source_dir}/metadata.json")" \
    || die "backup metadata has no frontend image"
  valid_sha "$restore_sha" || die "backup metadata commit SHA is invalid"
  valid_image_for_sha "$restore_backend" "$restore_sha" \
    || die "backup metadata backend image is invalid"
  valid_image_for_sha "$restore_frontend" "$restore_sha" \
    || die "backup metadata frontend image is invalid"
  [[ "$backup_id" == "release-${restore_sha:0:12}-"* ]] \
    || die "backup directory and metadata commit SHA do not match"
  require_runtime "$target_root"
  [[ -x "${MILVUS_BACKUP_HOOK:-}" ]] || die "MILVUS_BACKUP_HOOK must point to an executable hook"

  set_env_value APP_COMMIT_SHA "$restore_sha" "$env_file"
  set_env_value BACKEND_IMAGE "$restore_backend" "$env_file"
  set_env_value FRONTEND_IMAGE "$restore_frontend" "$env_file"
  compose config --quiet || die "restored image configuration is invalid"

  restore_tmp="$(mktemp -d "${TMPDIR:-/tmp}/enterprise-rag-restore.XXXXXX")"
  local postgres_source objects_source postgres_payload objects_payload
  postgres_source="$(payload_path "$source_dir" postgres.dump)"
  objects_source="$(payload_path "$source_dir" objects.tar.gz)"
  postgres_payload="${restore_tmp}/postgres.dump"
  objects_payload="${restore_tmp}/objects.tar.gz"
  materialize_payload "$postgres_source" "$postgres_payload"
  materialize_payload "$objects_source" "$objects_payload"
  public_domain="$(require_env_value PUBLIC_DOMAIN "$env_file")"
  [[ "$public_domain" =~ ^[A-Za-z0-9.-]+$ ]] || die "PUBLIC_DOMAIN is not a hostname"

  compose up -d postgres
  compose exec -T postgres sh -c \
    'pg_restore --exit-on-error --clean --if-exists --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB" -' \
    < "$postgres_payload"
  compose run --rm migrate
  compose run --rm --no-deps -T api enterprise-rag-object-archive restore < "$objects_payload"
  "$MILVUS_BACKUP_HOOK" restore "$backup_id" "${source_dir}/milvus" >/dev/null 2>&1 \
    || die "Milvus restore hook failed"
  compose run --rm --no-deps -T api enterprise-rag-reconcile --json > /dev/null \
    || die "restored Milvus/ObjectStore state did not reconcile cleanly"
  compose up -d api worker frontend gateway
  smoke_query
  printf '{"restored_backup_id":"%s","restore_root":"%s","query_smoke":true}\n' \
    "$backup_id" "$target_root"
}

main() {
  local action="${1:-}"
  local root="${DEPLOY_PATH:-/opt/enterprise-agentic-rag-v6}"
  case "$action" in
    backup)
      [[ $# -eq 2 ]] || { usage >&2; exit 2; }
      backup "$root" "$2"
      ;;
    restore|restore-drill)
      [[ $# -eq 3 ]] || { usage >&2; exit 2; }
      restore "$2" "$3"
      ;;
    -h|--help)
      usage
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
}

trap cleanup EXIT
main "$@"
