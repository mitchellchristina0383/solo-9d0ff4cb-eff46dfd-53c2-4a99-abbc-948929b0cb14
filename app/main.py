"""FastAPI bisection service: locate the commit that introduced a CI failure.

Workflow (all endpoints also explorable via the Swagger UI at /docs):

1. ``POST /sessions``            -- test lead registers the ordered commit list
2. ``POST /sessions/{id}/claims``  -- executors claim commits (parallel safe,
                                      each commit dispatched at most once)
3. ``POST /sessions/{id}/reports`` -- executors report pass / fail /
                                      build_failure
4. ``GET  /sessions/{id}``         -- live status: suspect range, in-flight
                                      claims, contradiction, conclusion
5. ``GET  /sessions/{id}/result``  -- just the verdict once completed
"""

from __future__ import annotations

import threading
import uuid

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from .bisect import (
    BisectSession,
    CommitState,
    NotClaimedError,
    Result,
    SessionStatus,
    UnknownCommitError,
)

app = FastAPI(
    title="CI Bisect Service",
    version="1.0.0",
    summary="Parallel commit bisection to find the change that broke a CI test.",
)

_sessions: dict[str, BisectSession] = {}
_store_lock = threading.Lock()


# --------------------------------------------------------------------- models

class CreateSessionIn(BaseModel):
    commits: list[str] = Field(
        min_length=2,
        description="Commit ids from one branch, ordered oldest to newest. "
                    "commits[0] is a known pass, commits[-1] a known failure.",
        examples=[["a1", "b2", "c3", "d4", "e5"]],
    )
    name: str | None = Field(default=None, description="Optional label, e.g. the failing test name.")


class CommitRef(BaseModel):
    index: int
    commit: str


class SessionCreated(BaseModel):
    session_id: str
    name: str | None
    status: SessionStatus
    commit_count: int
    known_good: CommitRef
    known_bad: CommitRef


class ClaimIn(BaseModel):
    executor: str = Field(min_length=1, max_length=64,
                          description="Identifier of the claiming executor.",
                          examples=["executor-1"])


class ClaimView(BaseModel):
    session_id: str
    commit: str | None = Field(description="Commit to test; null when nothing can be dispatched.")
    index: int | None
    executor: str | None
    reason: str | None = Field(
        description="Why no commit was handed out: completed | contradiction | "
                    "waiting_for_reports | no_testable_commits")
    detail: str | None


class ReportIn(BaseModel):
    commit: str = Field(description="A commit previously handed out by a claim.")
    result: Result
    executor: str | None = Field(default=None, description="Reporter identifier (optional).")


class CommitStateView(BaseModel):
    index: int
    commit: str
    state: CommitState
    executor: str | None


class InFlightView(BaseModel):
    index: int
    commit: str
    executor: str
    claimed_at: str


class ContradictionView(BaseModel):
    earlier_failed: CommitRef
    later_passed: CommitRef
    detail: str


class ConclusionView(BaseModel):
    faulty_commit: str | None = Field(
        description="The commit that introduced the fault; null when build "
                    "failures leave more than one suspect.")
    suspects: list[str]


class SessionView(BaseModel):
    session_id: str
    name: str | None
    status: SessionStatus
    commits: list[str]
    known_good: CommitRef
    known_bad: CommitRef
    suspect_commits: list[str] = Field(
        description="Commits that may have introduced the fault: (known_good, known_bad].")
    commit_states: list[CommitStateView]
    in_flight: list[InFlightView]
    contradiction: ContradictionView | None
    conclusion: ConclusionView | None


class ResultView(BaseModel):
    session_id: str
    status: SessionStatus
    contradiction: ContradictionView | None
    conclusion: ConclusionView | None


class SessionSummary(BaseModel):
    session_id: str
    name: str | None
    status: SessionStatus
    commit_count: int


# --------------------------------------------------------------------- helpers

def _get_session(session_id: str) -> BisectSession:
    with _store_lock:
        session = _sessions.get(session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail=f"unknown session {session_id!r}")
    return session


def _view(session_id: str, session: BisectSession) -> SessionView:
    return SessionView(session_id=session_id, **session.snapshot())


# ---------------------------------------------------------------------- routes

@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"service": "ci-bisect", "docs": "/docs"}


@app.post("/sessions", response_model=SessionCreated,
          status_code=status.HTTP_201_CREATED, tags=["sessions"])
def create_session(body: CreateSessionIn) -> SessionCreated:
    """Register an ordered commit list and start a bisection."""
    try:
        session = BisectSession(body.commits, name=body.name)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    session_id = uuid.uuid4().hex[:12]
    with _store_lock:
        _sessions[session_id] = session
    snap = session.snapshot()
    return SessionCreated(
        session_id=session_id,
        name=snap["name"],
        status=snap["status"],
        commit_count=len(snap["commits"]),
        known_good=snap["known_good"],
        known_bad=snap["known_bad"],
    )


@app.get("/sessions", response_model=list[SessionSummary], tags=["sessions"])
def list_sessions() -> list[SessionSummary]:
    with _store_lock:
        items = list(_sessions.items())
    return [
        SessionSummary(session_id=sid, name=s.name, status=s.status,
                       commit_count=len(s.commits))
        for sid, s in items
    ]


@app.get("/sessions/{session_id}", response_model=SessionView, tags=["sessions"])
def get_session(session_id: str) -> SessionView:
    """Full status: suspect range, per-commit state, in-flight claims,
    contradiction (if any) and conclusion (once completed)."""
    return _view(session_id, _get_session(session_id))


@app.post("/sessions/{session_id}/claims", response_model=ClaimView, tags=["bisect"])
def claim(session_id: str, body: ClaimIn) -> ClaimView:
    """Claim the next commit to test.  Parallel-safe: each commit is
    dispatched at most once.  ``commit`` is null when nothing can be
    dispatched right now (see ``reason``)."""
    session = _get_session(session_id)
    return ClaimView(session_id=session_id, **session.claim(body.executor))


@app.post("/sessions/{session_id}/reports", response_model=SessionView, tags=["bisect"])
def report(session_id: str, body: ReportIn) -> SessionView:
    """Report the outcome of a claimed commit.  Reports are accepted (and
    checked) even for commits that have fallen outside the suspect range
    since they were dispatched."""
    session = _get_session(session_id)
    try:
        session.report(body.commit, body.result, executor=body.executor)
    except UnknownCommitError:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            detail=f"commit {body.commit!r} is not in session "
                                   f"{session_id!r}") from None
    except NotClaimedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return _view(session_id, session)


@app.get("/sessions/{session_id}/result", response_model=ResultView, tags=["bisect"])
def get_result(session_id: str) -> ResultView:
    """The verdict: contradiction details, or the fault-introducing commit /
    remaining suspects once the bisection has completed."""
    session = _get_session(session_id)
    snap = session.snapshot()
    return ResultView(session_id=session_id, status=snap["status"],
                      contradiction=snap["contradiction"],
                      conclusion=snap["conclusion"])
