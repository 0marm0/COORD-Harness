"""PANEL tier: N independent assessments that must agree.

The one genuine gap in COORD's tiered review contract. ``docs/review-tiers.md``
describes T0 as a row where "an independent reviewer must record a verdict on
the row before it can close" -- singular, and the implementation is exactly
that: ``review_integrity.classify_verdict_status`` returns ``reviewed: True`` on
the FIRST verdict from an independent lane after the review barrier
(``src/coordharness/coord/review_integrity.py:177-184``), so no row can ask for
a second opinion. Measured absence of any existing quorum notion at c0c043b::

    $ grep -rniE 'quorum|unanimous|n_of' --include='*.py' src/
    # only unrelated identifiers: function_of_supplied_values_only,
    # remediation_of_event_id

Design notes, and the reasons, in one place:

* **Pure core.** ``classify_panel_status`` takes plain dicts so it is testable
  and reviewable without standing up coord.db, mirroring the split between the
  pure ``review_tier.py`` and the gating ``coord_db.py``.
* **Unanimity, not majority.** One FLAG or BLOCKED fails the panel. A panel that
  discards its minority has thrown away the signal it was convened to find.
* **Independence is (actor, session_id), not lane.** COORD has two lanes:
  ``review_integrity._INDEPENDENT_LANES = frozenset({"claude", "codex"})``. A
  two-lane harness cannot produce three lane-independent assessments. The result
  therefore reports ``lane_independent_count`` as a SEPARATE number from
  ``assessor_count`` so a caller can never read "3 assessors" as "3 lanes".
* **Declared, never inferred.** There is no pattern table that promotes a row to
  PANEL. A pattern-driven PANEL would be an uncapped tier-inflation engine.
* **No new event kind, no migration, no new tier value.** PANEL is a stricter
  reading of T0's existing ``audit_verdict`` evidence, and the declaration rides
  in the work row's existing ``acceptance_json`` (``schema.sql`` line 60), so
  nothing is added to the schema. Adding a fourth value to
  ``review_tier.VALID_REVIEW_TIERS`` would ripple through every site that
  branches on tier; a quorum predicate ripples through one.

WIRED 2026-09-01. ``coord_db.completion_review_state`` reads a declared quorum
from the row's ``acceptance_json`` and holds ``needs_review`` true until the
panel passes. It is opt-in by construction -- a panel is declared, never
inferred -- so a row that declares no quorum is unaffected. The adapter is
called fail-closed: ``completion_review_state`` is a read model the board calls,
so a panel that cannot be evaluated is reported ``panel_uncomputable`` and still
gates, rather than raising into a caller that only wanted to draw a row.

This paragraph is load-bearing. It carried the unwired marker for as long as
that was true, which is how the gap was found at all; leaving the marker in
place after the wiring would hand the next reader a false map of what this
harness enforces. The marker phrase itself is deliberately not repeated here --
``tools/dark_capability_check.py`` reads it, and a module that discusses the
phrase reports itself.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping


PASS_VERDICT = "PASS"
REVIEW_VERDICTS = frozenset({"PASS", "FLAG", "BLOCKED"})
INDEPENDENT_LANES = frozenset({"claude", "codex"})
MIN_PANEL_QUORUM = 2
MAX_PANEL_QUORUM = 8

REASON_NOT_A_PANEL = "not_a_panel"
REASON_QUORUM_UNMET = "panel_quorum_unmet"
REASON_DISSENT = "panel_dissent"
REASON_NO_OPPOSITE_LANE = "panel_lacks_opposite_lane"
REASON_SATISFIED = "panel_unanimous"


class PanelContractError(ValueError):
    """Raised when a declared panel quorum is not a usable integer."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def normalize_quorum(value: Any) -> int | None:
    """Return a validated quorum, or None when the row declares no panel.

    Refuses quorum < 2 loudly rather than silently degrading to a plain T0
    audit: a row that says ``panel_quorum: 1`` is either a typo or a
    misunderstanding, and both deserve an error.
    """
    if value is None or _text(value) == "":
        return None
    if isinstance(value, bool):
        raise PanelContractError("panel_quorum must be an integer, got a boolean")
    if isinstance(value, float) and not value.is_integer():
        raise PanelContractError(f"panel_quorum must be an integer, got {value!r}")
    try:
        quorum = int(value)
    except (TypeError, ValueError) as exc:
        raise PanelContractError(f"panel_quorum must be an integer, got {value!r}") from exc
    if quorum < MIN_PANEL_QUORUM:
        raise PanelContractError(
            f"panel_quorum must be >= {MIN_PANEL_QUORUM}; a quorum of {quorum} is an "
            "ordinary T0 audit, declare it as one"
        )
    if quorum > MAX_PANEL_QUORUM:
        raise PanelContractError(
            f"panel_quorum must be <= {MAX_PANEL_QUORUM}; larger panels are not "
            "reachable in a two-lane harness"
        )
    return quorum


