"""Seed a database with a fictional project so the harness can be explored.

Every row here is invented. Nothing is derived from any real board, and the
generator is deterministic, so two people running `coord demo seed` get the
same board and can compare notes.

The scenario is a small team porting a payments service, staffed by three
agents working different parts of it. It exists to exercise the shapes that
matter -- an epic with children, a blocked row with a reason, a parked row with
a resume condition, a handoff between agents, a review that has not happened
yet -- rather than to look impressive.
"""

from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from . import config
from .coord import config as coord_config
from .bootstrap import bootstrap_database
from .coord import coord_db
from .coord import native_cockpit

# A fixed clock so seeded boards are reproducible. 2026-03-02T09:00:00Z.
EPOCH = 1772442000.0
HOUR = 3600.0

# ---------------------------------------------------------------------------
# The fictional estate
# ---------------------------------------------------------------------------
# A mid-sized product team running a mixed fleet: a few chat agents, a couple of
# cloud runners, a local GPU box, and scheduled background work. Everything here
# is invented. The point is to exercise the shapes a real board develops -- work
# nested under initiatives, several modules, agents of different kinds holding
# claims, jobs that outlive the agent that started them, rows blocked on other
# rows, and rows parked with a condition for coming back.

AGENTS: tuple[dict[str, Any], ...] = (
    {"session_id": "claude:frontend", "actor": "claude", "runner_type": "claude_chat",
     "human_label": "Claude (frontend)"},
    {"session_id": "claude:platform", "actor": "claude", "runner_type": "claude_chat",
     "human_label": "Claude (platform)"},
    {"session_id": "claude:cloud-a", "actor": "claude", "runner_type": "workflow",
     "human_label": "Claude cloud runner A"},
    {"session_id": "codex:backend", "actor": "codex", "runner_type": "codex",
     "human_label": "Codex (backend)"},
    {"session_id": "codex:review", "actor": "codex", "runner_type": "codex",
     "human_label": "Codex (review)"},
    {"session_id": "codex:cloud-b", "actor": "codex", "runner_type": "workflow",
     "human_label": "Codex cloud runner B"},
    {"session_id": "local:gpu", "actor": "local", "runner_type": "local_gpu",
     "human_label": "Local GPU box"},
    {"session_id": "service:scheduler", "actor": "service", "runner_type": "background",
     "human_label": "Scheduled runner"},
)

# Initiatives. Work nests underneath these, which is what gives the board its
# grouping and lets a hundred rows stay readable.
EPICS: tuple[dict[str, Any], ...] = (
    {"work_id": "INIT-UI", "title": "UI overhaul", "display": "UI overhaul",
     "module": "ui", "domain": "product"},
    {"work_id": "INIT-MODEL", "title": "Model development", "display": "Model development",
     "module": "ml", "domain": "ml"},
    {"work_id": "INIT-PLATFORM", "title": "Platform migration", "display": "Platform migration",
     "module": "platform", "domain": "platform"},
    {"work_id": "INIT-SEARCH", "title": "Search relevance", "display": "Search relevance",
     "module": "search", "domain": "ml"},
    {"work_id": "INIT-OPS", "title": "Operational hardening", "display": "Operational hardening",
     "module": "infra", "domain": "platform"},
)

