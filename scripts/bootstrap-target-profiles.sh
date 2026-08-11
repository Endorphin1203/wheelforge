#!/usr/bin/env bash
set -euo pipefail

: "${WF_DATABASE_USER:?WF_DATABASE_USER is required}"
: "${WF_DATABASE_PASSWORD:?WF_DATABASE_PASSWORD is required}"

database_host="${WF_DATABASE_HOST:-127.0.0.1}"
database_port="${WF_DATABASE_PORT:-3306}"
database_name="${WF_DATABASE_NAME:-wheelforge}"

for value in "${database_host}" "${database_port}" "${database_name}" "${WF_DATABASE_USER}"; do
  [[ "${value}" =~ ^[A-Za-z0-9_.:-]+$ ]] || {
    printf '%s\n' "Database connection value contains unsupported characters" >&2
    exit 2
  }
done

command -v mysql >/dev/null

MYSQL_PWD="${WF_DATABASE_PASSWORD}" mysql \
  --protocol=TCP \
  --host="${database_host}" \
  --port="${database_port}" \
  --user="${WF_DATABASE_USER}" \
  --database="${database_name}" \
  --batch --skip-column-names <<'SQL'
INSERT INTO target_profiles (
  id, code, os, architecture, python_implementation,
  python_version, python_full_version, platform_tag, abi_tags,
  validation_type, validation_policy_version, enabled, version_no
) VALUES
  (UUID(), 'linux-x86_64-cp39-manylinux2014', 'LINUX', 'X86_64', 'CPYTHON', '3.9', '3.9.25', 'manylinux2014_x86_64', JSON_ARRAY('cp39', 'abi3', 'none'), 'STATIC', 'wheel-tags-v1', TRUE, 0),
  (UUID(), 'linux-x86_64-cp310-manylinux2014', 'LINUX', 'X86_64', 'CPYTHON', '3.10', '3.10.20', 'manylinux2014_x86_64', JSON_ARRAY('cp310', 'abi3', 'none'), 'STATIC', 'wheel-tags-v1', TRUE, 0),
  (UUID(), 'linux-x86_64-cp311-manylinux2014', 'LINUX', 'X86_64', 'CPYTHON', '3.11', '3.11.15', 'manylinux2014_x86_64', JSON_ARRAY('cp311', 'abi3', 'none'), 'STATIC', 'wheel-tags-v1', TRUE, 0),
  (UUID(), 'linux-x86_64-cp312-manylinux2014', 'LINUX', 'X86_64', 'CPYTHON', '3.12', '3.12.13', 'manylinux2014_x86_64', JSON_ARRAY('cp312', 'abi3', 'none'), 'STATIC', 'wheel-tags-v1', TRUE, 0),
  (UUID(), 'linux-x86_64-cp313-manylinux2014', 'LINUX', 'X86_64', 'CPYTHON', '3.13', '3.13.13', 'manylinux2014_x86_64', JSON_ARRAY('cp313', 'abi3', 'none'), 'STATIC', 'wheel-tags-v1', TRUE, 0),
  (UUID(), 'linux-arm64-cp39-manylinux2014', 'LINUX', 'AARCH64', 'CPYTHON', '3.9', '3.9.25', 'manylinux2014_aarch64', JSON_ARRAY('cp39', 'abi3', 'none'), 'STATIC', 'wheel-tags-v1', TRUE, 0),
  (UUID(), 'linux-arm64-cp310-manylinux2014', 'LINUX', 'AARCH64', 'CPYTHON', '3.10', '3.10.20', 'manylinux2014_aarch64', JSON_ARRAY('cp310', 'abi3', 'none'), 'STATIC', 'wheel-tags-v1', TRUE, 0),
  (UUID(), 'linux-arm64-cp311-manylinux2014', 'LINUX', 'AARCH64', 'CPYTHON', '3.11', '3.11.15', 'manylinux2014_aarch64', JSON_ARRAY('cp311', 'abi3', 'none'), 'STATIC', 'wheel-tags-v1', TRUE, 0),
  (UUID(), 'linux-arm64-cp312-manylinux2014', 'LINUX', 'AARCH64', 'CPYTHON', '3.12', '3.12.13', 'manylinux2014_aarch64', JSON_ARRAY('cp312', 'abi3', 'none'), 'STATIC', 'wheel-tags-v1', TRUE, 0),
  (UUID(), 'linux-arm64-cp313-manylinux2014', 'LINUX', 'AARCH64', 'CPYTHON', '3.13', '3.13.13', 'manylinux2014_aarch64', JSON_ARRAY('cp313', 'abi3', 'none'), 'STATIC', 'wheel-tags-v1', TRUE, 0),
  (UUID(), 'windows-x64-cp39', 'WINDOWS', 'AMD64', 'CPYTHON', '3.9', '3.9.25', 'win_amd64', JSON_ARRAY('cp39', 'abi3', 'none'), 'STATIC', 'wheel-tags-v1', TRUE, 0),
  (UUID(), 'windows-x64-cp310', 'WINDOWS', 'AMD64', 'CPYTHON', '3.10', '3.10.20', 'win_amd64', JSON_ARRAY('cp310', 'abi3', 'none'), 'STATIC', 'wheel-tags-v1', TRUE, 0),
  (UUID(), 'windows-x64-cp311', 'WINDOWS', 'AMD64', 'CPYTHON', '3.11', '3.11.15', 'win_amd64', JSON_ARRAY('cp311', 'abi3', 'none'), 'STATIC', 'wheel-tags-v1', TRUE, 0),
  (UUID(), 'windows-x64-cp312', 'WINDOWS', 'AMD64', 'CPYTHON', '3.12', '3.12.13', 'win_amd64', JSON_ARRAY('cp312', 'abi3', 'none'), 'STATIC', 'wheel-tags-v1', TRUE, 0),
  (UUID(), 'windows-x64-cp313', 'WINDOWS', 'AMD64', 'CPYTHON', '3.13', '3.13.13', 'win_amd64', JSON_ARRAY('cp313', 'abi3', 'none'), 'STATIC', 'wheel-tags-v1', TRUE, 0),
  (UUID(), 'windows-arm64-cp39', 'WINDOWS', 'ARM64', 'CPYTHON', '3.9', '3.9.25', 'win_arm64', JSON_ARRAY('cp39', 'abi3', 'none'), 'STATIC', 'wheel-tags-v1', TRUE, 0),
  (UUID(), 'windows-arm64-cp310', 'WINDOWS', 'ARM64', 'CPYTHON', '3.10', '3.10.20', 'win_arm64', JSON_ARRAY('cp310', 'abi3', 'none'), 'STATIC', 'wheel-tags-v1', TRUE, 0),
  (UUID(), 'windows-arm64-cp311', 'WINDOWS', 'ARM64', 'CPYTHON', '3.11', '3.11.15', 'win_arm64', JSON_ARRAY('cp311', 'abi3', 'none'), 'STATIC', 'wheel-tags-v1', TRUE, 0),
  (UUID(), 'windows-arm64-cp312', 'WINDOWS', 'ARM64', 'CPYTHON', '3.12', '3.12.13', 'win_arm64', JSON_ARRAY('cp312', 'abi3', 'none'), 'STATIC', 'wheel-tags-v1', TRUE, 0),
  (UUID(), 'windows-arm64-cp313', 'WINDOWS', 'ARM64', 'CPYTHON', '3.13', '3.13.13', 'win_arm64', JSON_ARRAY('cp313', 'abi3', 'none'), 'STATIC', 'wheel-tags-v1', TRUE, 0)
ON DUPLICATE KEY UPDATE
  os = VALUES(os),
  architecture = VALUES(architecture),
  python_implementation = VALUES(python_implementation),
  python_version = VALUES(python_version),
  python_full_version = VALUES(python_full_version),
  platform_tag = VALUES(platform_tag),
  abi_tags = VALUES(abi_tags),
  validation_type = VALUES(validation_type),
  validation_policy_version = VALUES(validation_policy_version),
  enabled = TRUE,
  version_no = version_no + 1;
SQL

count="$(MYSQL_PWD="${WF_DATABASE_PASSWORD}" mysql \
  --protocol=TCP \
  --host="${database_host}" \
  --port="${database_port}" \
  --user="${WF_DATABASE_USER}" \
  --database="${database_name}" \
  --batch --skip-column-names \
  --execute="SELECT COUNT(*) FROM target_profiles WHERE enabled = TRUE AND validation_type = 'STATIC' AND validation_policy_version = 'wheel-tags-v1'")"

if [[ "${count}" != "20" ]]; then
  printf 'Expected 20 enabled V1 target profiles, found %s\n' "${count}" >&2
  exit 1
fi
printf '%s\n' "Bootstrapped 20 WheelForge V1 target profiles"
