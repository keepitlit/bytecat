#!/usr/bin/env bash
# Run every test script. Each one seeds a temporary catalog and cleans up after
# itself; tier-3 checks skip when no model server / Chroma is running.
set -u
cd "$(dirname "$0")/.."
fail=0
for t in tests/test_*.py; do
  echo "== $t"
  python3 "$t" || fail=1
done
exit $fail