# (work_id, title, module, domain, assignee, state, priority, step/note)
_WORK: tuple[tuple, ...] = (
    # UI overhaul
    ("UI-101", "Rebuild the settings screen", "ui", "product", "claude", "running", 1,
     "splitting the preferences panel"),
    ("UI-102", "Design tokens for dark mode", "ui", "product", "claude", "running", 2,
     "auditing contrast on the muted palette"),
    ("UI-103", "Replace the legacy table component", "ui", "product", "codex", "running", 3,
     "replacing it screen by screen; four of nine done"),
    ("UI-104", "Keyboard navigation pass", "ui", "product", "claude", "queued", 4, None),
    ("UI-105", "Empty and loading states", "ui", "product", "claude", "planned", 6, None),
    ("UI-106", "Responsive breakpoints", "ui", "product", None, "planned", 7, None),
    ("UI-107", "Accessibility audit", "ui", "product", None, "planned", 8, None),
    ("UI-108", "Retire the old theme switcher", "ui", "product", "claude", "parked", 9,
     "Delete once UI-102 has shipped and no screen still imports the old tokens."),
    # Model development
    ("ML-201", "Embedding model evaluation", "ml", "ml", "claude", "running", 1,
     "scoring candidate checkpoints"),
    ("ML-202", "Training data deduplication", "ml", "ml", "claude", "blocked", 2,
     "Upstream export is incomplete before the 5th of the month."),
    ("ML-203", "Reranker fine-tune", "ml", "ml", "codex", "running", 3,
     "epoch 2 of 6 on the held-out split"),
    ("ML-204", "Quantisation for the local runtime", "ml", "ml", "codex", "queued", 4, None),
    ("ML-205", "Offline evaluation harness", "ml", "ml", "codex", "done", 5, None),
    ("ML-206", "Prompt regression suite", "ml", "ml", "claude", "planned", 6, None),
    ("ML-207", "Model card and limitations", "ml", "ml", None, "planned", 9, None),
    # Platform migration
    ("PLT-301", "Move the job runner off the shared disk", "platform", "platform", "codex",
     "running", 1, "draining the old queue"),
    ("PLT-302", "Connection pooling for the API", "api", "platform", "codex", "queued", 2, None),
    ("PLT-303", "Structured logging everywhere", "platform", "platform", "claude", "running", 3,
     "converting the request path first"),
    ("PLT-304", "Retire the dual-write shim", "platform", "platform", "claude", "parked", 5,
     "Remove once PLT-301 has run a full week with no reconciliation drift."),
    ("PLT-305", "Secrets rotation runbook", "infra", "platform", None, "planned", 7, None),
    ("PLT-306", "Blue/green deploy path", "infra", "platform", "codex", "planned", 8, None),
    # Search relevance
    ("SRCH-401", "Rebuild the inverted index", "search", "ml", "codex", "running", 1,
     "reindexing shard 3 of 8"),
    ("SRCH-402", "Query expansion experiments", "search", "ml", "claude", "queued", 3, None),
    ("SRCH-403", "Latency budget for the search path", "search", "ml", "codex", "blocked", 2,
     "Waiting on PLT-302; pooling changes the numbers."),
    ("SRCH-404", "Relevance judgement set", "search", "ml", None, "planned", 6, None),
    # Operational hardening
    ("OPS-501", "Alert on stale job telemetry", "infra", "platform", "codex", "queued", 2, None),
    ("OPS-502", "Backup and restore drill", "infra", "platform", "service", "done", 4, None),
    ("OPS-503", "Rate limits on the public API", "api", "platform", "codex", "queued", 3, None),
    ("OPS-504", "Incident template and on-call rota", "infra", "platform", None, "planned", 8, None),
    # Unparented, to show the board handles work outside an initiative
    ("TASK-1", "Triage the inbox backlog", "ops", "product", "claude", "queued", 5, None),
    ("TASK-2", "Update the contributor guide", "docs", "product", "codex", "planned", 9, None),
    ("TASK-3", "Dependency audit", "infra", "platform", "service", "queued", 6, None),
)