def quorum_from_acceptance(acceptance: Any) -> int | None:
    """Read ``panel_quorum`` out of an acceptance contract.

    Accepts a mapping, or a JSON string holding one. Anything else declares no
    panel. Rides in acceptance_json so PANEL needs no schema migration.
    """
    if acceptance is None:
        return None
    if isinstance(acceptance, str):
        raw = acceptance.strip()
        if not raw:
            return None
        import json

        try:
            acceptance = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(acceptance, Mapping):
        return None
    return normalize_quorum(acceptance.get("panel_quorum"))


def _assessor_key(event: Mapping[str, Any]) -> tuple[str, str]:
    return (
        _text(event.get("actor")).casefold(),
        _text(event.get("session_id")).casefold(),
    )


def _qualifying_verdicts(
    events: Iterable[Mapping[str, Any]],
    *,
    author_lane: str,
    author_session_id: str,
    barrier_event_id: int,
) -> list[dict[str, Any]]:
    """Verdicts that count toward a panel, newest-per-assessor.

    Drops, in order: non-verdict events; verdicts at or before the review
    barrier (same rule as review_integrity.classify_verdict_status, so a panel
    cannot be assembled from stale verdicts about a superseded acceptance
    contract); and self-verdicts from the authoring session. Where one assessor
    posted twice, the highest event_id wins.
    """
    lane = _text(author_lane).casefold()
    author_session = _text(author_session_id).casefold()
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        if _text(event.get("kind")).casefold() != "audit_verdict":
            continue
        verdict = _text(event.get("verdict")).upper()
        if verdict not in REVIEW_VERDICTS:
            continue
        try:
            event_id = int(event.get("event_id") or 0)
        except (TypeError, ValueError):
            continue
        if event_id <= int(barrier_event_id or 0):
            continue
        actor, session_id = _assessor_key(event)
        if actor == lane and session_id == author_session:
            continue
        record = {
            "event_id": event_id,
            "actor": actor,
            "session_id": session_id,
            "verdict": verdict,
        }
        key = (actor, session_id)
        prior = latest.get(key)
        if prior is None or event_id > prior["event_id"]:
            latest[key] = record
    return sorted(latest.values(), key=lambda item: item["event_id"])


