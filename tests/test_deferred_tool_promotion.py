"""Promotion of a deferred tool is a handshake, and nothing measured it.

`handoff_existing` is withheld from the default MCP tool list because it is the
heavy, irreversible verb: it transfers a work item to another lane under a
version fence. A client that wants it has to *attest* -- supply the sha256 of a
promotion manifest that the server already accepts for that tool name. Merely
asking for the tool is not enough, and that distinction is the entire guard.

The only existing coverage counted the deferred set against the exposed set,
which is true whether or not the handshake is enforced: with the promotion
predicate replaced by `promoted = set(requested_promoted)` -- every request
granted, no manifest consulted -- the suite stayed green. These tests are shaped
against that ablation: an unattested request must leave the tool deferred, a
matching attestation must promote it, and neither may depend on the caller's
process environment leaking in.
"""

from __future__ import annotations

import pytest

from coordharness.coord import deferred_tools

_TOOL = "handoff_existing"
_ORDINARY = "claim_work"
_ACCEPTED_SHA = "a" * 64
_OTHER_SHA = "b" * 64
_REGISTERED = [_ORDINARY, _TOOL, "complete"]


@pytest.fixture(autouse=True)
def accepted_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    """One accepted manifest for the deferred tool, and nothing else."""
    monkeypatch.setattr(
        deferred_tools, "ACCEPTED_PROMOTION_MANIFEST_SHA256", {_TOOL: _ACCEPTED_SHA}
    )


def _filter(env: dict[str, str], promoted: set[str] | None = None) -> dict:
    return deferred_tools.filter_deferred_tools(_REGISTERED, promoted=promoted, env=env)


def test_the_heavy_tool_is_withheld_by_default() -> None:
    result = _filter({})

    assert result["deferred"] == [_TOOL]
    assert _TOOL not in result["visible"]
    assert _ORDINARY in result["visible"]
    assert result["promoted"] == []


def test_asking_for_the_tool_without_a_manifest_does_not_promote_it() -> None:
    """The defect this closes: request-as-authorisation."""
    result = _filter({}, promoted={_TOOL})

    assert result["requested_promoted"] == [_TOOL]
    assert result["promoted"] == []
    assert result["deferred"] == [_TOOL]
    assert _TOOL not in result["visible"]


@pytest.mark.parametrize(
    "supplied",
    ["", "   ", "not-a-sha", _OTHER_SHA, _ACCEPTED_SHA.upper(), _ACCEPTED_SHA[:63]],
    ids=["empty", "blank", "malformed", "wrong", "uppercase", "truncated"],
)
def test_a_manifest_that_is_not_the_accepted_one_does_not_promote(
    supplied: str,
) -> None:
    result = _filter(
        {deferred_tools.PROMOTION_MANIFEST_ENV_FLAG: supplied}, promoted={_TOOL}
    )

    assert result["promoted"] == []
    assert result["deferred"] == [_TOOL]


def test_the_accepted_manifest_promotes_only_the_tool_it_names() -> None:
    result = _filter(
        {deferred_tools.PROMOTION_MANIFEST_ENV_FLAG: _ACCEPTED_SHA},
        promoted={_TOOL, _ORDINARY},
    )

    assert result["promoted"] == [_TOOL]
    assert result["deferred"] == []
    assert _TOOL in result["visible"]
    assert result["promotion_manifest_sha256"] == _ACCEPTED_SHA


def test_a_tool_with_no_accepted_manifest_can_never_be_promoted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deferred_tools, "ACCEPTED_PROMOTION_MANIFEST_SHA256", {})

    result = _filter(
        {deferred_tools.PROMOTION_MANIFEST_ENV_FLAG: _ACCEPTED_SHA}, promoted={_TOOL}
    )

    assert result["promoted"] == []
    assert result["deferred"] == [_TOOL]


def test_disabling_the_catalog_does_not_become_a_promotion_bypass() -> None:
    """Turning the feature flag off must not be a cheaper route to the tool."""
    result = _filter(
        {
            deferred_tools.ENV_FLAG: "0",
            deferred_tools.PROMOTION_MANIFEST_ENV_FLAG: _ACCEPTED_SHA,
        },
        promoted={_TOOL},
    )

    assert result["enabled"] is False
    assert result["promoted"] == []
    assert result["deferred"] == [_TOOL]
    assert _TOOL not in result["visible"]


def test_the_env_mapping_is_honoured_over_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(deferred_tools.PROMOTION_MANIFEST_ENV_FLAG, _ACCEPTED_SHA)

    result = _filter({}, promoted={_TOOL})

    assert result["promoted"] == []
    assert result["promotion_manifest_sha256"] is None


def test_client_profile_attestation_states_are_distinguished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = "acme-console"
    monkeypatch.setattr(
        deferred_tools, "ACCEPTED_CLIENT_PROFILE_SHA256", {profile: _ACCEPTED_SHA}
    )
    monkeypatch.setattr(
        deferred_tools, "ACCEPTED_CLIENT_PROFILE_ACTORS", {profile: "claude"}
    )
    id_flag = deferred_tools.CLIENT_PROFILE_ID_ENV_FLAG
    sha_flag = deferred_tools.CLIENT_PROFILE_SHA256_ENV_FLAG

    cases = {
        "absent": ({}, False),
        "invalid_id": ({id_flag: "!", sha_flag: _ACCEPTED_SHA}, False),
        "invalid_hash": ({id_flag: profile, sha_flag: "nope"}, False),
        "unaccepted": ({id_flag: "other-console", sha_flag: _ACCEPTED_SHA}, False),
        "hash_mismatch": ({id_flag: profile, sha_flag: _OTHER_SHA}, False),
        "attested": ({id_flag: profile, sha_flag: _ACCEPTED_SHA}, True),
    }
    for state, (env, attested) in cases.items():
        result = deferred_tools.client_profile_attestation(env)
        assert result["state"] == state, state
        assert result["attested"] is attested, state

    attested = deferred_tools.client_profile_attestation(
        {id_flag: profile, sha_flag: _ACCEPTED_SHA}
    )
    assert attested["expected_actor"] == "claude"
    assert attested["profile_id"] == profile
