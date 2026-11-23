#!/usr/bin/env bash
# End-to-end smoke demo against a running service (default http://localhost:8000).
# Simulates two parallel executors bisecting 12 commits where c9 introduced
# the fault, including one build failure and out-of-order reports.
set -euo pipefail

BASE="${1:-http://localhost:8000}"
FAULT_INDEX=8   # c9 (0-based) is the first bad commit

echo "== create session =="
SID=$(curl -sS -X POST "$BASE/sessions" -H 'Content-Type: application/json' \
  -d '{"name":"nightly","commits":["c1","c2","c3","c4","c5","c6","c7","c8","c9","c10","c11","c12"]}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')
echo "session: $SID"

simulate() {  # echo the simulated result for a commit id
  local idx=${1#c}
  idx=$((idx - 1))
  if [ "$idx" -eq 5 ]; then echo build_failure;   # c6 does not build
  elif [ "$idx" -lt "$FAULT_INDEX" ]; then echo pass
  else echo fail; fi
}

echo "== two executors claim/report until done =="
while true; do
  progress=0
  for ex in executor-1 executor-2; do
    claim=$(curl -sS -X POST "$BASE/sessions/$SID/claims" \
      -H 'Content-Type: application/json' -d "{\"executor\":\"$ex\"}")
    commit=$(echo "$claim" | python3 -c 'import sys,json;print(json.load(sys.stdin)["commit"] or "")')
    [ -z "$commit" ] && continue
    progress=1
    result=$(simulate "$commit")
    echo "$ex: $commit -> $result"
    curl -sS -X POST "$BASE/sessions/$SID/reports" -H 'Content-Type: application/json' \
      -d "{\"commit\":\"$commit\",\"result\":\"$result\",\"executor\":\"$ex\"}" > /dev/null
  done
  [ "$progress" -eq 0 ] && break
done

echo "== final result =="
curl -sS "$BASE/sessions/$SID/result" | python3 -m json.tool
