#!/usr/bin/env bash
# Layering checks from docs/ARCHITECTURE.md and CLAUDE.md. Run via CI
# (.github/workflows/ci.yml) and locally as `./scripts/check_layering.sh`.
#
# WHY grep instead of an import-graph tool: all three rules are simple text
# patterns, and grep needs no extra dependency to enforce them.
set -euo pipefail
cd "$(dirname "$0")/.."

fail=0

echo "Checking: no fastapi import under backend/app/services/"
if grep -rn --include='*.py' -E '^\s*(import fastapi|from fastapi)' backend/app/services/ 2>/dev/null; then
  echo "FAIL: fastapi imported under backend/app/services/ — services must not know about HTTP."
  fail=1
fi

echo "Checking: no backend import under frontend/"
if grep -rn --include='*.py' -E '^\s*(import backend|from backend)' frontend/ 2>/dev/null; then
  echo "FAIL: backend imported under frontend/ — frontend must only talk to the API over HTTP."
  fail=1
fi

echo "Checking: no os.getenv/os.environ outside backend/app/config.py"
if grep -rn --include='*.py' --exclude='config.py' -E 'os\.getenv|os\.environ' backend/app/ 2>/dev/null; then
  echo "FAIL: os.getenv/os.environ used outside backend/app/config.py."
  fail=1
fi

if [ "$fail" -eq 1 ]; then
  exit 1
fi

echo "Layering checks passed."
