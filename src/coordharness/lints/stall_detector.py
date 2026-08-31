#!/usr/bin/env python3
"""Conservative final-message stall detection for agent handoffs.

The failure mode this module targets: an agent spawns child work (a subagent,
a workflow, a background job) and then ends its own turn immediately after,
leaving that child invisible to anyone reading the board -- no claim, no
heartbeat, nothing tracking it. `docs/coordination-model.md` describes this
failure mode in prose; this module is a first attempt at recognizing it from
run data.

This module is intentionally pure at its core: `detect_stall()` does not send
messages, mutate coord.db, or inspect anything beyond the strings and counts
it is handed. The coord.db-facing half (`scan_coord_db()`) opens a read-only
connection, reads `runs` and `run_events`, and returns candidate verdicts --
it never writes to the database and never nudges an agent. Wiring either half
to an automatic nudge, a hook, or a `doctor`-style check is a decision for a
future caller once production examples clear a precision bar; v1 is
record-only by construction (`should_auto_nudge` defaults to `False` and
nothing in this module calls it).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from coordharness import config as _harness_config
from coordharness.coord.coord_db import TERMINAL_RUN_STATES

_DB = str(_harness_config.coord_db_path())

# Tool/event names that plausibly spawn independent child work. Deliberately
# generic -- any host application can extend this by passing its own
# `tool_uses` into `detect_stall()` directly; this frozenset only matters for
# the coord.db scanner's own event-derived tool list.
KNOWN_SPAWN_TOOL_NAMES = frozenset(
    {
        "agent",
        "task",
        "workflow",
        "spawn_agent",
        "spawn_agents",
        "spawn_task",
        "start_workflow",
    }
)

WAITING_LANGUAGE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(wait(?:ing)?|monitor(?:ing)?|poll(?:ing)?|checking back)\b.*\b(agent|subagent|workflow|task|job|process)\b",
        # Requires the subject word to be the grammatical subject of an ongoing
        # state (a copula immediately followed by the state word), not merely
        # co-occurring anywhere earlier in the message with `.*` in between --
        # that older shape matched completion prose like "the agent finished
        # running the suite" or "workflow ... working set" just as eagerly as
        # a genuine "still running" report (MEASURED: 5/5 self-authored benign
        # completion messages false-positived under the old `.*`-spanning form;
        # see test_waiting_language_pattern_does_not_match_benign_completion_prose).
        r"\b(agent|subagent|workflow|task)\b\s+(?:is|are|was|remains|keeps)\s+(?:still\s+|currently\s+)?(running|working|in progress)\b",
        r"\b(i(?:'| a)?ll|will)\s+(?:wait|monitor|check back|continue when)\b",
        r"\bnot(?:hing)?\s+(?:else\s+)?(?:to\s+)?(?:report|do)\s+(?:until|while)\b",
    )
)

# Evidence that the "still running" work is a *tracked* background job rather
# than an untracked spawn -- generic vocabulary only (no host-specific paths).
MANAGED_JOB_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bdone_signal\b",
        r"\bsidecar\b.*\b(job|progress|telemetry)\b",
        r"\btracked\s+(background\s+)?job\b",
        r"\bmanaged[- ]background\b",
        r"\bjob[_ ]progress\b",
        r"\bheartbeat(?:ing|ed)?\b",
    )
)


@dataclass(frozen=True)
class StallVerdict:
    """Classifier output for one agent final response."""

    is_stall: bool
    confidence: float
    reasons: tuple[str, ...]
    action: str
    auto_nudge_enabled: bool = False

    @property
    def should_auto_nudge(self) -> bool:
        """True only after a future caller explicitly enables nudges."""

        return self.is_stall and self.auto_nudge_enabled


def _tool_name(tool: object) -> str:
    if isinstance(tool, str):
        return tool
    if isinstance(tool, Mapping):
        for key in ("name", "tool_name", "recipient_name", "type"):
            value = tool.get(key)
            if value:
                return str(value)
    return str(tool or "")


def _has_waiting_language(message: str) -> bool:
    return any(pattern.search(message) for pattern in WAITING_LANGUAGE_PATTERNS)


def _has_managed_job_evidence(message: str) -> bool:
    return any(pattern.search(message) for pattern in MANAGED_JOB_PATTERNS)


def detect_stall(
    final_message: str,
    *,
    tool_uses: Sequence[object] | None = None,
    duration_seconds: float | None = None,
    has_live_child: bool = False,
    managed_job_artifact: bool = False,
    auto_nudge_enabled: bool = False,
) -> StallVerdict:
    """Classify whether an agent final message likely ended too early.

    A tracked long-running job is not a stall just because the final message
    says it is still running. The detector only flags terminal messages that
    leave untracked child-agent/workflow work invisible to the board.
    """

    message = (final_message or "").strip()
    tool_names = tuple(_tool_name(tool) for tool in (tool_uses or ()))
    spawn_count = sum(1 for name in tool_names if name.lower() in KNOWN_SPAWN_TOOL_NAMES)
    managed_job = managed_job_artifact or _has_managed_job_evidence(message)

    reasons: list[str] = []
    if _has_waiting_language(message) and not managed_job:
        reasons.append("waiting_language_without_managed_job")
    if spawn_count and len(tool_names) <= 2 and not managed_job:
        reasons.append("spawned_agent_with_low_followup_tool_count")
    if has_live_child and duration_seconds is not None and duration_seconds < 60 and not managed_job:
        reasons.append("short_parent_turn_with_live_child")

    if not reasons:
        return StallVerdict(
            is_stall=False,
            confidence=0.0,
            reasons=(),
            action="record_only",
            auto_nudge_enabled=auto_nudge_enabled,
        )

    confidence = min(0.95, 0.55 + 0.15 * len(reasons))
    return StallVerdict(
        is_stall=True,
        confidence=round(confidence, 2),
        reasons=tuple(reasons),
        action="record_only_auto_nudge_disabled",
        auto_nudge_enabled=auto_nudge_enabled,
    )


def evaluate_labeled_examples(examples: Iterable[Mapping[str, object]]) -> dict[str, float | int]:
    """Return precision/recall metrics for a small labeled fixture."""

    tp = fp = tn = fn = 0
    for example in examples:
        raw_tool_uses = example.get("tool_uses")
        raw_duration = example.get("duration_seconds")
        verdict = detect_stall(
            str(example.get("final_message", "")),
            tool_uses=raw_tool_uses if isinstance(raw_tool_uses, Sequence) else None,
            duration_seconds=raw_duration if isinstance(raw_duration, (int, float)) else None,
            has_live_child=bool(example.get("has_live_child", False)),
            managed_job_artifact=bool(example.get("managed_job_artifact", False)),
        )
        expected = bool(example.get("is_stall"))
        if verdict.is_stall and expected:
            tp += 1
        elif verdict.is_stall and not expected:
            fp += 1
        elif not verdict.is_stall and expected:
            fn += 1
        else:
            tn += 1

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "examples": tp + fp + tn + fn,
        "true_positive": tp,
        "false_positive": fp,
        "true_negative": tn,
        "false_negative": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
    }


# --- coord.db-facing scanner --------------------------------------------
#
# Everything below reads `runs` and `run_events` through a read-only
# connection and never writes. A "candidate" is a finished (terminal-state)
# run whose session has at least one still-non-terminal (live/running/
# waiting/reserved/...) child run underneath it -- the shape the failure mode
# produces: the parent turn ended, but it left work running that nothing else
# is tracking.


@dataclass(frozen=True)
class RunRow:
    run_id: str
    work_id: str
    session_id: str
    parent_session_id: str
    started_at: float
    finished_at: float | None
    state: str


@dataclass(frozen=True)
class StallCandidate:
    """One finished run flagged as a likely stall, with its verdict."""

    run_id: str
    work_id: str
    session_id: str
    verdict: StallVerdict


def _run_events_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_events'"
    ).fetchone()
    return row is not None


def _load_runs(conn: sqlite3.Connection) -> list[RunRow]:
    cols = "run_id, work_id, session_id, parent_session_id, started_at, finished_at, state"
    rows = conn.execute(f"SELECT {cols} FROM runs").fetchall()
    return [
        RunRow(
            run_id=str(r["run_id"] or ""),
            work_id=str(r["work_id"] or ""),
            session_id=str(r["session_id"] or ""),
            parent_session_id=str(r["parent_session_id"] or ""),
            started_at=float(r["started_at"]) if r["started_at"] is not None else 0.0,
            finished_at=float(r["finished_at"]) if r["finished_at"] is not None else None,
            state=str(r["state"] or ""),
        )
        for r in rows
    ]


def _content_text(raw: str | None) -> str:
    """Best-effort text extraction from a run_events content_json blob."""

    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return str(raw)
    if isinstance(payload, str):
        return payload
    if isinstance(payload, Mapping):
        for key in ("text", "message", "body", "content", "final_message"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _run_event_evidence(conn: sqlite3.Connection, run_id: str) -> tuple[str, list[str], bool]:
    """Return (final_message, tool_names_in_order, managed_job_evidence) for a run."""

    if not _run_events_table_exists(conn):
        return "", [], False

    rows = conn.execute(
        "SELECT category, event_type, content_json, metadata_json"
        " FROM run_events WHERE run_id = ? ORDER BY seq ASC",
        (run_id,),
    ).fetchall()

    final_message = ""
    tool_names: list[str] = []
    managed_job = False
    for row in rows:
        category = str(row["category"] or "")
        blob_text = _content_text(row["content_json"]) + " " + _content_text(row["metadata_json"])
        if _has_managed_job_evidence(blob_text):
            managed_job = True
        if category == "message":
            text = _content_text(row["content_json"])
            if text:
                final_message = text
        elif category == "tool":
            try:
                payload = json.loads(row["content_json"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            name = ""
            if isinstance(payload, Mapping):
                name = str(payload.get("tool_name") or "")
            tool_names.append(name)

    return final_message, tool_names, managed_job


def scan_coord_db(
    db_path: str | None = None,
    *,
    auto_nudge_enabled: bool = False,
) -> list[StallCandidate]:
    """Scan coord.db for finished runs that likely stalled with live children.

    Read-only: opens the database with `mode=ro` and issues no writes. This
    function only classifies and returns candidates -- it is the caller's
    decision (not this module's) whether to surface, log, or ever act on them.
    """

    path = db_path or _DB
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        runs = _load_runs(conn)

        # Non-terminal is the coord_db.TERMINAL_RUN_STATES complement, not just
        # state=='live': the real schema also uses 'running'/'waiting' (see
        # coord_db.py's own `state IN ('live','running','waiting')` guards) and
        # 'reserved' (roadmap_binding.py's reservation insert). Treating only
        # 'live' as non-terminal missed candidates where the still-running
        # child (or the finished parent) used any of those other states.
        live_parent_sessions: set[str] = {
            r.parent_session_id
            for r in runs
            if r.parent_session_id and r.state.strip().lower() not in TERMINAL_RUN_STATES
        }

        candidates: list[StallCandidate] = []
        for run in runs:
            if run.state.strip().lower() not in TERMINAL_RUN_STATES:
                continue
            if not run.session_id or run.session_id not in live_parent_sessions:
                continue

            final_message, tool_names, managed_job = _run_event_evidence(conn, run.run_id)
            duration = (
                run.finished_at - run.started_at
                if run.finished_at is not None
                else None
            )
            verdict = detect_stall(
                final_message,
                tool_uses=tool_names,
                duration_seconds=duration,
                has_live_child=True,
                managed_job_artifact=managed_job,
                auto_nudge_enabled=auto_nudge_enabled,
            )
            if verdict.is_stall:
                candidates.append(
                    StallCandidate(
                        run_id=run.run_id,
                        work_id=run.work_id,
                        session_id=run.session_id,
                        verdict=verdict,
                    )
                )
        return candidates
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Record-only scan for finished runs that likely stalled with live, "
            "untracked child work still under them. Never writes to coord.db "
            "and never nudges an agent."
        )
    )
    ap.add_argument("--db", default=_DB)
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()

    candidates = scan_coord_db(args.db)

    if args.json:
        print(
            json.dumps(
                {
                    "schema": "coordharness.stall-detector.v1",
                    "mode": "RECORD-ONLY (no writes, no auto-nudge)",
                    "candidate_count": len(candidates),
                    "candidates": [
                        {
                            "run_id": c.run_id,
                            "work_id": c.work_id,
                            "session_id": c.session_id,
                            "confidence": c.verdict.confidence,
                            "reasons": list(c.verdict.reasons),
                        }
                        for c in candidates
                    ],
                },
                indent=2,
            )
        )
    else:
        print(f"stall detector (RECORD-ONLY) - {len(candidates)} candidate(s)")
        for c in candidates:
            print(f"  {c.run_id} [{c.work_id}] confidence={c.verdict.confidence} reasons={list(c.verdict.reasons)}")
        if candidates:
            print("\nRECORD-ONLY - not wired to any nudge or gate. Review manually.")

    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "KNOWN_SPAWN_TOOL_NAMES",
    "StallVerdict",
    "StallCandidate",
    "RunRow",
    "detect_stall",
    "evaluate_labeled_examples",
    "scan_coord_db",
]
