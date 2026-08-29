"""Three kinds of `done` were being served as two.

`derive_status` answers a `done` claim in three materially different ways and
returned only two labels for them:

  * the declared artifact resolved            -> done, unverified=False
  * an artifact was declared and is missing   -> done, unverified=True
  * no artifact was ever declared             -> done, unverified=False

The first and the third came back identical, so a caller counting verified
work counted a job that produced no proof at all beside one whose proof it had
just read off disk. Strictly less evidence, and the same confidence.

The fix deliberately does NOT flip the third case to `unverified`. `coord
create` requires `--done-signal`, so a job with no signal is an orphan or
unlinked sidecar rather than a job dodging a gate, and refusing it would
invent a requirement its author was never given -- the behaviour
`test_board_done_is_verified.py::test_a_job_that_declares_no_proof_anywhere_is_left_alone`
pins on purpose. What changes is that the three cases are now distinguishable:
`declared_proof` records whether a proof was ever asked for, so "verified" and
"never required" stop being one label. The board's served status is unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordharness.jobs.status import derive_status


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coordharness"))
    monkeypatch.delenv("COORD_JOB_PROCESS_PATTERNS_JSON", raising=False)
    monkeypatch.delenv("COORD_JOB_PROCESS_PATTERNS_FILE", raising=False)
    return tmp_path


def _artifact(root: Path, relative: str = "artifacts/embeddings.json") -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"vectors": list(range(64))}), encoding="utf-8")
    return relative


def test_a_done_claim_with_no_declared_proof_is_not_labelled_verified(root: Path) -> None:
    evidence = derive_status({"id": "orphan", "status": "done"}, root)

    assert evidence.status == "done"
    assert evidence.declared_proof is False, (
        "a claim that declared no proof was indistinguishable from a proved one"
    )
    assert evidence.unverified is False, (
        "the refusal must not widen: no proof was ever required of this job"
    )


def test_a_done_claim_whose_artifact_resolved_is_labelled_proved(root: Path) -> None:
    signal = _artifact(root)

    evidence = derive_status({"id": "proved", "status": "done", "done_signal": signal}, root)

    assert evidence.status == "done"
    assert evidence.declared_proof is True
    assert evidence.done_signal_exists is True
    assert evidence.unverified is False


def test_a_done_claim_whose_artifact_is_missing_stays_unverified(root: Path) -> None:
    evidence = derive_status(
        {"id": "claimed", "status": "done", "done_signal": "artifacts/never-made.json"}, root
    )

    assert evidence.status == "done"
    assert evidence.declared_proof is True
    assert evidence.done_signal_exists is False
    assert evidence.unverified is True


def test_the_three_done_cases_are_three_distinct_labels(root: Path) -> None:
    """The defect in one assertion: two of the three collided."""
    signal = _artifact(root)
    labels = {
        name: (evidence.unverified, evidence.declared_proof)
        for name, evidence in (
            ("proved", derive_status({"id": "a", "status": "done", "done_signal": signal}, root)),
            (
                "declared but missing",
                derive_status({"id": "b", "status": "done", "done_signal": "artifacts/no.json"}, root),
            ),
            ("never declared", derive_status({"id": "c", "status": "done"}, root)),
        )
    }

    assert len(set(labels.values())) == 3, labels


def test_declared_proof_is_recorded_for_states_that_are_not_done(root: Path) -> None:
    """It is a fact about the item, not about the branch that answered it.

    `board.snapshot` reads it on claims that leave through the rubric branch,
    which never reaches the done words at all.
    """
    blocked = derive_status(
        {"id": "d", "status": "done", "rubric_verdict": "blocked", "done_signal": "artifacts/no.json"},
        root,
    )

    assert blocked.status == "blocked"
    assert blocked.declared_proof is True

    queued = derive_status({"id": "e", "status": "queued"}, root)

    assert queued.status == "queued"
    assert queued.declared_proof is False
