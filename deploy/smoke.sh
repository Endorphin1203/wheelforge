#!/usr/bin/env bash
set -euo pipefail

: "${WF_DATABASE_USER:?WF_DATABASE_USER is required}"
: "${WF_DATABASE_PASSWORD:?WF_DATABASE_PASSWORD is required}"
: "${WF_DATA_ROOT:?WF_DATA_ROOT is required}"
: "${WF_WORKSPACE_ROOT:?WF_WORKSPACE_ROOT is required}"

api_url="${WF_API_READINESS_URL:-http://127.0.0.1:8080/actuator/health/readiness}"
frontend_url="${WF_FRONTEND_URL:-http://127.0.0.1:8080/}"
database_host="${WF_DATABASE_HOST:-127.0.0.1}"
database_port="${WF_DATABASE_PORT:-3306}"

command -v curl >/dev/null
command -v mysqladmin >/dev/null
command -v python3 >/dev/null

health="$(curl --fail --silent --show-error --max-time 10 "${api_url}")"
python3 -c 'import json,sys; assert json.load(sys.stdin).get("status") == "UP"' <<<"${health}"
frontend="$(curl --fail --silent --show-error --max-time 10 "${frontend_url}")"
grep --fixed-strings --quiet '<title>WheelForge</title>' <<<"${frontend}"

MYSQL_PWD="${WF_DATABASE_PASSWORD}" mysqladmin \
  --protocol=TCP \
  --host="${database_host}" \
  --port="${database_port}" \
  --user="${WF_DATABASE_USER}" \
  --connect-timeout=5 \
  --silent ping

for root in "${WF_DATA_ROOT}" "${WF_WORKSPACE_ROOT}"; do
  test -d "${root}"
  test ! -L "${root}"
  test -r "${root}" && test -w "${root}" && test -x "${root}"
  probe="$(mktemp "${root}/.wheelforge-smoke.XXXXXX")"
  rm -f -- "${probe}"
done

test "$(cd "${WF_DATA_ROOT}" && pwd -P)" != "$(cd "${WF_WORKSPACE_ROOT}" && pwd -P)"
printf '%s\n' "WheelForge native smoke checks passed"
