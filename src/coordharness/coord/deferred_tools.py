
from __future__ import annotations

import os
import re
from typing import Any


ENV_FLAG = "COORD_DEFERRED_TOOL_CATALOG"
PROMOTION_MANIFEST_ENV_FLAG = "COORD_DEFERRED_PROMOTION_MANIFEST_SHA256"
CLIENT_PROFILE_ID_ENV_FLAG = "COORD_MCP_CLIENT_PROFILE_ID"
CLIENT_PROFILE_SHA256_ENV_FLAG = "COORD_MCP_CLIENT_PROFILE_SHA256"
DEFERRED_HEAVY_TOOL_NAMES: set[str] = {"handoff_existing"}
ACCEPTED_PROMOTION_MANIFEST_SHA256: dict[str, str] = {}
ACCEPTED_PROMOTION_EVIDENCE_BINDINGS: dict[str, dict[str, str]] = {}
ACCEPTED_CLIENT_PROFILE_SHA256: dict[str, str] = {}
ACCEPTED_CLIENT_PROFILE_ACTORS: dict[str, str] = {}


def catalog_enabled(env: dict[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    raw = str(source.get(ENV_FLAG, "1") or "").strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def client_profile_attestation(env: dict[str, str] | None = None) -> dict[str, Any]:

    source = os.environ if env is None else env
    raw_profile_id = str(source.get(CLIENT_PROFILE_ID_ENV_FLAG, "") or "").strip()
    raw_supplied_sha256 = str(
        source.get(CLIENT_PROFILE_SHA256_ENV_FLAG, "") or ""
    ).strip()
    id_valid = bool(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}", raw_profile_id)
    )
    supplied_valid = bool(re.fullmatch(r"[0-9a-f]{64}", raw_supplied_sha256))
    lookup_profile_id = raw_profile_id if id_valid else ""
    raw_accepted_sha256 = str(
        ACCEPTED_CLIENT_PROFILE_SHA256.get(lookup_profile_id, "") or ""
    ).strip()
    raw_accepted_actor = str(
        ACCEPTED_CLIENT_PROFILE_ACTORS.get(lookup_profile_id, "") or ""
    ).strip().lower()
    accepted_valid = bool(re.fullmatch(r"[0-9a-f]{64}", raw_accepted_sha256))
    actor_valid = raw_accepted_actor in {"claude", "codex"}
    accepted_sha256 = raw_accepted_sha256 if accepted_valid else None
    accepted_actor = raw_accepted_actor if actor_valid else None
    attested = bool(
        id_valid
        and supplied_valid
        and accepted_valid
        and actor_valid
        and raw_supplied_sha256 == raw_accepted_sha256
    )
    if not raw_profile_id and not raw_supplied_sha256:
        state = "absent"
    elif not id_valid:
        state = "invalid_id"
    elif not supplied_valid:
        state = "invalid_hash"
    elif not accepted_valid or not actor_valid:
        state = "unaccepted"
    elif raw_supplied_sha256 != raw_accepted_sha256:
        state = "hash_mismatch"
    else:
        state = "attested"
    return {
        "profile_id": (
            raw_profile_id
            if id_valid
            else ("unattested" if state == "absent" else "invalid")
        ),
        "supplied_sha256": raw_supplied_sha256 if supplied_valid else None,
        "accepted_sha256": accepted_sha256,
        "expected_actor": accepted_actor,
        "attested": attested,
        "state": state,
        "id_valid": id_valid,
        "profile_env_flag": CLIENT_PROFILE_ID_ENV_FLAG,
        "profile_sha256_env_flag": CLIENT_PROFILE_SHA256_ENV_FLAG,
    }


def filter_deferred_tools(
    tool_names: list[str] | tuple[str, ...] | set[str],
    *,
    promoted: set[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    requested_promoted = set(promoted or set())
    source = os.environ if env is None else env
    raw_supplied_manifest_sha256 = str(
        source.get(PROMOTION_MANIFEST_ENV_FLAG, "") or ""
    ).strip()
    supplied_manifest_sha256 = (
        raw_supplied_manifest_sha256
        if re.fullmatch(r"[0-9a-f]{64}", raw_supplied_manifest_sha256)
        else ""
    )
    enabled = catalog_enabled(env)
    promoted = {
        name
        for name in requested_promoted
        if enabled
        and re.fullmatch(r"[0-9a-f]{64}", supplied_manifest_sha256)
        and re.fullmatch(
            r"[0-9a-f]{64}",
            str(ACCEPTED_PROMOTION_MANIFEST_SHA256.get(name, "") or ""),
        )
        and supplied_manifest_sha256 == ACCEPTED_PROMOTION_MANIFEST_SHA256[name]
    }
    registered = {str(tool) for tool in tool_names}
    registered_deferred = registered & DEFERRED_HEAVY_TOOL_NAMES
    visible: list[str] = []
    deferred: list[str] = []
    for name in sorted(registered):
        if name in DEFERRED_HEAVY_TOOL_NAMES and name not in promoted:
            deferred.append(name)
        else:
            visible.append(name)
    return {
        "visible": visible,
        "deferred": deferred,
        "candidate_tools": sorted(DEFERRED_HEAVY_TOOL_NAMES),
        "registered_deferred_candidates": sorted(registered_deferred),
        "not_registered": sorted(DEFERRED_HEAVY_TOOL_NAMES - registered_deferred),
        "promoted": sorted(promoted),
        "requested_promoted": sorted(requested_promoted),
        "promotion_manifest_sha256": supplied_manifest_sha256 or None,
        "accepted_promotion_manifests": dict(ACCEPTED_PROMOTION_MANIFEST_SHA256),
        "mode": "deferred_tool_catalog",
        "enabled": enabled,
        "env_flag": ENV_FLAG,
        "promotion_env_flag": PROMOTION_MANIFEST_ENV_FLAG,
    }
