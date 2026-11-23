"""End-to-end tests for the bisection service API.

Covered scenarios: parallel claiming, out-of-order reports, build failures,
contradiction handling, completion gating and input validation.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.bisect import BisectSession, Result
from app.main import app

client = TestClient(app)


def make_session(commits: list[str], name: str | None = None) -> str:
    resp = client.post("/sessions", json={"commits": commits, "name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


def claim(sid: str, executor: str) -> dict:
    resp = client.post(f"/sessions/{sid}/claims", json={"executor": executor})
    assert resp.status_code == 200, resp.text
    return resp.json()


def report(sid: str, commit: str, result: str, executor: str | None = None) -> dict:
    payload: dict = {"commit": commit, "result": result}
    if executor:
        payload["executor"] = executor
    resp = client.post(f"/sessions/{sid}/reports", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def get(sid: str) -> dict:
    resp = client.get(f"/sessions/{sid}")
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------- bisect

def test_full_bisect_finds_faulty_commit():
    # fault introduced by c7 (index 6); c1 passes, c10 reproduces
    commits = [f"c{i}" for i in range(1, 11)]
    sid = make_session(commits, name="nightly-test")

    claimed: list[str] = []
    while True:
        got = claim(sid, "executor-1")
        if got["commit"] is None:
            assert got["reason"] == "completed"
            break
        claimed.append(got["commit"])
        result = "pass" if got["index"] < 6 else "fail"
        view = report(sid, got["commit"], result, "executor-1")
        if view["status"] == "completed":
            break

    view = get(sid)
    assert view["status"] == "completed"
    assert view["conclusion"] == {"faulty_commit": "c7", "suspects": ["c7"]}
    # midpoint strategy needs only log2(10) ~= 3-4 dispatches
    assert len(claimed) <= 4
    assert len(set(claimed)) == len(claimed)  # never dispatched twice


def test_two_commit_session_completes_immediately():
    sid = make_session(["good", "bad"])
    view = get(sid)
    assert view["status"] == "completed"
    assert view["conclusion"] == {"faulty_commit": "bad", "suspects": ["bad"]}
    got = claim(sid, "executor-1")
    assert got["commit"] is None and got["reason"] == "completed"


# --------------------------------------------------------------------- claims

def test_parallel_claims_get_distinct_commits_each_once():
    commits = [f"c{i}" for i in range(1, 21)]
    sid = make_session(commits)

    first = claim(sid, "executor-1")
    second = claim(sid, "executor-2")
    assert first["commit"] is not None and second["commit"] is not None
    assert first["commit"] != second["commit"]

    view = get(sid)
    in_flight = {c["commit"] for c in view["in_flight"]}
    assert in_flight == {first["commit"], second["commit"]}

    # drain every remaining claimable commit; none may repeat
    seen = {first["commit"], second["commit"]}
    while True:
        got = claim(sid, "executor-1")
        if got["commit"] is None:
            break
        assert got["commit"] not in seen
        seen.add(got["commit"])
    # every interior commit dispatched exactly once
    assert seen == set(commits[1:-1])


def test_concurrent_claims_are_unique_at_core_level():
    session = BisectSession([f"c{i}" for i in range(60)])
    claimed: list[int] = []
    guard = threading.Lock()

    def worker(name: str):
        while True:
            got = session.claim(name)
            if got["commit"] is None:
                return
            with guard:
                claimed.append(got["index"])

    threads = [threading.Thread(target=worker, args=(f"ex-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == len(set(claimed))  # no commit dispatched twice
    assert sorted(claimed) == list(range(1, 59))


# -------------------------------------------------------------------- reports

def test_out_of_order_reports_still_checked():
    commits = [f"c{i}" for i in range(1, 11)]
    sid = make_session(commits)

    low = claim(sid, "executor-1")    # index 4
    high = claim(sid, "executor-2")   # index 5
    assert low["index"] == 4 and high["index"] == 5

    # the later commit reports first and passes -> suspect range moves past
    # the still-outstanding earlier claim
    view = report(sid, high["commit"], "pass", "executor-2")
    assert view["known_good"]["commit"] == commits[5]

    # the earlier claim is now outside the range, but its report must still
    # be accepted and checked
    view = report(sid, low["commit"], "pass", "executor-1")
    state = view["commit_states"][4]
    assert state["state"] == "passed"
    assert view["known_good"]["commit"] == commits[5]  # unchanged: older pass

    # finish the bisection: fault is at c8 (index 7)
    got = claim(sid, "executor-1")
    assert got["index"] == 7
    report(sid, got["commit"], "fail")
    got = claim(sid, "executor-1")
    assert got["index"] == 6
    view = report(sid, got["commit"], "pass")
    assert view["status"] == "completed"
    assert view["conclusion"]["faulty_commit"] == "c8"


def test_build_failure_keeps_range_and_suspect():
    commits = [f"c{i}" for i in range(1, 6)]  # c1 pass, c5 fail
    sid = make_session(commits)

    got = claim(sid, "executor-1")
    assert got["commit"] == "c3"      # midpoint
    view = report(sid, got["commit"], "build_failure")
    # build failure must not move the bounds
    assert view["known_good"]["commit"] == "c1"
    assert view["known_bad"]["commit"] == "c5"
    assert view["commit_states"][2]["state"] == "build_failed"
    assert view["status"] == "running"

    got = claim(sid, "executor-1")
    assert got["commit"] == "c2"
    report(sid, got["commit"], "pass")
    got = claim(sid, "executor-1")
    assert got["commit"] == "c4"
    view = report(sid, got["commit"], "fail")

    # c3 cannot be built, so the origin is either c3 or c4
    assert view["status"] == "completed"
    assert view["conclusion"] == {"faulty_commit": None, "suspects": ["c3", "c4"]}
    assert view["suspect_commits"] == ["c3", "c4"]


def test_contradiction_stops_dispatch_and_lists_pair():
    commits = [f"c{i}" for i in range(1, 7)]
    sid = make_session(commits)

    a = claim(sid, "executor-1")   # index 2 (c3)
    b = claim(sid, "executor-2")   # index 3 (c4)
    extra = claim(sid, "executor-1")  # still in flight when contradiction hits
    assert (a["index"], b["index"]) == (2, 3)

    report(sid, a["commit"], "fail", "executor-1")   # earlier commit fails
    view = report(sid, b["commit"], "pass", "executor-2")  # later commit passes

    assert view["status"] == "contradiction"
    contra = view["contradiction"]
    assert contra["earlier_failed"] == {"index": 2, "commit": "c3"}
    assert contra["later_passed"] == {"index": 3, "commit": "c4"}

    # dispatch halted
    got = claim(sid, "executor-1")
    assert got["commit"] is None and got["reason"] == "contradiction"

    # ... but a report from a test claimed before the halt is still checked
    view = report(sid, extra["commit"], "pass", "executor-1")
    assert view["commit_states"][extra["index"]]["state"] == "passed"
    assert view["status"] == "contradiction"  # first contradiction stands

    result = client.get(f"/sessions/{sid}/result").json()
    assert result["conclusion"] is None
    assert result["contradiction"]["earlier_failed"]["commit"] == "c3"


def test_completion_waits_for_in_flight_reports():
    commits = [f"c{i}" for i in range(1, 9)]
    sid = make_session(commits)

    a = claim(sid, "executor-1")   # index 3
    report(sid, a["commit"], "pass")
    b = claim(sid, "executor-1")   # index 5
    c = claim(sid, "executor-2")   # index 4
    assert (b["index"], c["index"]) == (5, 4)

    view = report(sid, b["commit"], "fail")  # range now (3, 5]: only c5,
    # and index 4 is still out for testing
    assert view["status"] == "running"       # must wait for the claimed test
    assert view["conclusion"] is None
    got = claim(sid, "executor-1")
    assert got["commit"] is None and got["reason"] == "waiting_for_reports"

    view = report(sid, c["commit"], "pass", "executor-2")
    assert view["status"] == "completed"
    assert view["conclusion"] == {"faulty_commit": "c6", "suspects": ["c6"]}


# ----------------------------------------------------------------- validation

def test_unknown_session_returns_404():
    assert client.get("/sessions/nope").status_code == 404
    assert client.post("/sessions/nope/claims", json={"executor": "e"}).status_code == 404
    resp = client.post("/sessions/nope/reports",
                       json={"commit": "c1", "result": "pass"})
    assert resp.status_code == 404


def test_report_requires_prior_claim():
    sid = make_session(["c1", "c2", "c3", "c4"])

    resp = client.post(f"/sessions/{sid}/reports",
                       json={"commit": "c2", "result": "pass"})
    assert resp.status_code == 409  # never dispatched

    got = claim(sid, "executor-1")
    assert got["commit"] == "c2"
    report(sid, "c2", "pass")
    resp = client.post(f"/sessions/{sid}/reports",
                       json={"commit": "c2", "result": "pass"})
    assert resp.status_code == 409  # already reported

    resp = client.post(f"/sessions/{sid}/reports",
                       json={"commit": "c1", "result": "pass"})
    assert resp.status_code == 409  # boundary commit, never dispatched

    resp = client.post(f"/sessions/{sid}/reports",
                       json={"commit": "zzz", "result": "pass"})
    assert resp.status_code == 404  # unknown commit


def test_session_creation_validation():
    resp = client.post("/sessions", json={"commits": ["only-one"]})
    assert resp.status_code == 422
    resp = client.post("/sessions", json={"commits": ["a", "b", "a"]})
    assert resp.status_code == 422
    resp = client.post("/sessions", json={"commits": ["a", ""]})
    assert resp.status_code == 422


def test_list_sessions():
    sid = make_session(["c1", "c2", "c3"], name="smoke")
    listing = client.get("/sessions").json()
    entry = next(s for s in listing if s["session_id"] == sid)
    assert entry["name"] == "smoke" and entry["status"] == "running"