NOTES: dict[str, str] = {'UI-101': 'Preferences panel was the only shared state; the split is clean.',
    'UI-102': 'Muted palette fails contrast on two surfaces; retuning before the audit.',
    'UI-103': 'Legacy table is used on nine screens; replacing it screen by screen.',
    'UI-104': 'Focus order is wrong wherever a panel is collapsed by default.',
    'UI-105': 'Every list needs an empty state; three currently render a bare frame.',
    'UI-106': 'Breakpoints assume a fixed sidebar that is now collapsible.',
    'UI-107': 'Blocked on UI-102 landing; contrast fixes change what needs auditing.',
    'UI-108': 'Delete once UI-102 ships and no screen imports the old tokens.',
    'ML-201': 'Three checkpoints score within noise; picking on latency, not accuracy.',
    'ML-202': 'Upstream export publishes partial months before the 5th.',
    'ML-203': 'Waiting on the deduplicated corpus; training on duplicates skews recall.',
    'ML-204': 'Target is the local runtime, so accuracy loss matters less than memory.',
    'ML-205': 'Scores reproduce within tolerance on the holdout split.',
    'ML-206': 'Catch prompt drift before it reaches a release, not after.',
    'ML-207': 'Cannot be written until the evaluation numbers settle.',
    'PLT-301': 'Draining the old queue; new work already lands on the new runner.',
    'PLT-302': 'Connection churn is the largest term in the tail latency.',
    'PLT-303': 'Half the services log free text; the other half log nothing useful.',
    'PLT-304': 'Remove once PLT-301 has run a week with no reconciliation drift.',
    'PLT-305': 'No documented path to rotate a leaked credential under load.',
    'PLT-306': 'Needs the new runner first; blue/green on the old one is not safe.',
    'SRCH-401': 'Reindexing shard 3 of 8; the rest follow once this one verifies.',
    'SRCH-402': 'Expansion helps recall and hurts precision; looking for the trade.',
    'SRCH-403': 'Latency budget cannot be set until connection pooling lands.',
    'SRCH-404': 'Without judgements the relevance work has no way to be wrong.',
    'OPS-501': 'A job that dies holding a claim currently looks the same as a slow one.',
    'OPS-502': 'Restored to a scratch instance and verified against a known row count.',
    'OPS-503': 'One client can currently exhaust the pool for everyone.',
    'OPS-504': 'On-call exists informally; nothing is written down.',
    'TASK-1': 'Inbox has notes from three agents nobody has read.',
    'TASK-2': 'Setup instructions are a version behind the actual toolchain.',
    'TASK-3': '412 packages, three advisories, none reachable from our code paths.'}

_PARENT = {"UI": "INIT-UI", "ML": "INIT-MODEL", "PLT": "INIT-PLATFORM",
           "SRCH": "INIT-SEARCH", "OPS": "INIT-OPS"}


# Dependencies the notes already state in prose. Recording them as data is what
# lets the graph draw an edge; a board that says "waiting on PLT-302" in text
# and stores nothing has the relationship only in a human's head.
DEPENDS_ON: dict[str, list[str]] = {
    "ML-203": ["ML-202"],
    "ML-204": ["ML-203"],
    "ML-205": ["ML-201"],
    "ML-206": ["ML-205"],
    "SRCH-402": ["SRCH-401"],
    "SRCH-403": ["PLT-302"],
    "SRCH-404": ["SRCH-402"],
    "UI-104": ["UI-103"],
    "UI-107": ["UI-102"],
    "UI-108": ["UI-102"],
    "PLT-304": ["PLT-301"],
    "PLT-306": ["PLT-302"],
    "OPS-501": ["PLT-301"],
    "OPS-503": ["PLT-302"],
    "TASK-3": ["OPS-501"],
}

# Rows whose effect puts them at the top review tier. An audit request is a
# T0-only act -- the coordination contract refuses one on work that
# self-verifies -- so a board that seeds no T0 row can never show a review
# being asked for. Both of these earn it honestly: an evaluation harness
# produces the numbers other work is judged against, and a judgement set is
# ground truth.
T0_ROWS: frozenset[str] = frozenset({"ML-205", "SRCH-404"})


def _work_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for work_id, title, module, domain, assignee, state, priority, note in _WORK:
        row: dict[str, Any] = {
            "work_id": work_id, "title": title, "display": title,
            "module": module, "domain": domain, "priority": priority,
            "state": state, "surface": "job",
            "parent_id": _PARENT.get(work_id.split("-")[0]),
        }
        if assignee:
            row["assignee"] = assignee
        row["note"] = NOTES.get(work_id, "")
        if work_id in DEPENDS_ON:
            row["depends_on"] = json.dumps(DEPENDS_ON[work_id])
        if state == "blocked":
            row["blocked_reason_class"] = "upstream_dependency"
            row["note"] = note
        elif state == "parked":
            row["next_step"] = note
            row["resume_when"] = "the dependency above has settled"
        elif note:
            row["step"] = note
        if state in {"running", "queued", "done"}:
            row["done_signal"] = f"docs/reports/{work_id.lower()}.md"
        if work_id in T0_ROWS:
            row["tier"] = "T0"
        rows.append(row)
    return rows


