from __future__ import annotations

from typing import Optional, TypedDict

ALL_DERIVED_STATUSES: tuple[str, ...] = (
    "archived", "superseded", "cancelled", "canceled", "closed",
    "failed", "done", "attention",
    "running", "blocked", "paused", "queued", "planned",
)

STATUS_VOCAB_MAP: dict[str, str] = {
    "archived": "done",
    "superseded": "done",
    "cancelled": "done",
    "canceled": "done",
    "closed": "done",
    "done": "done",
    "failed": "failed",
    "running": "running",
    "blocked": "blocked",
    "paused": "blocked",
    "attention": "queued",
    "queued": "queued",
    "planned": "queued",
}

_LIST_FOR: dict[str, str] = {
    "running": "running_rows",
    "blocked": "attention_rows",
    "paused": "attention_rows",
    "attention": "attention_rows",
    "failed": "attention_rows",
    "queued": "next_rows",
    "planned": "next_rows",
    "done": "done_rows",
    "archived": "done_rows",
    "superseded": "done_rows",
    "cancelled": "done_rows",
    "canceled": "done_rows",
    "closed": "done_rows",
}


def bucket(raw_status: str) -> str:
    return STATUS_VOCAB_MAP.get(str(raw_status or "").lower(), "queued")


def list_for(raw_status: str) -> str:
    return _LIST_FOR.get(str(raw_status or "").lower(), "next_rows")


def assert_vocab_total() -> None:
    missing_b = [s for s in ALL_DERIVED_STATUSES if s not in STATUS_VOCAB_MAP]
    missing_l = [s for s in ALL_DERIVED_STATUSES if s not in _LIST_FOR]
    if missing_b or missing_l:
        raise AssertionError(f"STATUS_VOCAB_MAP not total: bucket-missing={missing_b} list-missing={missing_l}")


class BoardRow(TypedDict, total=False):
    work_id: str
    title: Optional[str]
    display: Optional[str]
    domain: Optional[str]
    module: Optional[str]
    lane: Optional[str]
    sublane: Optional[str]
    group: str
    raw_status: str
    status: str
    proof_state: Optional[str]
    assignee: Optional[str]
    owner_session_id: Optional[str]
    owner_session_label: Optional[str]
    owner_session_actor: Optional[str]
    owner_conversation_title: Optional[str]
    owner_external_thread_id: Optional[str]
    claim_status: Optional[str]
    claim_step: Optional[str]
    has_artifact: bool
    acceptance_json: Optional[str]
    context_pack_ref: Optional[str]
    done_signal: Optional[str]
    rubric_state: Optional[str]
    rubric_verdict: Optional[str]
    token_budget: Optional[int]
    due_date: Optional[float]
    visibility: Optional[str]
    priority: int
    pid: Optional[int]
    resource_class: Optional[str]
    runner_kind: Optional[str]
    eta_s: Optional[float]
    stale: bool
    display_status: Optional[str]


class SessionRow(TypedDict, total=False):
    session_id: str
    actor: str
    runner_type: Optional[str]
    live: bool
    child_sessions: int
    child_runs: int
    lease_until: Optional[float]
    pid: Optional[int]
    human_label: Optional[str]
    external_thread_id: Optional[str]
    conversation_title: Optional[str]
    worktree_id: Optional[str]
    label_source: Optional[str]


class EventRow(TypedDict, total=False):
    event_id: int
    ts: float
    kind: str
    actor: Optional[str]
    work_id: Optional[str]
    title: Optional[str]
    body: Optional[str]
    severity: Optional[str]


class WorkModel(TypedDict, total=False):
    running_rows: list[BoardRow]
    attention_rows: list[BoardRow]
    next_rows: list[BoardRow]
    done_rows: list[BoardRow]
    counts: dict[str, int]


class BoardState(TypedDict, total=False):
    work_model: WorkModel
    sessions: list[SessionRow]
    mode: str
    generated_at: float
    source: str
    group_by: str
