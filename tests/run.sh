#!/usr/bin/env bash
# Run every hook test suite. Each suite skips itself if its tool is missing.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
rc=0
for t in test-*.sh; do
  bash "$t" | tail -1 || rc=1
  [ "${PIPESTATUS[0]}" = 0 ] || rc=1
done
exit $rc
