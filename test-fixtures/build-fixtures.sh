#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
output_dir="${1:-${script_dir}/index}"

exec "${PYTHON:-python3}" "${script_dir}/build_fixtures.py" "${output_dir}"