# Tracked jobs: long-running work that outlives the agent that started it, with
# progress reported to a sidecar rather than to whoever launched it.
# `pct` is a percentage in 0-100, not a fraction.
#
# Every job names the work row it is running for. The launcher requires that
# binding, and a sidecar without one is telemetry nothing on the board owns --
# a seeded fixture that omitted it made `coord doctor` block on the very board
# this module exists to hand a first reader.
SAMPLE_JOBS: tuple[dict[str, Any], ...] = (
    {"job_id": "embed-corpus-v4", "roadmap_id": "ML-201", "resource_class": "gpu", "driver": "claude", "state": "running", "pct": 62.0,
     "step": "encoding shard 5 of 8 on the local GPU", "rows_done": 620_000,
     "rate": 8_400.0, "eta_s": 1_260, "runtime_s": 5_400, "attempt": 1},
    {"job_id": "rerank-finetune-r3", "roadmap_id": "ML-203", "resource_class": "gpu", "driver": "codex", "state": "done", "pct": 100.0,
     "step": "epoch 2 of 6", "rows_done": 34_000, "rate": 210.0,
     "eta_s": 7_200, "runtime_s": 3_900, "attempt": 1},
    {"job_id": "search-reindex-shard3", "roadmap_id": "SRCH-401", "resource_class": "cpu", "driver": "codex", "state": "done", "pct": 100.0,
     "step": "merging segments", "rows_done": 1_230_000, "rate": 22_000.0,
     "eta_s": 900, "runtime_s": 1_800, "attempt": 1},
    {"job_id": "ui-visual-regression", "roadmap_id": "UI-103", "resource_class": "cpu", "driver": "claude", "state": "running", "pct": 78.0,
     "step": "comparing 312 of 400 screenshots", "rows_done": 312,
     "rate": 4.0, "eta_s": 22, "runtime_s": 78, "attempt": 1},
    {"job_id": "nightly-integration-suite", "roadmap_id": "OPS-501", "resource_class": "cpu", "driver": "codex", "state": "done", "pct": 100.0,
     "step": "1,284 tests, 0 failures", "rows_done": 1_284,
     "runtime_s": 512, "exit_code": 0, "attempt": 1},
    {"job_id": "dedupe-training-corpus", "roadmap_id": "ML-202", "resource_class": "cpu", "driver": "claude", "state": "failed", "pct": 31.0,
     "step": "aborted: upstream export incomplete", "rows_done": 310_000,
     "runtime_s": 244, "exit_code": 1, "attempt": 2, "backoff_s": 900},
    {"job_id": "dependency-audit", "roadmap_id": "TASK-3", "resource_class": "cpu", "driver": "codex", "state": "done", "pct": 100.0,
     "step": "412 packages, 3 advisories", "rows_done": 412,
     "runtime_s": 96, "exit_code": 0, "attempt": 1},
    {"job_id": "backup-restore-drill", "roadmap_id": "OPS-502", "resource_class": "cpu", "driver": "operator", "state": "done", "pct": 100.0,
     "step": "restored to a scratch instance and verified", "rows_done": 1,
     "runtime_s": 1_140, "exit_code": 0, "attempt": 1},
    {"job_id": "quantise-checkpoint-q4", "roadmap_id": "ML-204", "resource_class": "gpu", "driver": "claude", "state": "done", "pct": 100.0,
     "step": "calibrating on the holdout split", "rows_done": 1_200,
     "rate": 45.0, "eta_s": 5_400, "runtime_s": 600, "attempt": 1},
    {"job_id": "log-schema-backfill", "roadmap_id": "PLT-303", "resource_class": "cpu", "driver": "codex", "state": "done", "pct": 100.0,
     "step": "rewriting 2026-07 partitions", "rows_done": 8_800_000,
     "rate": 140_000.0, "eta_s": 420, "runtime_s": 9_600, "attempt": 1},
)


DAY = 24 * HOUR


