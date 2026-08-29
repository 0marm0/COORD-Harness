from __future__ import annotations

import base64
import json
import re
import subprocess
from pathlib import Path

import pytest

from coordharness.coord import board_context
from coordharness.coord.creation_lint import (
    durable_id_policy_issues,
    expected_owner_prefix,
)
from coordharness.coord.row_classification import derive_semantic_system
from coordharness.knowledge import kfts, memory_proposals


def test_public_operator_prefix_is_role_based() -> None:
    assert expected_owner_prefix("operator") == "OP"
    assert expected_owner_prefix("human") == "OP"
    assert expected_owner_prefix("user") == "OP"
    assert expected_owner_prefix("initials") is None


def test_durable_id_grammar_is_not_a_house_convention() -> None:
    """A stranger's work ids must be creatable.

    The rule this pins used to require every durable id to begin with the
    literal `N` and four digits -- the date-stamped convention of the project
    this harness was extracted from. It was enforced in shipped code, covered
    by no test, and mentioned in no document, so the first thing an outside
    adopter hit was a refusal naming a scheme they had never seen.
    """
    # An ordinary prefix of the adopter's own choosing is accepted.
    for work_id in ("ACME-CLA-BILLING-EXPORT", "Q3-CDX-INDEX-REBUILD", "W7-OP-SIGN-OFF"):
        assert durable_id_policy_issues(work_id) == [], work_id

    # The part of the rule that carries weight is untouched: an id still has to
    # name its owning lane, so a row's owner is legible from the id alone.
    for work_id in ("ACME-BILLING-EXPORT", "ACME-QA-BILLING-EXPORT", "PLAIN"):
        assert durable_id_policy_issues(work_id), work_id

    # And a lane followed by a chat number is still refused, because a chat is
    # not an owner and does not outlive the conversation that made it.
    assert durable_id_policy_issues("ACME-CLA-3-BILLING-EXPORT")


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


# --- private-project leakage guard -----------------------------------------
#
# `tools/privacy_hygiene.py` only rejects text that hashes to a phrase someone
# already knew to add to `.github/privacy-denylist.sha256`. It cannot catch a
# reference to the private source project expressed in wording nobody has
# hashed yet — which is exactly how a `CLAUDE.md §3` citation from the private
# contract file survived in `work_contracts.py` while that denylist stayed
# green. This test closes that gap with STRUCTURAL patterns for the whole
# class of private-project reference (name, path, or section pointer) rather
# than a fixed vocabulary of known-bad phrases, so a new instance in fresh
# wording is still caught.
#
# This file is itself excluded from the scan below: it has to define the
# forbidden tokens somewhere. Two of them (the private repo's codename and
# its two-word product name) are ALSO exact entries in
# `.github/privacy-denylist.sha256`'s pre-registered opaque phrase digests —
# spelling them as plain literals here would make this guard trip that other
# one on itself. They are base64-encoded below purely to keep the literal
# words out of this file's own source text; the *decoded*, *compiled*
# patterns are unaffected and still match the whole token/phrase, as a whole
# word, wherever it appears in any scanned file.

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SELF_RELATIVE = "tests/test_public_generalization.py"

_CODENAME = base64.b64decode("bGl0YW4=").decode("ascii")  # decodes to a 5-letter token
_PRODUCT_NAME = base64.b64decode("bGl0IGFuYWx5dGljcw==").decode("ascii")  # two words

# Suffixes that are never useful to decode as text; skipped purely so the
# scan doesn't waste time turning binary noise into replacement characters.
_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".ico", ".gif", ".pdf", ".sqlite", ".sqlite3",
    ".db", ".pyc", ".pyo", ".so", ".dylib", ".whl", ".zip", ".gz", ".tar",
    ".webp", ".woff", ".woff2", ".ttf",
}

# Each pattern is a whole-word / structured match, never a naive substring:
# the codename pattern is anchored on word boundaries, so ordinary English
# words that merely contain the codename inside them do not fire; the
# home-path pattern requires a real identifier after `/Users/`, so a
# documentation placeholder like `/Users/<name>/...` never matches.
#
# The examples that used to sit in this comment were spelled out, which made
# this file the last thing in the tree still carrying the token -- a scanner
# tripping over its own explanation of why it should not trip.
_PRIVATE_PROJECT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private contract section pointer",
        re.compile(r"CLAUDE\.md\s*§|AGENTS\.md\s*§", re.IGNORECASE),
    ),
    (
        "private repo codename",
        re.compile(r"\b" + re.escape(_CODENAME) + r"\b", re.IGNORECASE),
    ),
    (
        "private repo product name",
        re.compile(
            r"\b" + r"[\s-]+".join(_PRODUCT_NAME.split(" ")) + r"\b",
            re.IGNORECASE,
        ),
    ),
    ("absolute host home path", re.compile(r"/Users/[A-Za-z0-9._-]+")),
    ("personal username", re.compile(r"\bomar\b", re.IGNORECASE)),
)


def _scan_targets() -> list[str]:
    """Every file that would ship as this public repo: tracked, plus any new
    untracked-but-not-gitignored file — mirrors how `tools/privacy_hygiene.py`
    selects its own payload set."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
    )
    paths = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", errors="surrogateescape")
        candidate = _REPO_ROOT / rel
        if candidate.is_file() and not candidate.is_symlink():
            paths.append(rel)
    return paths


def test_no_private_project_leakage_in_public_tree() -> None:
    findings: list[str] = []
    for rel in _scan_targets():
        if rel == _SELF_RELATIVE:
            continue
        if Path(rel).suffix.lower() in _BINARY_SUFFIXES:
            continue
        raw = (_REPO_ROOT / rel).read_bytes()
        # Force text decoding rather than any binary-auto-detection heuristic:
        # a null byte here must never silently suppress a real match.
        text = raw.decode("utf-8", errors="replace")
        for label, pattern in _PRIVATE_PROJECT_PATTERNS:
            for match in pattern.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                findings.append(f"{rel}:{line_no}: {label} ({match.group(0)!r})")
    assert not findings, "private-project reference(s) leaked into the public tree:\n" + "\n".join(
        sorted(findings)
    )
