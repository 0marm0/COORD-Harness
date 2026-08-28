from __future__ import annotations

import json

import pytest

from coordharness.coord import board_context
from coordharness.coord.creation_lint import expected_owner_prefix
from coordharness.coord.row_classification import derive_semantic_system
from coordharness.knowledge import kfts, memory_proposals


def test_public_operator_prefix_is_role_based() -> None:
    assert expected_owner_prefix("operator") == "OP"
    assert expected_owner_prefix("human") == "OP"
    assert expected_owner_prefix("user") == "OP"
    assert expected_owner_prefix("initials") is None


def test_product_classification_is_explicit_or_configured(monkeypatch) -> None:
    assert derive_semantic_system({"module": "customer-workflow"}) == "shared"
    assert derive_semantic_system({"semantic_system": "product"}) == "product"
    monkeypatch.setenv("COORD_PRODUCT_MODULES", "customer-workflow,other-module")
    assert derive_semantic_system({"module": "customer-workflow"}) == "product"
    with pytest.raises(ValueError, match="unsupported semantic_system"):
        derive_semantic_system({"semantic_system": "secret-product"})


def test_memory_intent_uses_roles_and_pronouns() -> None:
    assert kfts._memory_intent("remember my preference")
    assert kfts._memory_intent("remember the operator preference")
    assert not kfts._memory_intent("ordinary retrieval query")


def test_every_actor_is_barred_from_self_review(tmp_path) -> None:
    db = tmp_path / "memory.sqlite"
    proposal = memory_proposals.propose_memory(
        kind="fact",
        statement="The synthetic build uses one local authority.",
        evidence_pointer="docs://synthetic-build",
        source_actor="operator",
        db_path=db,
    )
    with pytest.raises(ValueError, match="may not review its own proposal"):
        memory_proposals.review_proposal(
            proposal.id,
            status="accepted",
            reviewer="operator",
            db_path=db,
        )
    accepted = memory_proposals.review_proposal(
        proposal.id,
        status="accepted",
        reviewer="reviewer-b",
        db_path=db,
    )
    assert accepted.reviewed_by == "reviewer-b"


def test_context_recipes_use_the_installed_module_and_public_docs() -> None:
    payloads = [
        board_context.build_digest([]),
        board_context.search_rows([], "synthetic"),
        board_context.build_skeleton([]),
    ]
    rendered = json.dumps(payloads, sort_keys=True)
    assert "python -m coordharness.coord.board_context" in rendered
    assert "coordharness/scripts/board_context.py" not in rendered
    assert "coordharness/scripts/codex_coord.py" not in rendered
    assert board_context._POLICY_EPOCH_DOC_PATHS == (
        "docs/agent-protocol.md",
        "docs/review-tiers.md",
    )