def _ago(days: float = 0.0, hours: float = 0.0, minutes: float = 0.0) -> float:
    """Seconds before the seed instant. Written out so the offsets read as a
    calendar rather than as a pile of magic numbers."""
    return days * DAY + hours * HOUR + minutes * 60.0


# The recorded history: (age, kind, actor, work_id, to_lane, title, body).
#
# Every age is distinct, so the board has as many instants as it has events and
# nothing has to be tie-broken to be ordered. They span four fictional days
# ending a few minutes before the seed, which is what lets a view group by UTC
# date and find more than one group. `to_lane` is the destination of a
# coordination act and is written into `to_selector` as `actor:<lane>`; it is
# None for everything that is a note to the record rather than a message to a
# lane.
#
# The kinds are deliberately varied. A seeded board where every event is a
# `note` renders every kind-aware view as one colour, and a reader cannot tell
# a view that collapsed from a board that is uniform.
_EVENT_HISTORY: tuple[tuple[float, str, str, str, str | None, str, str], ...] = (
    (_ago(days=3, hours=7, minutes=12), "note", "claude", "UI-101", None,
     "settings screen split", "Preferences panel was the only shared state."),
    (_ago(days=3, hours=6, minutes=41), "decision", "claude", "UI-102", None,
     "dark mode tokens", "Retune the muted palette rather than exempt two surfaces."),
    (_ago(days=3, hours=5, minutes=58), "note", "codex", "PLT-301", None,
     "queue drain started", "New work already lands on the new runner."),
    (_ago(days=3, hours=4, minutes=33), "handoff", "claude", "PLT-303", "codex",
     "logging pass to backend", "Request path converted; the workers are yours."),
    (_ago(days=3, hours=3, minutes=9), "note", "codex", "PLT-303", None,
     "picked up logging", "Starting on the worker pool."),
    (_ago(days=3, hours=1, minutes=27), "heartbeat", "service", "OPS-502", None,
     "drill running", ""),
    (_ago(days=2, hours=22, minutes=4), "note", "codex", "SRCH-401", None,
     "shard 1 reindexed", "Verified against the previous segment counts."),
    (_ago(days=2, hours=20, minutes=51), "note", "claude", "ML-201", None,
     "checkpoint sweep", "Three checkpoints score within noise."),
    (_ago(days=2, hours=19, minutes=12), "blocked_reason_classified", "claude", "ML-202", None,
     "upstream export", "Partial months are published before the 5th."),
    (_ago(days=2, hours=17, minutes=45), "handoff", "codex", "ML-203", "claude",
     "reranker to chat lane", "Epoch 2 of 6; the corpus question is yours."),
    (_ago(days=2, hours=16, minutes=2), "note", "claude", "ML-203", None,
     "corpus duplicates", "Training on duplicates skews recall."),
    (_ago(days=2, hours=14, minutes=38), "heartbeat", "local", "ML-201", None,
     "gpu box alive", ""),
    (_ago(days=2, hours=12, minutes=15), "note", "codex", "UI-103", None,
     "four of nine screens", "Legacy table replaced screen by screen."),
    (_ago(days=2, hours=10, minutes=44), "decision", "codex", "PLT-302", None,
     "pooling before budgets", "Latency budgets cannot be set before pooling lands."),
    (_ago(days=2, hours=9, minutes=6), "blocked_reason_classified", "codex", "SRCH-403", None,
     "waiting on pooling", "Pooling changes the numbers this budget is set against."),
    (_ago(days=2, hours=7, minutes=29), "note", "service", "OPS-501", None,
     "stale telemetry", "A dead job holding a claim looks like a slow one."),
    (_ago(days=2, hours=5, minutes=52), "audit_request", "codex", "ML-205", "claude",
     "review the offline harness", "Scores reproduce on the holdout; second pair of eyes."),
    (_ago(days=2, hours=3, minutes=18), "note", "claude", "ML-205", None,
     "reading the harness", "Checking the split is really held out."),
    (_ago(days=2, hours=1, minutes=40), "audit_verdict", "claude", "ML-205", "codex",
     "harness verdict", "Holdout is clean; the tolerance is stated."),
    (_ago(days=1, hours=23, minutes=7), "note", "codex", "ML-205", None,
     "verdict acknowledged", "Landing the harness."),
    (_ago(days=1, hours=21, minutes=33), "heartbeat", "local", "ML-204", None,
     "quantisation queued", ""),
    (_ago(days=1, hours=19, minutes=55), "note", "claude", "UI-104", None,
     "focus order", "Wrong wherever a panel is collapsed by default."),
    (_ago(days=1, hours=18, minutes=11), "handoff", "claude", "UI-103", "codex",
     "table component", "Nine screens; you have the backend half."),
    (_ago(days=1, hours=16, minutes=26), "note", "codex", "UI-103", None,
     "table taken", "Starting with the settings and billing screens."),
    (_ago(days=1, hours=14, minutes=48), "work_resumed", "codex", "PLT-301", None,
     "drain resumed", "Reconciliation drift is inside tolerance."),
    (_ago(days=1, hours=13, minutes=2), "note", "service", "TASK-3", None,
     "audit run", "412 packages, three advisories."),
    (_ago(days=1, hours=11, minutes=24), "audit_request", "claude", "SRCH-404", "codex",
     "review the judgement set", "Ground truth; it needs a second reader before it is used."),
    (_ago(days=1, hours=9, minutes=47), "note", "codex", "SRCH-404", None,
     "reading judgements", "Sampling 40 of the pairs."),
    (_ago(days=1, hours=8, minutes=3), "audit_verdict", "codex", "SRCH-404", "claude",
     "judgement set verdict", "Two pairs disagree with the guideline; the rest hold."),
    (_ago(days=1, hours=6, minutes=29), "tier_corrected", "codex", "SRCH-404", None,
     "tier stated", "Ground truth is T0; recorded rather than assumed."),
    (_ago(days=1, hours=4, minutes=55), "note", "claude", "SRCH-402", None,
     "expansion trade", "Helps recall, hurts precision."),
    (_ago(days=1, hours=3, minutes=12), "heartbeat", "service", "OPS-503", None,
     "rate limit sweep", ""),
    (_ago(days=1, hours=1, minutes=38), "note", "codex", "PLT-306", None,
     "blue/green blocked", "Not safe on the old runner."),
    (_ago(hours=23, minutes=4), "continuation_ready", "claude", "UI-108", None,
     "switcher removal ready", "UI-102 has shipped; nothing imports the old tokens."),
    (_ago(hours=21, minutes=21), "note", "claude", "PLT-304", None,
     "shim still needed", "One week of clean reconciliation, not yet elapsed."),
    (_ago(hours=19, minutes=44), "handoff", "codex", "OPS-501", "service",
     "alerting to the scheduler", "Thresholds are written down; the rota is not."),
    (_ago(hours=17, minutes=58), "note", "service", "OPS-501", None,
     "alert wired", "Firing on the staging telemetry."),
    (_ago(hours=16, minutes=13), "claim_conflict", "claude", "UI-103", None,
     "two lanes on one screen", "Both lanes touched the billing screen; codex holds it."),
    (_ago(hours=14, minutes=36), "note", "codex", "UI-103", None,
     "conflict settled", "Billing screen stays with the backend lane."),
    (_ago(hours=12, minutes=51), "heartbeat", "local", "ML-203", None,
     "epoch 2", ""),
    (_ago(hours=11, minutes=7), "note", "claude", "ML-206", None,
     "prompt drift", "Catch it before a release, not after."),
    (_ago(hours=9, minutes=22), "session_closeout", "claude", "UI-102", None,
     "frontend session closed", "Contrast fixes handed to the next session."),
    (_ago(hours=7, minutes=45), "note", "codex", "OPS-503", None,
     "pool exhaustion", "One client can currently take the whole pool."),
    (_ago(hours=6, minutes=2), "handoff", "service", "TASK-3", "codex",
     "audit results to backend", "Three advisories; none reachable from our paths."),
    (_ago(hours=4, minutes=28), "note", "codex", "TASK-3", None,
     "advisories triaged", "Filed, not fixed; nothing is reachable."),
    (_ago(hours=2, minutes=53), "reopen", "claude", "UI-105", None,
     "empty states reopened", "Three lists still render a bare frame."),
    (_ago(hours=1, minutes=17), "note", "claude", "TASK-1", None,
     "inbox backlog", "Notes from three agents nobody has read."),
    (_ago(minutes=38), "heartbeat", "service", "OPS-502", None,
     "scheduler alive", ""),
    (_ago(minutes=11), "note", "codex", "SRCH-401", None,
     "shard 3 of 8", "The rest follow once this one verifies."),
)


