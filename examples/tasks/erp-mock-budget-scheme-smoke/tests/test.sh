#!/bin/bash
set -euo pipefail
python3 "$(dirname "$0")/verify.py" \
  "${BENCH_WORKSPACE:-/workspace}" "${BENCH_LOG_DIR:-/logs/verifier}"
