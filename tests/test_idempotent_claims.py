"""Tests for idempotent claims driven by an optional ``request_id``.

Covers the lost-response fix:

* same (executor, request_id) replay returns the original dispatch, even when
  the commit has left the suspect range or dispatch has been halted;
* concurrent same-key claims dispatch exactly one commit;
* replay after the original commit was reported says ``already_reported`` and
  leaves the in-flight list unchanged;
* a no-claim answer is not remembered -- a same-number retry re-evaluates the
  current dispatch state;
* new request numbers, different executors and legacy (number-less) clients
  keep their original behaviour.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.bisect import BisectSession, CommitState, Result, SessionStatus
from app.main import app

client = TestClient(app)


def make_session(commits: list[str]) -> str:
    resp = client.post("/sessions", json={"commits": commits})
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


# ------------------------------------------------------------- core: replay

def test_retry_same_request_id_returns_same_commit():
    session = BisectSession([f"c{i}" for i in range(1, 11)])

    first = session.claim("executor-1", "req-1")
    assert first["replayed"] is False
    assert first["request_id"] == "req-1"

    second = session.claim("executor-1", "req-1")
    assert second["replayed"] is True
    assert second["request_id"] == "req-1"
    assert second["commit"] == first["commit"]
    assert second["index"] == first["index"]
    assert second["reason"] is None

    # the commit stays dispatched exactly once
    snap = session.snapshot()
    assert len(snap["in_flight"]) == 1
    assert snap["in_flight"][0]["commit"] == first["commit"]
    assert snap["in_flight"][0]["request_id"] == "req-1"


def test_concurrent_same_key_claims_dispatch_once():
    session = BisectSession([f"c{i}" for i in range(1, 41)])
    answers: list[dict] = []
    guard = threading.Lock()

    def worker() -> None:
        got = session.claim("executor-1", "hot-req")
        with guard:
            answers.append(got)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(answers) == 16
    assert {a["index"] for a in answers} == {answers[0]["index"]}
    assert sum(1 for a in answers if not a["replayed"]) == 1
    assert sum(1 for a in answers if a["replayed"]) == 15
    assert all(a["request_id"] == "hot-req" for a in answers)
    assert len(session.snapshot()["in_flight"]) == 1
    assert ("executor-1", "hot-req") in session.claim_keys


def test_retry_recovers_commit_that_left_suspect_range():
    session = BisectSession([f"c{i}" for i in range(1, 11)])

    low = session.claim("executor-1", "low-req")    # index 4
    high = session.claim("executor-2", "high-req")  # index 5
    # later commit reports first: good bound moves past the earlier claim
    session.report(high["commit"], Result.PASS)
    assert session.good_idx == 5 and low["index"] < session.good_idx

    retry = session.claim("executor-1", "low-req")
    assert retry["replayed"] is True
    assert retry["commit"] == low["commit"]
    assert retry["index"] == low["index"]

    # still exactly one in-flight entry, still the original dispatch
    in_flight = session.snapshot()["in_flight"]
    assert [x["commit"] for x in in_flight] == [low["commit"]]

    # the executor can report using the recovered commit
    session.report(low["commit"], Result.PASS)
    assert session.states[low["index"]] is CommitState.PASSED


def test_retry_after_contradiction_returns_outstanding_commit():
    session = BisectSession([f"c{i}" for i in range(1, 7)])

    a = session.claim("executor-1", "a-req")      # index 2
    b = session.claim("executor-2", "b-req")      # index 3
    extra = session.claim("executor-3", "extra")  # index 4, stays outstanding

    session.report(a["commit"], Result.FAIL)
    session.report(b["commit"], Result.PASS)
    assert session.status is SessionStatus.CONTRADICTION

    # a fresh request number cannot dispatch anything anymore
    fresh = session.claim("executor-1", "fresh-req")
    assert fresh["commit"] is None
    assert fresh["reason"] == "contradiction"
    assert fresh["replayed"] is False

    # ... but the outstanding request number still recovers its commit
    retry = session.claim("executor-3", "extra")
    assert retry["replayed"] is True
    assert retry["commit"] == extra["commit"]
    assert retry["index"] == extra["index"]


# ------------------------------------------------------ core: after report

def test_same_key_after_report_says_already_reported():
    session = BisectSession([f"c{i}" for i in range(1, 9)])

    first = session.claim("executor-1", "req-1")
    session.report(first["commit"], Result.FAIL)

    retry = session.claim("executor-1", "req-1")
    assert retry["replayed"] is True
    assert retry["reason"] == "already_reported"
    assert retry["commit"] == first["commit"]
    assert retry["index"] == first["index"]
    assert "fail" in retry["detail"]
    # nothing added to the in-flight list (it was and stays empty here)
    assert session.snapshot()["in_flight"] == []


def test_already_reported_keeps_other_in_flight_untouched():
    session = BisectSession([f"c{i}" for i in range(1, 11)])

    a = session.claim("executor-1", "r1")
    b = session.claim("executor-2", "r2")
    session.report(a["commit"], Result.PASS)

    retry = session.claim("executor-1", "r1")
    assert retry["reason"] == "already_reported"
    assert retry["commit"] == a["commit"]

    # the other executor's dispatch is still the sole in-flight entry
    in_flight = session.snapshot()["in_flight"]
    assert [x["commit"] for x in in_flight] == [b["commit"]]


def test_build_failure_replay_is_also_already_reported():
    session = BisectSession([f"c{i}" for i in range(1, 7)])
    first = session.claim("executor-1", "bf")
    session.report(first["commit"], Result.BUILD_FAILURE)

    retry = session.claim("executor-1", "bf")
    assert retry["reason"] == "already_reported"
    assert retry["commit"] == first["commit"]
    assert "build_failure" in retry["detail"]


# ------------------------------------------------------- core: no-claim retry

def test_no_claim_is_not_remembered_and_retry_reevaluates():
    # a two-commit session is completed before any claim
    session = BisectSession(["good", "bad"])

    first = session.claim("executor-1", "r1")
    assert first["commit"] is None
    assert first["reason"] == "completed"
    assert first["replayed"] is False
    assert first["request_id"] == "r1"
    assert ("executor-1", "r1") not in session.claim_keys

    second = session.claim("executor-1", "r1")
    assert second["reason"] == "completed"
    assert second["replayed"] is False  # fresh evaluation, not a replay


def test_waiting_retry_later_reflects_completion():
    session = BisectSession([f"c{i}" for i in range(1, 9)])
    # drain every interior commit with their own request numbers
    claimed = []
    for n in range(6):
        got = session.claim("pool", f"req-{n}")
        assert got["commit"] is not None
        claimed.append(got)

    late = session.claim("late-executor", "late")
    assert late["commit"] is None
    assert late["reason"] == "waiting_for_reports"
    assert ("late-executor", "late") not in session.claim_keys

    for got in claimed:
        session.report(got["commit"], Result.PASS)
    # all passes below the known-bad tail: session completes
    assert session.status is SessionStatus.COMPLETED

    retry = session.claim("late-executor", "late")
    assert retry["reason"] == "completed"
    assert retry["replayed"] is False


# ------------------------------------------------------ core: key isolation

def test_new_request_id_claims_another_commit():
    session = BisectSession([f"c{i}" for i in range(1, 21)])

    a = session.claim("executor-1", "r1")
    b = session.claim("executor-1", "r2")
    assert a["replayed"] is False and b["replayed"] is False
    assert a["index"] != b["index"]
    assert len(session.snapshot()["in_flight"]) == 2


def test_same_number_different_executors_are_independent():
    session = BisectSession([f"c{i}" for i in range(1, 11)])

    a = session.claim("executor-1", "shared-number")
    b = session.claim("executor-2", "shared-number")
    assert a["index"] != b["index"]

    assert session.claim("executor-1", "shared-number")["index"] == a["index"]
    assert session.claim("executor-2", "shared-number")["index"] == b["index"]


def test_legacy_claim_without_request_id_dispatches_every_call():
    session = BisectSession([f"c{i}" for i in range(1, 11)])

    a = session.claim("executor-1")
    b = session.claim("executor-1")
    assert a["request_id"] is None and b["request_id"] is None
    assert a["replayed"] is False and b["replayed"] is False
    assert a["index"] != b["index"]
    assert session.claim_keys == {}


# ----------------------------------------------------------------- API level

def test_api_idempotent_claim_lifecycle():
    sid = make_session([f"c{i}" for i in range(1, 11)])

    resp = client.post(
        f"/sessions/{sid}/claims",
        json={"executor": "executor-1", "request_id": "req-42"},
    )
    assert resp.status_code == 200
    first = resp.json()
    assert first["request_id"] == "req-42"
    assert first["replayed"] is False

    # simulated lost response: same number retried
    second = client.post(
        f"/sessions/{sid}/claims",
        json={"executor": "executor-1", "request_id": "req-42"},
    ).json()
    assert second["replayed"] is True
    assert second["commit"] == first["commit"]

    view = client.get(f"/sessions/{sid}").json()
    assert len(view["in_flight"]) == 1
    assert view["in_flight"][0]["request_id"] == "req-42"

    # report the recovered commit, then retry again
    report = client.post(
        f"/sessions/{sid}/reports",
        json={"commit": first["commit"], "result": "fail",
              "executor": "executor-1"},
    )
    assert report.status_code == 200

    third = client.post(
        f"/sessions/{sid}/claims",
        json={"executor": "executor-1", "request_id": "req-42"},
    ).json()
    assert third["reason"] == "already_reported"
    assert third["commit"] == first["commit"]
    assert third["replayed"] is True
    # in-flight list unchanged by the replay (the reported commit is gone)
    assert client.get(f"/sessions/{sid}").json()["in_flight"] == []


def test_api_concurrent_same_key_dispatches_once():
    sid = make_session([f"c{i}" for i in range(1, 51)])
    answers: list[dict] = []
    guard = threading.Lock()

    def worker() -> None:
        local_client = TestClient(app)
        resp = local_client.post(
            f"/sessions/{sid}/claims",
            json={"executor": "executor-1", "request_id": "race"},
        )
        with guard:
            answers.append(resp.json())

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(answers) == 12
    assert {a["index"] for a in answers} == {answers[0]["index"]}
    assert sum(1 for a in answers if not a["replayed"]) == 1
    assert len(client.get(f"/sessions/{sid}").json()["in_flight"]) == 1


def test_api_legacy_claim_shape_remains_valid():
    sid = make_session([f"c{i}" for i in range(1, 11)])
    resp = client.post(f"/sessions/{sid}/claims", json={"executor": "old-exec"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["request_id"] is None
    assert body["replayed"] is False
    assert body["reason"] is None and body["commit"] is not None


def test_api_empty_request_id_rejected():
    sid = make_session(["a", "b", "c", "d"])
    resp = client.post(
        f"/sessions/{sid}/claims",
        json={"executor": "executor-1", "request_id": ""},
    )
    assert resp.status_code == 422


def test_api_no_claim_echoes_request_id():
    sid = make_session(["good", "bad"])
    body = client.post(
        f"/sessions/{sid}/claims",
        json={"executor": "executor-1", "request_id": "zz"},
    ).json()
    assert body["reason"] == "completed"
    assert body["request_id"] == "zz"
    assert body["replayed"] is False