class _Clock:
    """A movable fictional clock.

    The seeder used to pin `db_now` to one constant, which made every write
    land on the same instant. A board whose entire recorded history happened in
    one microsecond is not a board any reader will ever see: nothing can be
    ordered, no view can group by day, and a recency list is indistinguishable
    from insertion order. So the clock is now settable, and the event history
    below walks it backwards through several fictional days before the claims
    are taken.
    """

    __slots__ = ("at",)

    def __init__(self, at: float) -> None:
        self.at = float(at)


@contextmanager
def _synthetic_clock(epoch: float):
    """Keep every lifecycle write on the same fictional, reproducible clock."""
    clock = _Clock(epoch)
    original = coord_db.db_now
    coord_db.db_now = lambda _conn: clock.at
    try:
        yield clock
    finally:
        coord_db.db_now = original


def _connect(db_path: Path) -> sqlite3.Connection:
    bootstrap_database(db_path)
    return coord_config.connect(db_path)


def _open_sessions(conn: sqlite3.Connection) -> None:
    for agent in AGENTS:
        coord_db.register_session(
            conn,
            agent["session_id"],
            agent["actor"],
            runner_type=agent["runner_type"],
            human_label=agent["human_label"],
            label_source="operator",
        )


def _seed_work(conn: sqlite3.Connection) -> None:
    for epic in EPICS:
        coord_db.upsert_work(
            conn, epic["work_id"], surface="epic", title=epic["title"],
            display=epic["display"], module=epic["module"], domain=epic["domain"],
        )
    for row in _work_rows():
        fields = {k: v for k, v in row.items() if k not in {"work_id", "state", "step"} and v is not None}
        coord_db.upsert_work(conn, row["work_id"], **fields)