def classify_panel_status(
    events: Iterable[Mapping[str, Any]],
    *,
    author_lane: str,
    quorum: int,
    barrier_event_id: int,
    author_session_id: str,
) -> dict[str, Any]:
    """Decide whether a panel has been satisfied.

    Returns a dict with ``passed`` plus every number a caller might otherwise be
    tempted to recompute. ``lane_independent_count`` is deliberately separate
    from ``assessor_count``: in a two-lane harness the former can never exceed
    2, and reporting only the latter would let ``panel_quorum: 3`` imply an
    independence guarantee the harness cannot make.
    """
    checked_quorum = normalize_quorum(quorum)
    if checked_quorum is None:
        return {
            "passed": False,
            "reason": REASON_NOT_A_PANEL,
            "quorum": None,
            "assessor_count": 0,
            "lane_independent_count": 0,
            "lane_independent_actors": [],
            "assessors": [],
            "dissenters": [],
        }

    lane = _text(author_lane).casefold()
    author_session = _text(author_session_id).casefold()
    if lane not in {"claude", "codex"}:
        raise PanelContractError("panel author_lane must be claude or codex")
    if not author_session or author_session.partition(":")[0] != lane:
        raise PanelContractError(
            "panel author_session_id must be nonempty and match author_lane"
        )
    if isinstance(barrier_event_id, bool) or not isinstance(barrier_event_id, int):
        raise PanelContractError("panel barrier_event_id must be a non-negative integer")
    if barrier_event_id < 0:
        raise PanelContractError("panel barrier_event_id must be a non-negative integer")
    qualifying = _qualifying_verdicts(
        events,
        author_lane=lane,
        author_session_id=author_session,
        barrier_event_id=barrier_event_id,
    )
    assessors = [
        {"actor": item["actor"], "session_id": item["session_id"], "verdict": item["verdict"]}
        for item in qualifying
    ]
    dissenters = [item for item in assessors if item["verdict"] != PASS_VERDICT]
    lane_independent = sorted(
        {
            item["actor"]
            for item in qualifying
            if item["actor"] in INDEPENDENT_LANES and item["actor"] != lane
        }
    )
    base = {
        "quorum": checked_quorum,
        "assessor_count": len(assessors),
        "lane_independent_count": len(lane_independent),
        "lane_independent_actors": lane_independent,
        "assessors": assessors,
        "dissenters": dissenters,
    }

    # Dissent is reported before an unmet quorum: one BLOCKED is a finding, and
    # burying it under "not enough reviewers yet" would let a row keep
    # collecting PASSes in the hope of outvoting it. Unanimity means the panel
    # is already failed the moment anyone objects.
    if dissenters:
        return {**base, "passed": False, "reason": REASON_DISSENT}
    if len(assessors) < checked_quorum:
        return {**base, "passed": False, "reason": REASON_QUORUM_UNMET}
    if not lane_independent:
        return {**base, "passed": False, "reason": REASON_NO_OPPOSITE_LANE}
    return {**base, "passed": True, "reason": REASON_SATISFIED}


def classify_panel_status_for_work(
    conn: Any,
    work_id: str,
    *,
    author_lane: str,
    quorum: int,
    barrier_event_id: int,
    author_session_id: str,
) -> dict[str, Any]:
    """Thin coord.db adapter over the pure core.

    Reads the same events rows review_integrity._own_row_audit_verdict_events
    reads, plus the session_id column that function does not need -- panel
    independence is per (actor, session_id), not per actor. Requires a
    connection with ``row_factory = sqlite3.Row``, which is what
    ``coord.config.connect`` returns. Kept deliberately dumb: all judgement
    lives in classify_panel_status.
    """
    rows = conn.execute(
        "SELECT event_id, kind, actor, session_id, verdict FROM events"
        " WHERE work_id=? AND kind='audit_verdict' ORDER BY event_id ASC",
        (work_id,),
    ).fetchall()
    events = [
        {
            "event_id": row["event_id"],
            "kind": row["kind"],
            "actor": row["actor"],
            "session_id": row["session_id"],
            "verdict": row["verdict"],
        }
        for row in rows
    ]
    result = classify_panel_status(
        events,
        author_lane=author_lane,
        quorum=quorum,
        barrier_event_id=barrier_event_id,
        author_session_id=author_session_id,
    )
    result["work_id"] = work_id
    return result


__all__ = [
    "PanelContractError",
    "classify_panel_status",
    "classify_panel_status_for_work",
    "normalize_quorum",
    "quorum_from_acceptance",
]
