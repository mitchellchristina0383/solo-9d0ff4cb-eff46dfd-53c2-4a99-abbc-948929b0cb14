"""Core bisection domain logic (framework-free, unit-testable).

Model
-----
A test lead registers an ordered list of commits from one branch.  The first
commit is a known pass, the last is a known reproduction of the failure, and
the fault is assumed to persist once introduced (monotonic failure).

Executors *claim* commits to test.  Every commit is dispatched at most once.
Reports come back as ``pass`` / ``fail`` / ``build_failure``:

* ``pass``  -> commit is good; the lower bound of the suspect range moves up.
* ``fail``  -> commit is bad; the upper bound of the suspect range moves down.
* ``build_failure`` -> commit cannot be tested.  The range does NOT move, but
  the commit stays inside it and remains a possible fault origin.

Reports are accepted for any previously claimed commit, even one that has
meanwhile fallen outside the suspect range -- its result is still checked and
applied.  If an *earlier* commit reproduces the failure while a *later* commit
passes, the monotonicity assumption is violated: the session records the
contradictory pair and stops dispatching.

The session completes when every claimed test has reported back and no
untested commit remains that could narrow the range.  The conclusion is then
either the single fault-introducing commit or the remaining suspect list
(untunable build-failure commits plus the first known-bad commit).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from enum import Enum


class Result(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    BUILD_FAILURE = "build_failure"


class CommitState(str, Enum):
    PENDING = "pending"            # never dispatched
    CLAIMED = "claimed"            # dispatched, report outstanding
    PASSED = "passed"              # reported: test passed
    FAILED = "failed"              # reported: failure reproduced
    BUILD_FAILED = "build_failed"  # reported: commit does not build


class SessionStatus(str, Enum):
    RUNNING = "running"
    CONTRADICTION = "contradiction"  # monotonicity violated; dispatch halted
    COMPLETED = "completed"          # nothing left that can narrow the range


class UnknownCommitError(KeyError):
    """The reported commit is not part of this session."""


class NotClaimedError(RuntimeError):
    """The commit was never dispatched, or its result was already reported."""

    def __init__(self, commit: str, state: "CommitState"):
        self.commit = commit
        self.state = state
        super().__init__(
            f"commit {commit!r} is in state {state.value!r}; "
            "only a claimed, not-yet-reported commit can be reported"
        )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class BisectSession:
    """One bisection over an ordered commit list.  Thread-safe."""

    def __init__(self, commits: list[str], name: str | None = None):
        if len(commits) < 2:
            raise ValueError("need at least two commits (known pass + known fail)")
        if any(not isinstance(c, str) or not c.strip() for c in commits):
            raise ValueError("commit ids must be non-empty strings")
        if len(set(commits)) != len(commits):
            raise ValueError("commit ids must be unique")
        self.name = name
        self.commits = list(commits)
        self._index = {c: i for i, c in enumerate(self.commits)}
        n = len(commits)
        self.states = [CommitState.PENDING] * n
        # Boundary knowledge supplied by the test lead.
        self.states[0] = CommitState.PASSED
        self.states[n - 1] = CommitState.FAILED
        # Suspect range is (good_idx, bad_idx]: the fault was introduced by
        # one of these commits.
        self.good_idx = 0
        self.bad_idx = n - 1
        self.status = SessionStatus.RUNNING
        self.in_flight: dict[int, dict] = {}   # idx -> claim info
        self.reports: dict[int, dict] = {}     # idx -> report info
        self.contradiction: dict | None = None
        self.lock = threading.Lock()
        self._check_completion_locked()

    # ------------------------------------------------------------------ claim

    def claim(self, executor: str) -> dict:
        """Hand out one commit to test, or explain why none is available."""
        with self.lock:
            if self.status is SessionStatus.CONTRADICTION:
                return self._no_claim("contradiction",
                                      "dispatch halted: contradictory results recorded")
            if self.status is SessionStatus.COMPLETED:
                return self._no_claim("completed", "bisection already finished")
            candidates = [
                i for i in range(self.good_idx + 1, self.bad_idx)
                if self.states[i] is CommitState.PENDING
            ]
            if not candidates:
                if self.in_flight:
                    return self._no_claim(
                        "waiting_for_reports",
                        "no untested commit in the suspect range; "
                        "waiting for claimed tests to report back",
                    )
                # Defensive: _check_completion_locked should have caught this.
                return self._no_claim("no_testable_commits",
                                      "nothing left that can narrow the range")
            mid = (self.good_idx + self.bad_idx) / 2
            idx = min(candidates, key=lambda i: (abs(i - mid), i))
            self.states[idx] = CommitState.CLAIMED
            self.in_flight[idx] = {"executor": executor, "claimed_at": _utcnow()}
            return {
                "commit": self.commits[idx],
                "index": idx,
                "executor": executor,
                "reason": None,
                "detail": None,
            }

    def _no_claim(self, reason: str, detail: str) -> dict:
        return {"commit": None, "index": None, "executor": None,
                "reason": reason, "detail": detail}

    # ----------------------------------------------------------------- report

    def report(self, commit: str, result: Result, executor: str | None = None) -> None:
        """Record the outcome of a previously claimed commit.

        Accepted even if the commit has fallen outside the suspect range since
        it was dispatched, and even if the session is already in contradiction.
        """
        with self.lock:
            try:
                idx = self._index[commit]
            except KeyError:
                raise UnknownCommitError(commit) from None
            if self.states[idx] is not CommitState.CLAIMED:
                raise NotClaimedError(commit, self.states[idx])

            if result is Result.PASS:
                self.states[idx] = CommitState.PASSED
                if idx > self.good_idx:
                    self.good_idx = idx
            elif result is Result.FAIL:
                self.states[idx] = CommitState.FAILED
                if idx < self.bad_idx:
                    self.bad_idx = idx
            else:  # build failure: range untouched, commit stays a suspect
                self.states[idx] = CommitState.BUILD_FAILED

            self.in_flight.pop(idx, None)
            self.reports[idx] = {
                "result": result.value,
                "executor": executor,
                "reported_at": _utcnow(),
            }
            self._check_contradiction_locked()
            if self.status is SessionStatus.RUNNING:
                self._check_completion_locked()

    def _check_contradiction_locked(self) -> None:
        # good_idx = highest passing index, bad_idx = lowest failing index.
        # If they cross, an earlier commit reproduced the failure while a
        # later one passed -- the monotonicity assumption is broken.
        if self.good_idx >= self.bad_idx and self.contradiction is None:
            self.status = SessionStatus.CONTRADICTION
            self.contradiction = {
                "earlier_failed": {
                    "index": self.bad_idx,
                    "commit": self.commits[self.bad_idx],
                },
                "later_passed": {
                    "index": self.good_idx,
                    "commit": self.commits[self.good_idx],
                },
                "detail": (
                    f"earlier commit {self.commits[self.bad_idx]!r} reproduced "
                    f"the failure but later commit {self.commits[self.good_idx]!r} "
                    "passed; the fault does not persist monotonically. "
                    "Dispatch halted."
                ),
            }

    def _check_completion_locked(self) -> None:
        testable_pending = any(
            self.states[i] is CommitState.PENDING
            for i in range(self.good_idx + 1, self.bad_idx)
        )
        if not testable_pending and not self.in_flight:
            self.status = SessionStatus.COMPLETED

    # -------------------------------------------------------------- conclusion

    def conclusion(self) -> dict | None:
        """Fault-introducing commit, or remaining suspects, once completed."""
        if self.status is not SessionStatus.COMPLETED:
            return None
        suspects = [
            self.commits[i]
            for i in range(self.good_idx + 1, self.bad_idx)
            if self.states[i] is CommitState.BUILD_FAILED
        ]
        suspects.append(self.commits[self.bad_idx])
        faulty = suspects[0] if len(suspects) == 1 else None
        return {"faulty_commit": faulty, "suspects": suspects}

    # ---------------------------------------------------------------- snapshot

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "name": self.name,
                "status": self.status.value,
                "commits": list(self.commits),
                "known_good": {"index": self.good_idx,
                               "commit": self.commits[self.good_idx]},
                "known_bad": {"index": self.bad_idx,
                              "commit": self.commits[self.bad_idx]},
                "suspect_commits": self.commits[self.good_idx + 1: self.bad_idx + 1],
                "commit_states": [
                    {
                        "index": i,
                        "commit": c,
                        "state": self.states[i].value,
                        "executor": (
                            self.in_flight.get(i, {}).get("executor")
                            or (self.reports.get(i) or {}).get("executor")
                        ),
                    }
                    for i, c in enumerate(self.commits)
                ],
                "in_flight": [
                    {
                        "index": i,
                        "commit": self.commits[i],
                        "executor": info["executor"],
                        "claimed_at": info["claimed_at"],
                    }
                    for i, info in sorted(self.in_flight.items())
                ],
                "contradiction": self.contradiction,
                "conclusion": self.conclusion(),
            }