def _live_activity(conn: sqlite3.Connection, clock: _Clock) -> list[str]:
    """Drive real lifecycle verbs so the board shows a fleet mid-flight.

    A freshly seeded board where every row is `planned` is accurate and tells a
    reader nothing. Claims are taken by the agent each row is assigned to, which
    is also what the cross-actor guard requires.

    Returns whatever refused, rather than swallowing it. A guard declining a
    write is legitimate demo state and must not abort the seed -- but a silent
    `except: continue` also hides a claim that failed for a reason nobody
    intended, which is how this seeder came to produce a board where no agent
    work was running at all.
    """
    claim_by_actor = {
        "claude": ["claude:frontend", "claude:platform", "claude:cloud-a"],
        "codex": ["codex:backend", "codex:review", "codex:cloud-b"],
        "service": ["service:scheduler"],
    }
    cursor = {actor: 0 for actor in claim_by_actor}
    refusals: list[str] = []

    for row in _work_rows():
        if row["state"] not in {"running", "blocked"}:
            continue
        actor = row.get("assignee")
        sessions = claim_by_actor.get(actor or "")
        if not sessions:
            refusals.append(f"{row['work_id']}: no session for actor {actor!r}")
            continue
        session = sessions[cursor[actor] % len(sessions)]
        cursor[actor] += 1
        try:
            claim = coord_db.claim_work(conn, session, row["work_id"], step=row.get("step") or "")
        except Exception as exc:  # noqa: BLE001
            refusals.append(f"{row['work_id']}: claim refused: {type(exc).__name__}: {exc}")
            continue
        if row["state"] == "blocked":
            reason = row.get("note") or "upstream dependency"
            try:
                # Blocking is a release with a status and a criterion, not its
                # own verb: the contract requires naming what would unblock it.
                coord_db.release_claim(
                    conn, claim, status="blocked", reason=reason,
                    resume_when=reason, resume_manual=True,
                )
            except Exception as exc:  # noqa: BLE001
                refusals.append(f"{row['work_id']}: block refused: {type(exc).__name__}: {exc}")

    # The recorded history, walked backwards from the seed instant. The clock is
    # moved for each write, so the board comes out with as many instants as it
    # has events instead of one instant with everything piled on it. Oldest
    # first, so the table reads forwards even though the offsets are ages.
    seed_at = clock.at
    try:
        for age, kind, actor, work_id, to_lane, title, body in sorted(
            _EVENT_HISTORY, key=lambda entry: -entry[0]
        ):
            clock.at = seed_at - age
            try:
                coord_db.post_event(
                    conn,
                    kind=kind,
                    actor=actor,
                    work_id=work_id,
                    to_selector=f"actor:{to_lane}" if to_lane else None,
                    title=title,
                    body=body or None,
                    idempotency_key=f"demo-history:{work_id}:{kind}:{int(age)}",
                )
            except Exception as exc:  # noqa: BLE001
                refusals.append(
                    f"{work_id}: {kind} refused: {type(exc).__name__}: {exc}"
                )
    finally:
        # Claims and leases were taken on the seed instant and must stay there;
        # leaving the clock in the past would make every lease look expired.
        clock.at = seed_at
    return refusals


