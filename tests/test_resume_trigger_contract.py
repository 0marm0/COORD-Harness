"""The resume-trigger contract was enforced everywhere and measured nowhere.

`normalize_resume_trigger_contract` is the single normaliser every park and
block path funnels through -- the CLI, the MCP server and `release_claim` all
call it before a resume predicate is written to the row. It is what stops a
parked row from carrying a resume condition nothing can evaluate: a bare
`resume_when` sentence with no machine predicate behind it, a predicate of a
type the continuation evaluator does not implement, or a predicate declared
`manual` and conditional at the same time.

Ablating any one of those refusals left the whole suite green, which meant the
contract was an assertion about the code rather than a property of it. These
are the legs: each refusal is exercised by the input that must trip it, and the
two accepting paths are pinned to their canonical serialisation so a permissive
rewrite cannot pass by returning something the row can still not evaluate.
"""

from __future__ import annotations

import json

import pytest

from coordharness.coord.continuation_contract import (
    MANUAL_RESUME_PREDICATE,
    RESUME_TRIGGER_CONTRACT_INVALID,
    ResumeTriggerContractError,
    normalize_resume_trigger_contract,
)

_PREDICATE = {
    "type": "artifact_exists",
    "path": "artifacts/continuation-probe.json",
}


def test_no_trigger_at_all_is_the_only_way_to_get_none() -> None:
    assert (
        normalize_resume_trigger_contract(resume_when=None, resume_predicate=None)
        is None
    )
    assert (
        normalize_resume_trigger_contract(resume_when="   ", resume_predicate="  ")
        is None
    )


def test_a_prose_resume_when_alone_is_refused() -> None:
    """The defect this closes: a row parked on a sentence nobody can evaluate."""
    with pytest.raises(ResumeTriggerContractError) as excinfo:
        normalize_resume_trigger_contract(
            resume_when="when the upstream index finishes",
            resume_predicate=None,
            resume_manual=False,
        )
    assert excinfo.value.code == RESUME_TRIGGER_CONTRACT_INVALID
    # The exact refusal matters: with this branch removed the empty predicate
    # falls through to `json.loads("")` and comes back as a *parse* error, whose
    # message also contains the string "resume-predicate". A looser assertion
    # here passes against an ablated guard.
    assert (
        str(excinfo.value).endswith(
            "resume_when requires --resume-predicate or explicit --resume-manual"
        )
    ), str(excinfo.value)


def test_a_predicate_with_no_resume_when_is_refused() -> None:
    with pytest.raises(ResumeTriggerContractError) as excinfo:
        normalize_resume_trigger_contract(
            resume_when="",
            resume_predicate=json.dumps(_PREDICATE),
        )
    assert "resume_when is required" in str(excinfo.value)


def test_manual_and_predicate_are_mutually_exclusive() -> None:
    with pytest.raises(ResumeTriggerContractError) as excinfo:
        normalize_resume_trigger_contract(
            resume_when="when the operator says so",
            resume_predicate=json.dumps(_PREDICATE),
            resume_manual=True,
        )
    assert "mutually exclusive" in str(excinfo.value)


def test_resume_manual_must_be_a_boolean_not_a_truthy_string() -> None:
    with pytest.raises(ResumeTriggerContractError) as excinfo:
        normalize_resume_trigger_contract(
            resume_when="when the operator says so",
            resume_predicate=None,
            resume_manual="yes",  # type: ignore[arg-type]
        )
    assert "resume_manual must be a boolean" in str(excinfo.value)


def test_malformed_and_unsupported_predicates_are_refused() -> None:
    with pytest.raises(ResumeTriggerContractError) as excinfo:
        normalize_resume_trigger_contract(
            resume_when="when the artifact lands",
            resume_predicate="{not json",
        )
    assert "must be valid JSON" in str(excinfo.value)

    with pytest.raises(ResumeTriggerContractError) as excinfo:
        normalize_resume_trigger_contract(
            resume_when="when the artifact lands",
            resume_predicate=json.dumps({"type": "vibes", "path": "x"}),
        )
    assert "unsupported resume predicate type" in str(excinfo.value)

    # A supported type with its required field missing is refused too, so the
    # type check alone cannot carry the whole contract.
    with pytest.raises(ResumeTriggerContractError):
        normalize_resume_trigger_contract(
            resume_when="when the artifact lands",
            resume_predicate=json.dumps({"type": "artifact_exists"}),
        )


def test_a_composite_predicate_validates_every_child() -> None:
    with pytest.raises(ResumeTriggerContractError):
        normalize_resume_trigger_contract(
            resume_when="when both land",
            resume_predicate=json.dumps(
                {"type": "all_of", "predicates": [_PREDICATE, {"type": "vibes"}]}
            ),
        )


def test_accepted_triggers_are_canonicalised() -> None:
    manual = normalize_resume_trigger_contract(
        resume_when="when the operator says so",
        resume_predicate=None,
        resume_manual=True,
    )
    assert json.loads(str(manual)) == MANUAL_RESUME_PREDICATE

    unordered = {"path": _PREDICATE["path"], "type": _PREDICATE["type"]}
    canonical = normalize_resume_trigger_contract(
        resume_when="when the artifact lands",
        resume_predicate=json.dumps(unordered),
    )
    # Sorted keys and no whitespace: two callers declaring the same predicate
    # must produce byte-identical rows, or replay comparison is meaningless.
    assert canonical == json.dumps(
        _PREDICATE, sort_keys=True, separators=(",", ":")
    )
    assert canonical == normalize_resume_trigger_contract(
        resume_when="when the artifact lands",
        resume_predicate=json.dumps(_PREDICATE),
    )
