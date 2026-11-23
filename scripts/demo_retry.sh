#!/usr/bin/env bash
# Idempotent-claim smoke demo against a running service
# (default http://localhost:8000).  Walks through the lost-response scenario:
#
#   1. claim with an explicit request_id
#   2. pretend the response was lost: retry the SAME number -> same commit,
#      replayed=true, no second dispatch (also done concurrently)
#   3. report the recovered commit, then retry again -> already_reported,
#      in-flight list unchanged
#   4. a number that first got a no-claim answer is retried later and sees the
#      current dispatch state (nothing is cached)
#   5. GET /sessions/{id}/result for the verdict
#
# Every step is a plain curl call you can also replay in the Swagger UI at
# /docs (POST /sessions/{session_id}/claims, set the "request_id" field).
set -euo pipefail

BASE="${1:-http://localhost:8000}"
export BASE
json() { python3 -c 'import sys,json;print(json.load(sys.stdin)'"$1"')'; }

echo "== create session (12 commits) =="
SID=$(curl -sS -X POST "$BASE/sessions" -H 'Content-Type: application/json' \
  -d '{"name":"retry-demo","commits":["c1","c2","c3","c4","c5","c6","c7","c8","c9","c10","c11","c12"]}' \
  | json '["session_id"]')
echo "session: $SID"

echo
echo "== 1. first claim, request_id req-007 =="
FIRST=$(curl -sS -X POST "$BASE/sessions/$SID/claims" -H 'Content-Type: application/json' \
  -d '{"executor":"executor-1","request_id":"req-007"}')
echo "$FIRST" | python3 -m json.tool
COMMIT=$(echo "$FIRST" | json '["commit"]')

echo
echo "== 2. response lost: retry the SAME request_id =="
SECOND=$(curl -sS -X POST "$BASE/sessions/$SID/claims" -H 'Content-Type: application/json' \
  -d '{"executor":"executor-1","request_id":"req-007"}')
echo "$SECOND" | python3 -m json.tool
[ "$(echo "$SECOND" | json '["commit"]')" = "$COMMIT" ] \
  && echo "OK: same commit $COMMIT recovered, replayed=true"
[ "$(echo "$SECOND" | json '["replayed"]')" = "True" ] || { echo "FAIL: expected replayed"; exit 1; }

echo
echo "== 3. four concurrent retries with the same key: exactly one dispatch =="
for _ in 1 2 3 4; do
  curl -sS -X POST "$BASE/sessions/$SID/claims" -H 'Content-Type: application/json' \
    -d '{"executor":"executor-1","request_id":"req-007"}' &
done
wait
echo "in-flight after concurrent retries:"
curl -sS "$BASE/sessions/$SID" | python3 -c '
import sys, json
view = json.load(sys.stdin)
for c in view["in_flight"]:
    print(" ", c["commit"], c["executor"], c["request_id"])
assert len(view["in_flight"]) == 1, "commit dispatched more than once"
print("OK: still exactly one in-flight entry")'

echo
echo "== 4. report the recovered commit, then retry req-007 again =="
curl -sS -X POST "$BASE/sessions/$SID/reports" -H 'Content-Type: application/json' \
  -d "{\"commit\":\"$COMMIT\",\"result\":\"fail\",\"executor\":\"executor-1\"}" \
  | python3 -c 'import sys,json; v=json.load(sys.stdin); print("reported; status =", v["status"])'
AGAIN=$(curl -sS -X POST "$BASE/sessions/$SID/claims" -H 'Content-Type: application/json' \
  -d '{"executor":"executor-1","request_id":"req-007"}')
echo "$AGAIN" | python3 -m json.tool
[ "$(echo "$AGAIN" | json '["reason"]')" = "already_reported" ] \
  && echo "OK: retry names the already-reported commit"

echo
echo "== 5. fresh request numbers still dispatch new commits =="
for n in 100 101; do
  curl -sS -X POST "$BASE/sessions/$SID/claims" -H 'Content-Type: application/json' \
    -d "{\"executor\":\"executor-2\",\"request_id\":\"req-$n\"}" \
    | python3 -c 'import sys,json; v=json.load(sys.stdin); print("req-'"$n"' ->", v["commit"], "replayed =", v["replayed"])'
done

echo
echo "== 6. no-claim answers are not cached =="
echo "-- claim everything still available, each with its own request number --"
n=200
while true; do
  GOT=$(curl -sS -X POST "$BASE/sessions/$SID/claims" -H 'Content-Type: application/json' \
        -d "{\"executor\":\"executor-2\",\"request_id\":\"req-$n\"}")
  C=$(echo "$GOT" | json '["commit"]' || true)
  [ "$C" = "None" ] && break
  echo "req-$n claimed $C (holding it, no report yet)"
  n=$((n + 1))
done

echo "-- probe-1 now finds only in-flight work: waiting_for_reports --"
PROBE=$(curl -sS -X POST "$BASE/sessions/$SID/claims" -H 'Content-Type: application/json' \
        -d '{"executor":"executor-9","request_id":"probe-1"}')
echo "$PROBE" | python3 -m json.tool
[ "$(echo "$PROBE" | json '["reason"]')" = "waiting_for_reports" ] \
  && echo "OK: first probe-1 got waiting_for_reports"

echo "-- report every held commit (all pass) to finish the bisection --"
export SID2="$SID"
curl -sS "$BASE/sessions/$SID" | python3 -c '
import json, os, subprocess, sys
sid = os.environ["SID2"]
base = os.environ.get("BASE", "http://localhost:8000")
view = json.load(sys.stdin)
for entry in view["in_flight"]:
    body = json.dumps({"commit": entry["commit"], "result": "pass",
                       "executor": entry["executor"]})
    r = subprocess.run(["curl", "-sS", "-X", "POST",
                        f"{base}/sessions/{sid}/reports",
                        "-H", "Content-Type: application/json", "-d", body],
                       capture_output=True, text=True)
    print("reported", entry["commit"])
'

echo "-- same probe-1 again: re-evaluated live, now completed (not cached) --"
PROBE2=$(curl -sS -X POST "$BASE/sessions/$SID/claims" -H 'Content-Type: application/json' \
         -d '{"executor":"executor-9","request_id":"probe-1"}')
echo "$PROBE2" | python3 -m json.tool
[ "$(echo "$PROBE2" | json '["reason"]')" = "completed" ] \
  && [ "$(echo "$PROBE2" | json '["replayed"]')" = "False" ] \
  && echo "OK: retry reflects current state, nothing was cached"

echo
echo "== 7. final verdict (also available at GET /sessions/{id}/result) =="
curl -sS "$BASE/sessions/$SID/result" | python3 -m json.tool