def _write_job_sidecars(*, now: float | None = None) -> int:
    """Write one progress sidecar per sample job.

    A tracked job reports progress to a file rather than to whoever started it,
    so the board can tell the difference between a job that is working and one
    whose process died holding a claim. These are invented, but they are the
    real shape the reader will produce.
    """
    directory = config.job_progress_dir()
    directory.mkdir(parents=True, exist_ok=True)
    now = time.time() if now is None else float(now)
    for job in SAMPLE_JOBS:
        payload = dict(job)
        payload.setdefault("done", job.get("state") == "done")
        # The launcher writes both stamps; a fixture that writes one is not the
        # shape this docstring claims, and the reader flagged every live job.
        stamped = now - float(job.get("runtime_s", 0)) / 8
        payload["last_progress_at"] = stamped
        payload["updated_at"] = now if job.get("state") == "running" else stamped
        (directory / f"{job['job_id']}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
    return len(SAMPLE_JOBS)


def seed(db_path: Path, *, quiet: bool = False) -> dict[str, int]:
    """Create the demo board. Returns a count of what was written."""
    conn = _connect(db_path)
    try:
        # Lifecycle writes land on the real clock unless a reproducible-build
        # run pins one. A fixed epoch makes every lease expire the moment the
        # seed is a few hours old, and a permanently-expired lease derives to
        # attention -- so the board showed no running agent work at all.
        epoch = config.source_date_epoch(None)
        if epoch is None:
            epoch = time.time()
        with _synthetic_clock(epoch) as clock:
            _open_sessions(conn)
            _seed_work(conn)
            refusals = _live_activity(conn, clock)
        # A reproducible seed must use one clock for lifecycle rows, event
        # history, and job telemetry.  Mixing a fixed lifecycle clock with the
        # host clock produces future-dated jobs and an impossible read model.
        jobs_written = _write_job_sidecars(now=epoch)
        # The native clients read a materialised projection rather than querying
        # the board directly, so a seeded board is invisible to them until it is
        # built once.
        native_cockpit.refresh(conn, source_version="coordharness.demo")
        # Leave the database readable by a client that cannot write beside it.
        # A WAL database needs a -shm file even for a read-only open, so a
        # sandboxed reader gets SQLITE_CANTOPEN unless the journal is folded in.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        counts = {
            "sessions": conn.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0],
            "work_items": conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0],
            "claims": conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "job sidecars": jobs_written,
        }
    finally:
        conn.close()
    if not quiet:
        for key, value in counts.items():
            print(f"  {key:12s} {value}")
        # Printed, not hidden. A guard declining is legitimate; a claim failing
        # for an unintended reason looks identical from the outside unless the
        # seed says so.
        for refusal in refusals:
            print(f"  refused      {refusal}")
    return counts


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    db_path = args.db or config.coord_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"seeding demo board at {db_path}")
    seed(db_path, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
