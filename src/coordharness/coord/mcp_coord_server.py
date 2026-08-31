from __future__ import annotations

import functools
import hashlib
import inspect
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from mcp.server.fastmcp import Context, FastMCP

from coordharness.config import HARNESS_ROOT
from coordharness import config as harness_config
from coordharness.coord import coord_db, deferred_tools
from coordharness.coord.config import (
    DEFAULT_DB_PATH,
    connect,
    connect_ro,
    configured_lanes as _configured_lanes,
    counterpart_lane as _counterpart_lane,
    lane_set as _lane_set,
    lanes_display as _lanes_display,
)
from coordharness.coord.continuation_contract import (
    normalize_resume_trigger_contract,
    require_park_resume_contract,
)
from coordharness.coord.ingest import normalize_session_id, resolve_identity
from coordharness.coord.policy.pipeline import apply_output_budget, run_boundary_policy
from coordharness.jobs import status as job_status
from coordharness.knowledge import facts, kfts, read_surface
from coordharness.knowledge.context_federator import (
    DEFAULT_POINTER_READ_BYTES,
    ContextFederator,
    config_for_context_profile,
    default_context_providers,
    read_context_pointer,
)
from coordharness.util import sha256_file

INTERACTIVE_LEASE_S = 3600.0

_HARNESS = Path(HARNESS_ROOT)
_REPO_ROOT = _HARNESS.parent
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_TERMINAL_ROADMAP_STATUSES = {"done", "failed", "archived", "superseded", "cancelled", "canceled", "closed", "skipped"}
_ACTIVE_PROGRESS_STATUSES = {"running", "blocked"}
_NEXT_WORK_STATUSES = {"queued", "planned"}
_WORK_CONTEXT_KEYS = (
    "id", "roadmap_id", "job_id", "name", "title", "display", "status",
    "assignee", "assigned_by", "priority", "surface", "epic", "parent",
    "module", "lane", "sublane", "resource_class", "done_signal",
    "done_signal_alt", "acceptance", "acceptance_json", "context_pack_ref",
    "note", "next_step", "resume_when", "blocked_reason_class",
    "resume_predicate_json", "continuation_ready_at",
    "completion_requested_at", "last_update", "updated_at", "created_at", "source",
)
_MCP_TOOL_NAMES = {
    "claim_work", "classify_blocked", "recover_blocked", "correct_tier", "resume_parked", "heartbeat", "release", "complete",
    "park", "block", "board", "inbox", "audit", "verdict",
    "orient", "preflight", "next_work", "work_context", "event_context", "inbox_recent", "knowledge_search",
    "read_note", "facts_lookup", "runs", "handoff_existing",
    "facts_query", "knowledge_index_status", "memory_proposals_list", "memory_proposals_get",
    "request_audit", "note", "decision", "session_closeout",
    "get_decision_context",
    "declare_write_set", "write_set_conflicts", "route",
}
_SERVER_PROMOTION_CANDIDATES = frozenset({"handoff_existing"})
_DEFAULT_BOARD_LIMIT = 100
_MAX_BOARD_INLINE_LIMIT = 100
# A write set is a set of prefixes, not a file list. The bound is here to keep
# one declaration from becoming a manifest of every file in a module.
_MAX_WRITE_SCOPES = 64
_MAX_KNOWLEDGE_SEARCH_LIMIT = 40
_KNOWLEDGE_PROVIDER_NAMES = {
    "board",
    "facts",
    "kfts",
    "artifact_manifest",
    "accepted_memory",
    "memory_proposals",
    "board_history",
}
_AUDIT_INLINE_BODY_LIMIT = min(2048, 12_000)
_AUDIT_PAYLOAD_JSON_LIMIT = 12_000
_KNOWLEDGE_TRANSPORT_RESERVE_BYTES = 256
_STRUCTURED_EVENT_KINDS = {"handoff", "audit_request", "audit_verdict"}
_GENERIC_AUDIT_RESERVED_KINDS = frozenset({
    "decision",
    "session_closeout",
    "handoff",
    "audit_req",
    "audit_request",
    "handoff_superseded",
    "lifecycle_rejected",
    "coord_done",
    "done",
    "complete",
    "completed",
    "claim",
    "claimed",
    "claim_conflict",
    "claim_takeover",
    "heartbeat",
    "block",
    "blocked",
    "release",
    "released",
    "park",
    "parked",
    "pause",
    "paused",
    "resume",
    "resumed",
    "fail",
    "failed",
    "cancel",
    "cancelled",
    "canceled",
    "close",
    "closed",
    "archive",
    "archived",
    "supersede",
    "superseded",
    "skip",
    "skipped",
    "running",
    "queued",
    "planned",
    "rubric_verdict",
    "proof_attached",
    "proof_repath",
    "work_grouped",
    "reopen",
    "observed_seen",
    "stale",
    "registered",
    "spawned",
    "reaped",
    "backlog_projection_write",
    "status_reconcile",
    "coord_status_reconcile",
})
_AUDIT_VERDICTS = {"PASS", "FLAG", "BLOCKED"}
_PREFLIGHT_ID_LIST_LIMIT = 50
_DEFAULT_INBOX_RECENT_LIMIT = 8
_DEFAULT_ORIENT_PEER_LIMIT = 4
_DEFAULT_ORIENT_INBOX_LIMIT = 4
_DEFAULT_ORIENT_WORK_LIMIT = 4
_EVENT_BODY_PREVIEW_LIMIT = 240
_EVENT_PAYLOAD_KEY_LIMIT = 25
_HANDOFF_EVENT_ID_PREVIEW_LIMIT = 64
_POLICY_VERBOSE_ENV = "COORD_POLICY_VERBOSE"
_MCP_PROTOCOL_EPOCH = "coordharness-coord.mcp.v2"
_SERVER_BUILD_MANIFEST_SCHEMA = "coordharness.mcp-build-manifest.v1"
_SERVER_BUILD_FILE_PATHS = (
    "config.py",
    "coord/agent_cli.py",
    "coord/board_context.py",
    "coord/config.py",
    "coord/coord_db.py",
    "coord/create_schema.py",
    "coord/deferred_tools.py",
    "coord/exact_authority.py",
    "coord/exact_query_core.py",
    "coord/ingest.py",
    "coord/loop_contracts.py",
    "coord/mcp_coord_server.py",
    "coord/output_budget.py",
    "coord/policy/pipeline.py",
    "coord/process_liveness.py",
    "coord/r4_plane_authority.py",
    "coord/run_events.py",
    "coord/schema.sql",
    "coord/staleness.py",
    "coord/token_ledger_rollup.py",
    "coord/work_query_v2.py",
    "jobs/status.py",
    "knowledge/context_federator.py",
    "knowledge/facts.py",
    "knowledge/kfts.py",
    "coord/mcp_client_compatibility.json",
)


def _compute_server_build_manifest() -> tuple[tuple[str, str], ...]:

    rows: list[tuple[str, str]] = []
    for relative_path in _SERVER_BUILD_FILE_PATHS:
        path = _PACKAGE_ROOT / relative_path
        if not path.is_file():
            raise RuntimeError(f"MCP build dependency is missing: {relative_path}")
        rows.append((relative_path, hashlib.sha256(path.read_bytes()).hexdigest()))
    return tuple(rows)


_SERVER_BUILD_MANIFEST = _compute_server_build_manifest()
_SERVER_BUILD_MANIFEST_SUBJECT = {
    "schema_version": _SERVER_BUILD_MANIFEST_SCHEMA,
    "files": _SERVER_BUILD_MANIFEST,
}
_SERVER_STARTED_AT = time.time()
_SERVER_BUILD_SHA256 = hashlib.sha256(
    json.dumps(
        _SERVER_BUILD_MANIFEST_SUBJECT,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()
_SERVER_INSTANCE_ID = hashlib.sha256(
    f"{os.getpid()}:{_SERVER_STARTED_AT:.9f}:{_SERVER_BUILD_SHA256}".encode("utf-8")
).hexdigest()[:20]
_CAPABILITY_HANDSHAKE_MAX_BYTES = 2_048
_POLICY_EPOCH_DOC_PATHS = ("README.md",)


def _compute_policy_epoch() -> dict[str, Any]:

    docs: list[str] = []
    subject: list[tuple[str, int, int]] = []
    for relative_path in _POLICY_EPOCH_DOC_PATHS:
        path = _HARNESS / relative_path
        docs.append(relative_path)
        try:
            stat = path.stat()
            subject.append((relative_path, stat.st_mtime_ns, stat.st_size))
        except OSError:
            subject.append((relative_path, -1, -1))
    epoch = hashlib.sha256(
        json.dumps(subject, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return {"docs": docs, "epoch": epoch}


_POLICY_EPOCH = _compute_policy_epoch()
_MCP_CLIENT_COMPATIBILITY_PATH = Path(__file__).resolve().parent / "mcp_client_compatibility.json"
_MCP_CLIENT_TEXT_MAX_CHARS = 80
_MCP_CLIENT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/ ()-]{0,79}")
_MCP_CLIENT_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}")
_MCP_PROTOCOL_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,39}")
_PROMOTION_REBIND_MUTABLE_DEPENDENCIES = frozenset({
    "src/coordharness/coordination/deferred_tools.py",
})


def _load_mcp_client_compatibility_policy() -> dict[str, Any]:

    policy = json.loads(_MCP_CLIENT_COMPATIBILITY_PATH.read_text())
    if policy.get("schema_version") != "coordharness.mcp-client-compatibility.v1":
        raise RuntimeError("unsupported MCP client compatibility policy schema")
    if policy.get("authority_effect") != "observability_only":
        raise RuntimeError("MCP client compatibility policy must remain observability_only")
    if policy.get("patch_equality_required") is not False:
        raise RuntimeError("MCP client compatibility policy must not require patch equality")
    clients = policy.get("clients")
    if not isinstance(clients, list) or not clients:
        raise RuntimeError("MCP client compatibility policy requires client rows")
    return policy


def _bounded_client_text(value: Any, pattern: re.Pattern[str]) -> tuple[str | None, str]:

    if value is None or value == "":
        return None, "unavailable"
    if not isinstance(value, (str, int)):
        return None, "invalid"
    text = str(value).strip()
    if len(text) > _MCP_CLIENT_TEXT_MAX_CHARS or pattern.fullmatch(text) is None:
        return None, "invalid"
    return text, "observed"


def _numeric_version(value: str) -> tuple[int, ...]:
    if _MCP_CLIENT_VERSION_RE.fullmatch(value) is None:
        raise ValueError("version must be a dotted numeric version")
    return tuple(int(part) for part in value.split("."))


def _mcp_client_compatibility(client_name: str | None, client_version: str | None) -> dict[str, Any]:

    policy = _load_mcp_client_compatibility_policy()
    policy_sha256 = hashlib.sha256(_MCP_CLIENT_COMPATIBILITY_PATH.read_bytes()).hexdigest()
    base: dict[str, Any] = {
        "state": "unavailable",
        "client_id": None,
        "minimum_version": None,
        "patch_equality_required": False,
        "authority_effect": "observability_only",
        "policy_sha256": policy_sha256,
    }
    if client_name is None:
        return base
    name_folded = client_name.casefold()
    match = next(
        (
            row
            for row in policy["clients"]
            if any(
                str(token).casefold() in name_folded
                for token in (row.get("name_contains") or [])
                if str(token).strip()
            )
        ),
        None,
    )
    if match is None:
        base["state"] = "observed_unconstrained"
        return base
    minimum = str(match.get("minimum_version") or "")
    base.update(client_id=str(match.get("client_id") or "unknown"), minimum_version=minimum)
    if client_version is None:
        base["state"] = "version_unavailable"
        return base
    try:
        observed_parts = _numeric_version(client_version)
        minimum_parts = _numeric_version(minimum)
    except ValueError:
        base["state"] = "version_unparseable"
        return base
    width = max(len(observed_parts), len(minimum_parts))
    observed_parts += (0,) * (width - len(observed_parts))
    minimum_parts += (0,) * (width - len(minimum_parts))
    base["state"] = "supported" if observed_parts >= minimum_parts else "below_minimum"
    return base


def _extract_mcp_client_transport(ctx: Context | None) -> dict[str, Any]:

    params = None
    try:
        request_context = ctx.request_context if ctx is not None else None
        session = request_context.session if request_context is not None else None
        params = session.client_params if session is not None else None
    except (AttributeError, RuntimeError, ValueError):
        params = None
    implementation = getattr(params, "clientInfo", None) if params is not None else None
    name, name_state = _bounded_client_text(
        getattr(implementation, "name", None), _MCP_CLIENT_NAME_RE
    )
    version, version_state = _bounded_client_text(
        getattr(implementation, "version", None), _MCP_CLIENT_VERSION_RE
    )
    protocol_version, protocol_state = _bounded_client_text(
        getattr(params, "protocolVersion", None), _MCP_PROTOCOL_VERSION_RE
    )
    capabilities_sha256 = None
    capabilities = getattr(params, "capabilities", None) if params is not None else None
    if capabilities is not None:
        try:
            subject = capabilities.model_dump(mode="json", by_alias=True, exclude_none=True)
            capabilities_sha256 = hashlib.sha256(
                json.dumps(subject, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        except (AttributeError, TypeError, ValueError):
            capabilities_sha256 = None
    return {
        "name": name,
        "name_state": name_state,
        "version": version,
        "version_state": version_state,
        "protocol_version": protocol_version,
        "protocol_version_state": protocol_state,
        "capabilities_sha256": capabilities_sha256,
    }


def _normalize_mcp_client_transport(value: dict[str, Any] | None) -> dict[str, Any]:

    source = value or {}
    name, name_state = _bounded_client_text(source.get("name"), _MCP_CLIENT_NAME_RE)
    version, version_state = _bounded_client_text(
        source.get("version"), _MCP_CLIENT_VERSION_RE
    )
    protocol_version, protocol_state = _bounded_client_text(
        source.get("protocol_version"), _MCP_PROTOCOL_VERSION_RE
    )
    capabilities_sha256 = source.get("capabilities_sha256")
    if not isinstance(capabilities_sha256, str) or re.fullmatch(
        r"[0-9a-f]{64}", capabilities_sha256
    ) is None:
        capabilities_sha256 = None
    return {
        "name": name,
        "name_state": name_state,
        "version": version,
        "version_state": version_state,
        "protocol_version": protocol_version,
        "protocol_version_state": protocol_state,
        "capabilities_sha256": capabilities_sha256,
    }

def _runtime_promotion_evidence_attested(tool: str, catalog: dict[str, Any]) -> dict[str, Any]:

    supplied = str(catalog.get("promotion_manifest_sha256") or "")
    accepted = str((catalog.get("accepted_promotion_manifests") or {}).get(tool) or "")
    binding = deferred_tools.ACCEPTED_PROMOTION_EVIDENCE_BINDINGS.get(tool) or {}
    raw_path = str(binding.get("path") or "").strip()
    path = (_HARNESS / raw_path).resolve(strict=False) if raw_path else None
    expected_schema = str(binding.get("schema_version") or "")
    receipt: dict[str, Any] = {
        "tool": tool,
        "attested": False,
        "state": "missing_evidence_path",
        "evidence_sha256": None,
        "dependency_count": 0,
    }
    evidence_root = (_HARNESS / "data_local" / "analysis_outputs").resolve(strict=False)
    try:
        binding_is_local = bool(path and path.is_relative_to(evidence_root))
    except (AttributeError, ValueError):
        binding_is_local = False
    if not binding_is_local or not re.fullmatch(
        r"coordharness\.mcp-writer-promotion-evidence\.v[0-9]+", expected_schema
    ):
        receipt["state"] = "invalid_evidence_binding"
        return receipt
    if path is None or not path.is_file():
        return receipt
    raw = path.read_bytes()
    evidence_sha256 = hashlib.sha256(raw).hexdigest()
    receipt["evidence_sha256"] = evidence_sha256
    if not supplied or supplied != accepted or evidence_sha256 != accepted:
        receipt["state"] = "evidence_hash_mismatch"
        return receipt
    try:
        evidence = json.loads(raw)
        manifest = {
            str(item[0]): str(item[1])
            for item in evidence.get("dependency_manifest", [])
            if isinstance(item, list) and len(item) == 2
        }
    except (TypeError, ValueError):
        receipt["state"] = "malformed_evidence"
        return receipt
    current = dict(_SERVER_BUILD_MANIFEST)
    receipt["dependency_count"] = len(manifest)
    if (
        evidence.get("schema_version") != expected_schema
        or evidence.get("tool") != tool
        or set(manifest) != set(current)
    ):
        receipt["state"] = "evidence_manifest_shape_mismatch"
        return receipt
    drift = sorted(
        relative_path
        for relative_path, digest in current.items()
        if relative_path not in _PROMOTION_REBIND_MUTABLE_DEPENDENCIES
        and manifest.get(relative_path) != digest
    )
    if drift:
        receipt["state"] = "source_dependency_drift"
        receipt["drift_count"] = len(drift)
        return receipt
    receipt["attested"] = True
    receipt["state"] = "attested"
    return receipt


def _server_tool_catalog(*, env: dict | None = None) -> dict[str, Any]:

    profile = deferred_tools.client_profile_attestation(env)
    catalog = deferred_tools.filter_deferred_tools(
        _MCP_TOOL_NAMES,
        promoted=(set(_SERVER_PROMOTION_CANDIDATES) if profile["attested"] else set()),
        env=env,
    )
    runtime_attestations = {
        name: _runtime_promotion_evidence_attested(name, catalog)
        for name in sorted(_SERVER_PROMOTION_CANDIDATES)
        if name in catalog["promoted"]
    }
    rejected = {
        name for name, receipt in runtime_attestations.items() if not receipt["attested"]
    }
    if rejected:
        catalog["promoted"] = sorted(set(catalog["promoted"]) - rejected)
        catalog["visible"] = sorted(set(catalog["visible"]) - rejected)
        catalog["deferred"] = sorted(set(catalog["deferred"]) | rejected)
    catalog["runtime_promotion_attestations"] = runtime_attestations
    catalog["client_profile"] = profile
    return catalog


def _get_conn(db_path: str | Path | None = None):
    if db_path is not None:
        p = Path(db_path)
    elif (ev := os.environ.get("COORD_COORD_DB")):
        p = Path(ev)
    else:
        p = DEFAULT_DB_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"coord.db is not initialized: {p}; run the coordination schema admin path first"
        )
    return connect(p)


def _capability_handshake(
    db_path: str | Path | None = None,
    *,
    env: dict | None = None,
    client_transport: dict[str, Any] | None = None,
) -> dict[str, Any]:

    catalog = _server_tool_catalog(env=env)
    observed_client = _normalize_mcp_client_transport(client_transport)
    accepted_for_server = {
        name: catalog["accepted_promotion_manifests"][name]
        for name in sorted(_SERVER_PROMOTION_CANDIDATES)
        if name in catalog["accepted_promotion_manifests"]
    }
    catalog_subject = {
        "server_candidates": sorted(_SERVER_PROMOTION_CANDIDATES),
        "accepted_manifests": accepted_for_server,
        "supplied_manifest": catalog["promotion_manifest_sha256"],
        "enabled": catalog["enabled"],
        "visible": catalog["visible"],
        "deferred": catalog["deferred"],
        "promoted": catalog["promoted"],
        "client_profile": {
            key: catalog["client_profile"][key]
            for key in (
                "profile_id",
                "supplied_sha256",
                "accepted_sha256",
                "expected_actor",
                "attested",
            )
        },
    }
    conn = _get_read_conn(db_path)
    try:
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    finally:
        conn.close()
    receipt = {
        "protocol_epoch": _MCP_PROTOCOL_EPOCH,
        "server_build_sha256": _SERVER_BUILD_SHA256,
        "server_instance_id": _SERVER_INSTANCE_ID,
        "server_started_at": _SERVER_STARTED_AT,
        "coord_schema_version": schema_version,
        "tool_catalog_sha256": hashlib.sha256(
            json.dumps(catalog_subject, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "visible_tool_count": len(catalog["visible"]),
        "lifecycle_authority": "coord.db",
        "writer_mode": "cli_authoritative_mcp_writers_gated",
        "client_transport": {
            "name": observed_client.get("name"),
            "version": observed_client.get("version"),
            "protocol_version": observed_client.get("protocol_version"),
        },
    }
    receipt_bytes = len(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    if receipt_bytes > _CAPABILITY_HANDSHAKE_MAX_BYTES:
        raise RuntimeError(
            "capability handshake bounded response invariant exceeded "
            f"{_CAPABILITY_HANDSHAKE_MAX_BYTES} bytes"
        )
    return receipt


def _get_read_conn(db_path: str | Path | None = None):
    if db_path is not None:
        return connect_ro(Path(db_path))
    if ev := os.environ.get("COORD_COORD_DB"):
        return connect_ro(Path(ev))
    return connect_ro()


def _refresh_native_cockpit(conn) -> None:
    try:
        from coordharness.coord import native_cockpit

        native_cockpit.request_refresh(conn, reason="mcp_coord_writer")
    except Exception:
        return None


def _resolve(env: dict | None = None) -> dict:
    return resolve_identity(env)


def _session_label_fields(identity: dict[str, Any]) -> dict[str, Any]:
    return {
        key: identity.get(key)
        for key in (
            "human_label",
            "external_thread_id",
            "conversation_title",
            "worktree_id",
            "label_source",
        )
        if identity.get(key)
    }


def _roadmap_path() -> Path:
    return harness_config.state_dir() / "roadmap_backlog.json"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as exc:
        import sys as _sys
        print(f"coordharness.coord: WARNING _read_json failed for {path}: {exc!r} — using default",
              file=_sys.stderr)
        return default


def _iter_backlog_items(backlog: dict[str, Any]):
    for section in ("today_live_jobs", "items", "epics"):
        for row in backlog.get(section, []) or []:
            if isinstance(row, dict):
                yield section, row


def _row_id(row: dict[str, Any]) -> str:
    for key in ("id", "roadmap_id", "job_id", "name"):
        val = str(row.get(key) or "").strip()
        if val:
            return val
    return ""


def _find_backlog_row(backlog: dict[str, Any], work_id: str) -> tuple[str, dict[str, Any]]:
    matches = [
        (section, row)
        for section, row in _iter_backlog_items(backlog if isinstance(backlog, dict) else {})
        if _row_id(row) == work_id
    ]
    if not matches:
        raise ValueError(f"work_id {work_id!r} is not in roadmap_backlog.json")
    if len(matches) > 1:
        sections = ", ".join(sorted({section for section, _row in matches}))
        raise ValueError(f"work_id {work_id!r} is ambiguous across [{sections}]")
    return matches[0]


def _resolve_actor(actor: str | None = None, env: dict | None = None) -> str:
    if actor and str(actor).strip():
        return harness_config.actor_name(str(actor))
    identity = _resolve(env)
    return harness_config.actor_name(str(identity.get("actor") or "local"))


def _resolve_tool_identity(
    *,
    actor: str | None,
    session_id: str | None,
    env: dict | None,
) -> tuple[dict[str, Any], str, str, dict[str, Any]]:
    explicit_actor = str(actor or "").strip()
    explicit_sid = str(session_id or "").strip()
    if env is None:
        if not explicit_actor or not explicit_sid:
            raise ValueError(
                "coordharness-coord MCP writer calls must pass explicit actor and session_id; "
                "the MCP server process environment is not caller identity"
            )
        resolved_actor = explicit_actor.lower()
        explicit_sid = normalize_session_id(resolved_actor, explicit_sid)
        expected_actor = coord_db.expected_actor_for_session_id(explicit_sid)
        if expected_actor and expected_actor != resolved_actor:
            raise ValueError(
                f"session_id {explicit_sid!r} requires actor={expected_actor!r}, "
                f"got actor={resolved_actor!r}"
            )
        return (
            {"actor": resolved_actor, "session_id": explicit_sid, "runner_type": None},
            resolved_actor,
            explicit_sid,
            {},
        )

    identity = _resolve(env)
    env_actor = str(identity.get("actor") or "").strip().lower()
    resolved_actor = str(explicit_actor or env_actor or "codex").strip().lower()
    if actor and resolved_actor != env_actor and not explicit_sid:
        raise ValueError(
            f"MCP actor={resolved_actor!r} cannot inherit env session for actor={env_actor!r}; "
            "pass an explicit matching session_id"
        )
    resolved_sid = normalize_session_id(
        resolved_actor,
        explicit_sid or str(identity.get("session_id") or "").strip(),
    )
    if not resolved_sid:
        raise ValueError("claim_work requires a session_id (set CLAUDE_CODE_SESSION_ID or pass session_id)")
    expected_actor = coord_db.expected_actor_for_session_id(resolved_sid)
    if expected_actor and expected_actor != resolved_actor:
        raise ValueError(
            f"session_id {resolved_sid!r} requires actor={expected_actor!r}, "
            f"got actor={resolved_actor!r}"
        )
    identity_matches_actor = resolved_actor == env_actor
    label_fields = _session_label_fields(identity) if identity_matches_actor else {}
    if not identity_matches_actor:
        identity = dict(identity)
        identity["runner_type"] = None
    return identity, resolved_actor, resolved_sid, label_fields


def _resolve_process_bound_identity(
    *,
    actor: str | None,
    session_id: str | None,
    env: dict | None,
    action: str,
) -> tuple[str, str]:
    identity = _resolve(env)
    process_actor = str(identity.get("actor") or "").strip().lower()
    process_sid = normalize_session_id(
        process_actor,
        str(identity.get("session_id") or "").strip(),
    )
    if (
        process_actor not in _lane_set()
        or not process_sid
        or process_sid.startswith("pid:")
        or ":pid:" in process_sid
        or process_sid.startswith("starship:")
        or ":starship:" in process_sid
    ):
        raise ValueError(f"{action} requires an exact client process identity")
    asserted_actor = str(actor or process_actor).strip().lower()
    asserted_sid = normalize_session_id(
        asserted_actor,
        str(session_id or process_sid).strip(),
    )
    if asserted_actor != process_actor or asserted_sid != process_sid:
        raise ValueError(
            f"{action} identity must match the stdio process: "
            f"expected {process_actor}/{process_sid}, got {asserted_actor}/{asserted_sid}"
        )
    return process_actor, process_sid


def _mcp_policy(
    conn,
    *,
    action: str,
    work_id: str | None,
    actor: str | None = None,
    session_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    full_payload = payload or {}
    if action in {"claim", "heartbeat", "release", "done"} and not _env_truthy(_POLICY_VERBOSE_ENV):
        return _mcp_lifecycle_policy(action=action, work_id=work_id, payload=full_payload)
    return run_boundary_policy(
        boundary="mcp",
        action=action,
        work_id=str(work_id or ""),
        actor=actor,
        session_id=session_id,
        payload=payload or {},
        conn=conn,
    )


def _mcp_lifecycle_policy(
    *,
    action: str,
    work_id: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    reason = None
    if action == "done":
        row = payload.get("row")
        if isinstance(row, dict):
            try:
                from coordharness.coord.agent_cli import done_terminal_block_reason

                reason = done_terminal_block_reason(row)
            except Exception as exc:
                reason = f"structured status unavailable: {type(exc).__name__}: {exc}"
    out: dict[str, Any] = {
        "ok": reason is None,
        "blocked": reason is not None,
        "block_reason": reason,
        "warning_count": 0,
        "results": [],
        "warnings": [],
        "boundary": "mcp",
        "action": action,
        "work_id": str(work_id or ""),
        "pass_order": ["structured_status"] if action == "done" else [],
    }
    if reason:
        out["results"] = [
            {
                "order": 1,
                "name": "structured_status",
                "mode": "enforce",
                "status": "block",
                "severity": "error",
                "reason": reason,
                "detail": {},
                "write_blocking": True,
                "lifecycle_mutation": False,
            }
        ]
    return out


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _policy_non_ok(policy: dict[str, Any]) -> list[dict[str, Any]]:
    rows = policy.get("results")
    if not isinstance(rows, list):
        rows = policy.get("warnings") if isinstance(policy.get("warnings"), list) else []
    non_ok: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("status") == "ok":
            continue
        item = {
            key: row.get(key)
            for key in ("name", "status", "reason")
            if row.get(key) not in (None, "")
        }
        non_ok.append(item)
    return non_ok


def _compact_policy_response(policy: dict[str, Any]) -> dict[str, Any]:
    non_ok = _policy_non_ok(policy)
    needs_detail = bool(policy.get("blocked")) or bool(non_ok) or int(policy.get("warning_count") or 0) > 0
    if _env_truthy(_POLICY_VERBOSE_ENV) or needs_detail:
        out = dict(policy)
        out["non_ok"] = non_ok
        return out
    out = {
        "ok": bool(policy.get("ok")),
        "blocked": bool(policy.get("blocked")),
        "block_reason": policy.get("block_reason"),
        "warning_count": int(policy.get("warning_count") or 0),
        "non_ok": [],
    }
    for key in ("boundary", "action", "work_id"):
        if key in policy:
            out[key] = policy.get(key)
    return out


def _bounded_audit_payload_json(payload: dict[str, Any], *, limit: int = _AUDIT_PAYLOAD_JSON_LIMIT) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str)
    raw_bytes = len(raw.encode("utf-8"))
    if raw_bytes <= limit:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    summary = {
        "_truncated": True,
        "_original_bytes": raw_bytes,
        "_sha256": digest,
        "_keys_sample": sorted(str(key) for key in payload.keys())[:50],
    }
    for key in (
        "schema_version",
        "work_id",
        "kind",
        "verdict",
        "operation_id",
        "operation_request_sha256",
        "output_budget",
    ):
        if key in payload:
            summary[key] = payload[key]
    return json.dumps(summary, sort_keys=True, default=str)


def _bounded_text_bytes(value: Any, limit: int) -> str:
    payload = str(value or "").encode("utf-8")
    if len(payload) <= limit:
        return payload.decode("utf-8")
    return payload[: max(0, int(limit))].decode("utf-8", errors="ignore")


def _compact_json_text(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _knowledge_transport_wire_bytes(text: str) -> int:
    envelope = {
        "jsonrpc": "2.0",
        "id": "x" * 64,
        "result": {
            "content": [{"type": "text", "text": text}],
            "isError": False,
        },
    }
    return len(_compact_json_text(envelope).encode("utf-8"))


def _measure_knowledge_response(response: dict[str, Any]) -> tuple[str, int]:
    for _ in range(8):
        text = _compact_json_text(response)
        response_bytes = len(text.encode("utf-8"))
        transport_bytes = _knowledge_transport_wire_bytes(text)
        if (
            response.get("response_bytes") == response_bytes
            and response.get("transport_bytes") == transport_bytes
        ):
            return text, transport_bytes
        response["response_bytes"] = response_bytes
        response["transport_bytes"] = transport_bytes
    text = _compact_json_text(response)
    return text, _knowledge_transport_wire_bytes(text)


def _knowledge_response_fits(response: dict[str, Any], budget: int) -> bool:
    _, transport_bytes = _measure_knowledge_response(response)
    return transport_bytes + _KNOWLEDGE_TRANSPORT_RESERVE_BYTES <= int(budget)


def _record_mcp_lifecycle_run_event(
    conn,
    *,
    work_id: str | None,
    session_id: str | None,
    actor: str | None,
    verb: str,
    action: str,
    status: str,
    step: str | None = None,
    claim_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int | None:
    if not work_id or not session_id:
        return None
    try:
        from coordharness.coord.run_events import record_coord_lifecycle_event

        return record_coord_lifecycle_event(
            conn,
            work_id=str(work_id),
            session_id=str(session_id),
            actor=str(actor or coord_db.expected_actor_for_session_id(str(session_id)) or "mcp"),
            verb=verb,
            action=action,
            status=status,
            step=step,
            claim_id=claim_id,
            source="mcp_coord_server",
            source_event_id=f"mcp:{claim_id or work_id}:{verb}:{coord_db.db_now(conn):.6f}",
            metadata=metadata if metadata else None,
        )
    except Exception:
        return None


def _post_mcp_lifecycle_rejected(
    *,
    verb: str,
    reason: str,
    work_id: str | None = None,
    actor: str | None = None,
    session_id: str | None = None,
    db_path: str | None = None,
) -> None:
    conn = _get_conn(db_path)
    try:
        norm_actor = str(actor or "").strip().lower() or None
        clean_reason = str(reason or "")[:2000]
        payload = {
            "verb": verb,
            "reason": clean_reason,
            "source": "mcp_coord_server",
            "schema_version": 1,
            "canonical_lifecycle_event": False,
            "attempted_kind": f"{norm_actor}_{verb}" if norm_actor else verb,
        }
        coord_db.post_event(
            conn,
            kind="lifecycle_rejected",
            actor=norm_actor,
            session_id=str(session_id or "").strip() or None,
            work_id=str(work_id or "").strip() or None,
            severity="warning",
            body=clean_reason or None,
            payload_json=json.dumps(payload, sort_keys=True),
        )
    finally:
        conn.close()


def _emit_lifecycle_rejection(verb: str):

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                try:
                    bound = inspect.signature(func).bind_partial(*args, **kwargs)
                    params = bound.arguments
                    _post_mcp_lifecycle_rejected(
                        verb=verb,
                        reason=f"{type(exc).__name__}: {exc}",
                        work_id=params.get("work_id"),
                        actor=params.get("actor"),
                        session_id=params.get("session_id"),
                        db_path=params.get("db_path"),
                    )
                except Exception:
                    pass
                raise

        return wrapper

    return decorator


def _assigned_to(row: dict[str, Any], actor: str) -> bool:
    haystack = " ".join(
        str(row.get(key) or "")
        for key in ("assignee", "owner_session_actor", "claim_actor")
    ).lower()
    return actor in haystack


def _priority_rank(value: Any) -> float:

    if value is None or value == "":
        return 99.0
    if isinstance(value, bool):
        return 99.0
    try:
        return float(value)
    except (TypeError, ValueError):
        lookup = {"critical": 1.0, "p0": 1.0, "high": 1.0, "p1": 1.0,
                  "medium": 5.0, "p2": 5.0, "low": 9.0, "p3": 9.0}
        return lookup.get(str(value).strip().lower(), 99.0)


def _priority_sort_value(value: Any) -> float:

    return coord_db.priority_sort_value(_priority_rank(value))


def _status_rank(status: str) -> int:
    return {"running": -1, "queued": 0, "planned": 1, "blocked": 8}.get(status, 9)


def _trim_backlog_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: row.get(k) for k in _WORK_CONTEXT_KEYS if k in row}
    rid = _row_id(row)
    if rid:
        out["id"] = rid
    if row.get("done_signal") is not None:
        out["done_signal_exists"] = job_status.done_signal_exists(row.get("done_signal"), _HARNESS)
    return out


def _compact_work_candidate(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id", "title", "display", "status", "assignee", "assigned_by", "priority",
        "surface", "epic", "parent", "module", "lane", "sublane",
        "resource_class", "context_pack_ref", "section", "source",
        "next_step", "resume_when", "blocked_reason_class",
        "completion_requested_at", "done_signal_exists",
    )
    out = {key: row.get(key) for key in keys if row.get(key) not in (None, "")}
    rid = _row_id(row)
    if rid:
        out["id"] = rid
    return out


def _capped_ids(ids: list[str], *, limit: int = _PREFLIGHT_ID_LIST_LIMIT) -> tuple[list[str], int, bool]:
    all_ids = sorted(set(str(item) for item in ids if str(item or "").strip()))
    return all_ids[:limit], len(all_ids), len(all_ids) > limit


def _coord_context_enabled(db_path: str | None = None) -> bool:
    if db_path is not None:
        return True
    try:
        return _HARNESS.resolve() == Path(HARNESS_ROOT).resolve()
    except Exception:
        return False


def _coord_board_to_context_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": str(row.get("work_id") or ""),
        "title": row.get("title"),
        "display": row.get("display"),
        "status": row.get("status") or row.get("intent_state"),
        "assignee": row.get("assignee"),
        "assigned_by": row.get("assigned_by"),
        "priority": row.get("priority"),
        "surface": row.get("surface"),
        "epic": row.get("domain"),
        "parent": row.get("parent_id"),
        "module": row.get("module"),
        "lane": row.get("lane"),
        "sublane": row.get("sublane"),
        "resource_class": row.get("resource_class"),
        "done_signal": row.get("done_signal"),
        "acceptance_json": row.get("acceptance_json"),
        "context_pack_ref": row.get("context_pack_ref"),
        "note": row.get("note"),
        "next_step": row.get("next_step"),
        "resume_when": row.get("resume_when"),
        "resume_predicate_json": row.get("resume_predicate_json"),
        "continuation_ready_at": row.get("continuation_ready_at"),
        "blocked_reason_class": row.get("blocked_reason_class"),
        "completion_requested_at": row.get("completion_requested_at"),
        "updated_at": row.get("updated_at"),
        "created_at": row.get("created_at"),
        "owner_session_id": row.get("owner_session_id"),
        "owner_session_actor": row.get("owner_session_actor"),
        "claim_status": row.get("claim_status"),
        "claim_step": row.get("claim_step"),
        "subject_plane": row.get("subject_plane") or "unknown",
        "subject_plane_exact": bool(row.get("subject_plane")),
        "program_id": row.get("program_id"),
        "workstream_id": row.get("workstream_id"),
        "episode_id": row.get("episode_id"),
        "span_id": row.get("span_id"),
        "quarantined": bool(row.get("quarantined")),
        "quarantine_reasons": row.get("quarantine_reasons") or [],
        "query_rank": row.get("query_rank"),
        "query_rank_key": row.get("query_rank_key"),
        "query_core_build_sha256": row.get("query_core_build_sha256"),
        "source": "coord.db",
    }
    return {k: v for k, v in out.items() if v not in (None, "")}


def _coord_context_rows(db_path: str | None = None) -> list[dict[str, Any]]:
    if not _coord_context_enabled(db_path):
        return []
    if not harness_config.is_strict_deployment():
        conn = _get_read_conn(db_path)
        try:
            return [
                _coord_board_to_context_row(row)
                for row in coord_db.board_rows(conn)
            ]
        finally:
            conn.close()
    from coordharness.coord.exact_query_core import load_query_snapshot

    snapshot = load_query_snapshot(db_path)
    return [
        _coord_board_to_context_row(row)
        for row in snapshot.flat_ranked_rows(include_quarantine=True)
    ]


def _query_core_receipt_for_profile(db_path: str | None = None) -> dict[str, Any]:
    """Describe the read authority honestly for the selected deployment profile."""

    if harness_config.is_strict_deployment():
        from coordharness.coord.exact_query_core import load_query_snapshot

        return load_query_snapshot(db_path).receipt()
    return {
        "mode": "generic_coord_db",
        "deployment_profile": "generic",
        "lifecycle_authority": "coord.db",
        "exact_authority_enforced": False,
        "standalone_eligible": True,
    }


def _coord_board_row(conn, work_id: str, *, group_by: str = "module") -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM v_work_owner WHERE work_id=?", (work_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    # Mirror ``coord_db.board_rows`` exactly: a local pid probe can only answer
    # for a run recorded on THIS host. Probing a foreign run's pid asks the
    # wrong kernel -- the number names a process on another machine -- and a
    # miss would report 0, which ``derive_work_status`` reads as "not running"
    # because it prefers ``live_pid_count`` over ``live_run_count`` whenever the
    # key is present. That suppresses the lease/heartbeat fallback and makes a
    # healthy foreign run read as dead. So one unanswerable run makes the whole
    # work item's pid count unanswerable: leave the key UNSET rather than
    # setting it to a number built from a partial view.
    live_pid_count = 0
    pid_seen = False
    foreign_host_seen = False
    for pr in conn.execute(
        "SELECT pid, pid_started_at, host_id FROM runs"
        " WHERE state='live' AND work_id=? AND pid IS NOT NULL",
        (work_id,),
    ).fetchall():
        if not coord_db.pid_liveness_is_meaningful(pr["host_id"]):
            foreign_host_seen = True
            continue
        pid_seen = True
        if coord_db.pid_matches(pr["pid"], pr["pid_started_at"]):
            live_pid_count += 1
    if pid_seen and not foreign_host_seen:
        out["live_pid_count"] = live_pid_count
    at = coord_db.db_now(conn)
    out["effective_tier"] = coord_db.effective_review_tier_for_work(
        conn, work_id, row=out
    )
    out["operator_ok_validated"] = coord_db._has_valid_operator_ok_unlocked(
        conn, work_id
    )
    out["status"] = coord_db.derive_work_status(out, at)
    out["proof_state"] = coord_db.derive_proof_state(out, at)
    out["group"] = out.get(group_by) or "(ungrouped)"
    return out


def _resolve_lifecycle_claim_row(
    conn,
    *,
    verb: str,
    claim_id: str | None = None,
    work_id: str | None = None,
    actor: str | None = None,
    session_id: str | None = None,
    env: dict | None = None,
) -> tuple[Any, str, str]:
    """Resolve the claim a lifecycle verb will act on, and who is asking.

    Returns ``(row, caller_actor, caller_session_id)``. The caller identity is
    part of the return value on purpose: every caller forwards it to the
    ``coord_db`` mutator, so the database re-checks ownership against the
    *asking* session rather than against the row it just read. Handing the
    stored holder back down instead would make the database guard vacuous from
    this surface -- a check that can only ever agree with itself.
    """

    normalized_claim_id = str(claim_id or "").strip()
    if normalized_claim_id:
        row = conn.execute(
            "SELECT c.claim_id, c.work_id, c.session_id, c.status, s.actor, w.* FROM claims c "
            "LEFT JOIN agent_sessions s ON s.session_id=c.session_id "
            "LEFT JOIN work_items w ON w.work_id=c.work_id WHERE c.claim_id=?",
            (normalized_claim_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"{verb} requires an existing claim_id; no claim {normalized_claim_id!r}")
        if str(row["status"] or "").strip().lower() not in {"running", "paused", "blocked"}:
            raise ValueError(
                f"{verb} requires a held claim; claim_id {normalized_claim_id!r} "
                f"has status {row['status']!r}"
            )
        explicit_actor = str(actor or "").strip().lower()
        explicit_sid = str(session_id or "").strip()
        # Resolve who is asking BEFORE the volunteered-identity checks below.
        # Those only ever ran `if explicit_actor or explicit_sid`, so a call
        # carrying nothing but a claim_id was never compared to the holder at
        # all -- and a claim_id is not a secret. `claim` returns it, the board
        # prints it, and handoff payloads carry it. The work_id branch below has
        # always resolved a bound identity; this branch simply never did.
        holder_sid = str(row["session_id"] or "").strip()
        holder_actor = str(
            row["actor"] or coord_db.expected_actor_for_session_id(holder_sid) or ""
        ).strip().lower()
        try:
            _identity, claim_caller_actor, claim_caller_sid = _resolve_tool_identity(
                actor=actor,
                session_id=session_id,
                env=env,
            )[:3]
        except ValueError as exc:
            if explicit_actor or explicit_sid:
                raise
            raise ValueError(
                f"{verb} claim_id {normalized_claim_id!r} is held by "
                f"{holder_actor or '?'}/{holder_sid or '?'}, and this call named "
                f"no session of its own. A claim id is printed on the board and "
                f"carried in handoff payloads, so holding one proves nothing: {exc}"
            ) from exc
        coord_db.assert_claim_holder(
            conn,
            normalized_claim_id,
            action=verb,
            session_id=claim_caller_sid,
            actor=claim_caller_actor,
        )
        if explicit_actor or explicit_sid:
            if explicit_sid:
                expected_actor = coord_db.expected_actor_for_session_id(explicit_sid)
                if explicit_actor and expected_actor and expected_actor != explicit_actor:
                    raise ValueError(
                        f"session_id {explicit_sid!r} requires actor={expected_actor!r}, "
                        f"got actor={explicit_actor!r}"
                    )
                if row["session_id"] != explicit_sid:
                    raise ValueError(
                        f"{verb} claim_id {normalized_claim_id!r} session_id {explicit_sid!r} "
                        f"does not match stored claim session_id {row['session_id']!r}"
                    )
            stored_actor = str(
                row["actor"] or coord_db.expected_actor_for_session_id(row["session_id"]) or ""
            ).strip().lower()
            if explicit_actor and stored_actor and explicit_actor != stored_actor:
                raise ValueError(
                    f"{verb} claim_id {normalized_claim_id!r} actor {explicit_actor!r} "
                    f"does not match stored claim actor {stored_actor!r}"
                )
        return row, claim_caller_actor, claim_caller_sid

    normalized_work_id = str(work_id or "").strip()
    if not normalized_work_id:
        raise ValueError(f"{verb} requires claim_id or work_id + session_id")

    supplied_sid = str(session_id or "").strip()
    if supplied_sid:
        supplied_actor = (
            str(actor or "").strip().lower()
            or coord_db.expected_actor_for_session_id(supplied_sid)
            or ""
        )
        try:
            norm_sid = (
                normalize_session_id(supplied_actor, supplied_sid)
                if supplied_actor
                else supplied_sid
            )
        except Exception:
            norm_sid = supplied_sid
        supplied_row = conn.execute(
            "SELECT c.claim_id, c.work_id, c.session_id, c.status, s.actor, w.* FROM claims c "
            "LEFT JOIN agent_sessions s ON s.session_id=c.session_id "
            "LEFT JOIN work_items w ON w.work_id=c.work_id "
            "WHERE c.work_id=? AND c.session_id=? AND c.status IN ('running','paused','blocked') "
            "ORDER BY c.acquired_at DESC LIMIT 1",
            (normalized_work_id, norm_sid),
        ).fetchone()
        if supplied_row is None:
            related_claim_id = coord_db.held_claim_id_for_session_family(
                conn,
                normalized_work_id,
                norm_sid,
                actor=supplied_actor or None,
            )
            if related_claim_id:
                supplied_row = conn.execute(
                    "SELECT c.claim_id, c.work_id, c.session_id, c.status, s.actor, w.* FROM claims c "
                    "LEFT JOIN agent_sessions s ON s.session_id=c.session_id "
                    "LEFT JOIN work_items w ON w.work_id=c.work_id "
                    "WHERE c.claim_id=?",
                    (related_claim_id,),
                ).fetchone()
        if supplied_row is not None:
            stored_actor = str(
                supplied_row["actor"]
                or coord_db.expected_actor_for_session_id(supplied_row["session_id"])
                or ""
            ).strip().lower()
            if supplied_actor and stored_actor and supplied_actor != stored_actor:
                raise ValueError(
                    f"{verb} resolved session_id={supplied_sid!r} as actor={supplied_actor!r}, "
                    f"but stored claim actor is {stored_actor!r}"
                )
            return supplied_row, supplied_actor, norm_sid

    _identity, resolved_actor, resolved_sid, _label_fields = _resolve_tool_identity(
        actor=actor,
        session_id=session_id,
        env=env,
    )
    row = conn.execute(
        "SELECT c.claim_id, c.work_id, c.session_id, c.status, s.actor, w.* FROM claims c "
        "LEFT JOIN agent_sessions s ON s.session_id=c.session_id "
        "LEFT JOIN work_items w ON w.work_id=c.work_id "
        "WHERE c.work_id=? AND c.session_id=? AND c.status IN ('running','paused','blocked') "
        "ORDER BY c.acquired_at DESC LIMIT 1",
        (normalized_work_id, resolved_sid),
    ).fetchone()
    if row is None:
        related_claim_id = coord_db.held_claim_id_for_session_family(
            conn,
            normalized_work_id,
            resolved_sid,
            actor=resolved_actor,
        )
        if related_claim_id:
            row = conn.execute(
                "SELECT c.claim_id, c.work_id, c.session_id, c.status, s.actor, w.* FROM claims c "
                "LEFT JOIN agent_sessions s ON s.session_id=c.session_id "
                "LEFT JOIN work_items w ON w.work_id=c.work_id "
                "WHERE c.claim_id=?",
                (related_claim_id,),
            ).fetchone()
    if row is None:
        orphaned = conn.execute(
            "SELECT 1 FROM work_items w WHERE w.work_id=?"
            " AND lower(COALESCE(w.intent_state,''))='blocked'"
            " AND trim(COALESCE(w.blocked_reason_class,''))=''"
            " AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.work_id=w.work_id"
            " AND c.status IN ('running','paused','blocked')"
            " AND (c.expires_at IS NULL OR c.expires_at>?))",
            (normalized_work_id, coord_db.db_now(conn)),
        ).fetchone()
        if orphaned is not None:
            raise ValueError(
                f"{verb} cannot resolve orphaned blocked work "
                f"{normalized_work_id!r}; call recover_blocked to explicitly "
                "requeue the blocked/no-live-claim/null-reason state with a receipt"
            )
        raise ValueError(
            f"{verb} found no held claim for work_id={normalized_work_id!r} "
            f"session_id={resolved_sid!r}; pass claim_id or claim the work first"
        )
    if row["actor"] and str(row["actor"]).strip().lower() != resolved_actor:
        raise ValueError(
            f"{verb} resolved session_id={resolved_sid!r} as actor={resolved_actor!r}, "
            f"but stored claim actor is {row['actor']!r}"
        )
    return row, resolved_actor, resolved_sid


def _parse_event_json(
    row: dict[str, Any],
    *,
    include_raw: bool = False,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    out = dict(row)
    for field, default in (("refs_json", []), ("payload_json", {})):
        raw = out.get(field)
        try:
            out[field[:-5] if field.endswith("_json") else field] = json.loads(raw or json.dumps(default))
        except (TypeError, json.JSONDecodeError):
            out[field[:-5] if field.endswith("_json") else field] = default
        if not include_raw:
            out.pop(field, None)
    if conn is not None and out.get("work_id") and isinstance(out.get("payload"), dict):
        work = conn.execute(
            "SELECT title,note,acceptance_json FROM work_items WHERE work_id=?",
            (out["work_id"],),
        ).fetchone()
        out["payload"] = coord_db.expand_event_payload_same_as_row(
            out["payload"], dict(work) if work is not None else None
        )
    return out


def _compact_one_line(value: Any, *, limit: int) -> tuple[str, bool]:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text, False
    return text[: max(0, limit - 3)].rstrip() + "...", True


def _event_summary(
    row: dict[str, Any], *, conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    parsed = _parse_event_json(row, conn=conn)
    keys = (
        "event_id", "ts", "kind", "actor", "session_id", "to_selector", "work_id",
        "run_id", "thread_id", "severity", "verdict", "trust", "title",
    )
    out = {key: parsed.get(key) for key in keys if parsed.get(key) not in (None, "")}

    body = parsed.get("body")
    if body:
        preview, truncated = _compact_one_line(body, limit=_EVENT_BODY_PREVIEW_LIMIT)
        out["body_preview"] = preview
        out["body_truncated"] = truncated

    refs = parsed.get("refs")
    if isinstance(refs, list) and refs:
        out["refs"] = refs

    payload = parsed.get("payload")
    if isinstance(payload, dict) and payload:
        payload_keys = sorted(str(key) for key in payload.keys() if str(key) != "sla_s")
        out["payload_keys"] = payload_keys[:_EVENT_PAYLOAD_KEY_LIMIT]
        out["payload_keys_truncated"] = len(payload_keys) > _EVENT_PAYLOAD_KEY_LIMIT
        if payload.get("schema_version") is not None:
            out["payload_schema_version"] = payload.get("schema_version")
    return out


def _work_fields_from_backlog(row: dict[str, Any], session_id: str | None = None) -> dict[str, Any]:
    status = str(row.get("status") or "").lower()
    fields: dict[str, Any] = {
        "surface": row.get("surface") or "job",
    }
    mapping = {
        "parent": "parent_id",
        "module": "module",
        "lane": "lane",
        "sublane": "sublane",
        "display": "display",
        "assignee": "assignee",
        "assigned_by": "assigned_by",
        "done_signal": "done_signal",
        "tier": "tier",
        "kind": "kind",
        "resource_class": "resource_class",
        "token_budget": "token_budget",
        "context_pack_ref": "context_pack_ref",
        "note": "note",
    }
    for src, dst in mapping.items():
        if row.get(src) not in (None, ""):
            fields[dst] = row.get(src)
    title = row.get("title") or row.get("name") or row.get("display")
    if title:
        fields["title"] = str(title)
    try:
        from coordharness.coord.ingest import _resolve_grouping, _valid_domain

        module, domain, sublane = _resolve_grouping(_row_id(row) or "", row)
        if module and not fields.get("module"):
            fields["module"] = module
        if sublane and not fields.get("sublane"):
            fields["sublane"] = sublane
        if domain and not _valid_domain(fields.get("domain")):
            fields["domain"] = domain
    except Exception:
        pass
    if status in {"planned", "queued", "running", "blocked", "done", "failed", "archived"}:
        fields["intent_state"] = status
    priority = row.get("priority")
    if priority not in (None, ""):
        fields["priority"] = int(_priority_rank(priority))
    if row.get("acceptance_json") not in (None, ""):
        acceptance = row.get("acceptance_json")
        fields["acceptance_json"] = acceptance if isinstance(acceptance, str) else json.dumps(acceptance)
    elif row.get("acceptance") not in (None, ""):
        fields["acceptance_json"] = json.dumps({"acceptance": row.get("acceptance")})
    if session_id:
        fields["created_by_session_id"] = session_id
    if "title" not in fields:
        raise ValueError(f"canonical roadmap row {_row_id(row)!r} has no title/name/display")
    return fields


def _claim_quality_missing(work_id: str, row: dict[str, Any] | None, actor: str) -> list[str]:
    # One definition, two surfaces: see coord_db.claim_readiness. This surface
    # refuses by default and the CLI warns by default; COORD_CLAIM_STRICT is the
    # only knob that moves either.
    return coord_db.claim_readiness(work_id, row, actor=actor)


_DEP_TERMINAL_INTENT = {
    "done",
    "complete",
    "completed",
    "failed",
    "archived",
    "superseded",
    "cancelled",
    "canceled",
    "closed",
    "cleared",
}


def _parse_id_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    items: Any
    if isinstance(value, (list, tuple)):
        items = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = raw.split(",")
            items = parsed if isinstance(parsed, (list, tuple)) else [parsed]
        else:
            items = raw.split(",")
    else:
        items = [value]
    return [str(item).strip() for item in items if str(item).strip()]


def _normalize_depends_on(value: Any) -> str | None:
    ids = _parse_id_list(value)
    if not ids:
        return None
    return json.dumps(ids, ensure_ascii=True)


def _normalize_acceptance_json(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, str):
        text = value.strip()
        return json.dumps([text]) if text else None
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return json.dumps(items) if items else None
    return json.dumps([str(value).strip()])


def _born_complete_seed_fields(
    *,
    parent: str | None = None,
    module: str | None = None,
    sublane: str | None = None,
    note: str | None = None,
    display: str | None = None,
    title: str | None = None,
    acceptance: Any = None,
    depends_on: Any = None,
    done_signal: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for column, raw in (
        ("parent_id", parent),
        ("module", module),
        ("sublane", sublane),
        ("note", note),
        ("display", display),
        ("title", title),
        ("done_signal", done_signal),
    ):
        text = str(raw or "").strip()
        if text:
            out[column] = text
    acceptance_json = _normalize_acceptance_json(acceptance)
    if acceptance_json is not None:
        out["acceptance_json"] = acceptance_json
    depends = _normalize_depends_on(depends_on)
    if depends is not None:
        out["depends_on"] = depends
    return out


def _col_is_empty(value: Any) -> bool:
    if value in (None, ""):
        return True
    if isinstance(value, str) and value.strip() in ("", "[]", "{}"):
        return True
    return False


def _fill_if_empty_fields(
    existing_row: dict[str, Any], seed_fields: dict[str, Any]
) -> dict[str, Any]:
    return {
        column: value
        for column, value in seed_fields.items()
        if _col_is_empty(existing_row.get(column))
    }


def _unmet_dependencies(conn, depends_on_value: Any) -> list[str]:
    unmet: list[str] = []
    for dep_id in _parse_id_list(depends_on_value):
        try:
            row = conn.execute(
                "SELECT intent_state FROM work_items"
                " WHERE work_id=? AND archived_at IS NULL",
                (dep_id,),
            ).fetchone()
        except Exception:
            return unmet
        if row is None:
            continue
        state = str((row["intent_state"] if row["intent_state"] is not None else "") or "").strip().lower()
        if state not in _DEP_TERMINAL_INTENT:
            unmet.append(dep_id)
    return unmet


def _new_row_quality_warnings(row: dict[str, Any]) -> list[str]:
    try:
        from coordharness.coord.creation_lint import row_quality_missing_fields
    except Exception:
        return []
    try:
        return row_quality_missing_fields(dict(row), require_policy_id=False)
    except Exception:
        return []


def _atomization_warning(conn, row: dict[str, Any]) -> str | None:
    module = str(row.get("module") or "").strip()
    assignee = str(row.get("assignee") or "").strip()
    title = str(row.get("title") or "").strip()
    work_id = str(row.get("work_id") or "")
    if not module or not assignee or not title:
        return None
    try:
        from coordharness.coord.creation_lint import sibling_atomization_warning

        now = coord_db.db_now(conn)
        rows = conn.execute(
            "SELECT title FROM work_items"
            " WHERE module=? AND assignee=? AND work_id<>?"
            " AND archived_at IS NULL"
            " AND COALESCE(created_at, 0) >= ?"
            " AND LOWER(COALESCE(intent_state, '')) NOT IN"
            " ('done','failed','archived','superseded','cancelled','canceled','closed','cleared')",
            (module, assignee, work_id, now - 3600.0),
        ).fetchall()
    except Exception:
        return None
    return sibling_atomization_warning(title, [r["title"] for r in rows])


@_emit_lifecycle_rejection("claim")
def _tool_claim(
    work_id: str,
    step: str | None = None,
    tier: str | None = None,
    actor: str | None = None,
    session_id: str | None = None,
    db_path: str | None = None,
    env: dict | None = None,
    *,
    parent: str | None = None,
    module: str | None = None,
    sublane: str | None = None,
    note: str | None = None,
    display: str | None = None,
    title: str | None = None,
    acceptance: Any = None,
    depends_on: Any = None,
    done_signal: str | None = None,
    priority: Any = None,
    write_scopes: Any = None,
) -> dict[str, Any]:
    identity, resolved_actor, resolved_sid, label_fields = _resolve_tool_identity(
        actor=actor,
        session_id=session_id,
        env=env,
    )
    # Normalized before the claim is taken, so an ungrantable scope refuses the
    # call outright instead of leaving the caller holding a claim it never got
    # to declare against.
    normalized_scopes = (
        _normalize_write_scopes(write_scopes, action="claim_work write_scopes")
        if write_scopes
        else []
    )
    conn = _get_conn(db_path)
    try:
        coord_db.register_session(conn, resolved_sid, resolved_actor,
                                  runner_type=identity.get("runner_type"),
                                  **label_fields)
        seed_fields = _born_complete_seed_fields(
            parent=parent,
            module=module,
            sublane=sublane,
            note=note,
            display=display,
            title=title,
            acceptance=acceptance,
            depends_on=depends_on,
            done_signal=done_signal,
        )
        priority_value = None
        if priority is not None and str(priority).strip() != "":
            priority_value = int(_priority_rank(priority))
        pending_work_fields: dict[str, Any] = {}
        exists = conn.execute("SELECT 1 FROM work_items WHERE work_id=?", (work_id,)).fetchone()
        row_created = exists is None
        if exists is None:
            backlog = _read_json(_roadmap_path(), {"today_live_jobs": [], "items": [], "epics": []})
            try:
                _section, row = _find_backlog_row(backlog if isinstance(backlog, dict) else {}, work_id)
            except ValueError:
                if not seed_fields:
                    raise
                row = None
            if row is not None:
                work_fields = _work_fields_from_backlog(row, resolved_sid)
            else:
                work_fields = {"surface": "job", "assignee": resolved_actor}
                if resolved_sid:
                    work_fields["created_by_session_id"] = resolved_sid
            work_fields.update(seed_fields)
            if tier is not None:
                work_fields["tier"] = tier
            if priority_value is not None:
                work_fields["priority"] = priority_value
            from coordharness.coord.creation_lint import validate_creation_policy

            work_fields["tier"] = validate_creation_policy(
                work_id,
                work_fields,
                tier_down_authorized=coord_db.tier_down_authorized_for_fields(
                    conn, work_id, work_fields
                ),
            )
            work_columns = {
                str(column[1])
                for column in conn.execute("PRAGMA table_info(work_items)").fetchall()
            }
            if "authority_declaration_json" in work_columns:
                work_fields["authority_declaration_json"] = (
                    coord_db.new_work_quarantine_declaration(work_id)
                )
            pending_work_fields = work_fields
        else:
            existing_row = conn.execute(
                "SELECT * FROM work_items WHERE work_id=?", (work_id,)
            ).fetchone()
            fill_fields = _fill_if_empty_fields(
                dict(existing_row) if existing_row else {}, seed_fields
            )
            if tier is not None:
                fill_fields["tier"] = tier
            if priority_value is not None:
                fill_fields["priority"] = priority_value
            pending_work_fields = fill_fields
        current_work_row = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?", (work_id,)
        ).fetchone()
        proposed_work_row = dict(current_work_row) if current_work_row else {
            "work_id": work_id
        }
        proposed_work_row.update(pending_work_fields)
        missing = _claim_quality_missing(work_id, proposed_work_row, resolved_actor)
        readiness_warning: dict[str, Any] | None = None
        if missing:
            message = coord_db.claim_readiness_message(work_id, missing)
            enforcement = coord_db.claim_readiness_enforcement(
                default=coord_db.CLAIM_READINESS_REFUSE
            )
            if enforcement == coord_db.CLAIM_READINESS_REFUSE:
                raise coord_db.ClaimReadinessError(
                    work_id, missing, f"refusing {message}"
                )
            readiness_warning = {
                "code": "claim_not_ready",
                "message": message,
                "missing": list(missing),
                "enforcement": enforcement,
                "env": coord_db.CLAIM_STRICT_ENV,
            }
        policy = _mcp_policy(
            conn,
            action="claim",
            work_id=work_id,
            actor=resolved_actor,
            session_id=resolved_sid,
            payload={
                "row": proposed_work_row,
                "step": step,
                "run_event_category": "lifecycle",
            },
        )
        if policy.get("blocked"):
            raise ValueError(f"policy blocked claim {work_id}: {policy.get('block_reason')}")
        try:
            claim_id = coord_db.claim_work(
                conn,
                resolved_sid,
                work_id,
                step=step,
                lease_s=INTERACTIVE_LEASE_S,
                work_fields=pending_work_fields or None,
            )
        except sqlite3.IntegrityError:
            coord_db.post_claim_conflict(
                conn,
                work_id=work_id,
                requester_actor=resolved_actor,
                requester_session_id=resolved_sid,
                requester_step=step,
                verb="claim",
            )
            raise
        claim_row = conn.execute(
            "SELECT lease_token FROM claims WHERE claim_id=?", (claim_id,)
        ).fetchone()
        if claim_row is None or not str(claim_row["lease_token"] or ""):
            raise RuntimeError("new claim is missing its exact custody fence")
        run_event_id = _record_mcp_lifecycle_run_event(
            conn,
            work_id=work_id,
            session_id=resolved_sid,
            actor=resolved_actor,
            verb="claim",
            action="claim",
            status="applied",
            step=step,
            claim_id=claim_id,
        )
        _refresh_native_cockpit(conn)
        context_capsule = None
        try:
            context_capsule = coord_db.build_claim_context_capsule(conn, work_id)
        except Exception:
            context_capsule = None
        result: dict[str, Any] = {
            "claim_id": claim_id,
            "claim_fence": str(claim_row["lease_token"]),
            "work_id": work_id,
            "actor": resolved_actor,
            "session_id": resolved_sid,
            "status": "running",
            "policy": _compact_policy_response(policy),
            "run_event_id": run_event_id,
            "context_capsule": context_capsule,
        }
        if readiness_warning is not None:
            result["claim_readiness"] = readiness_warning
        if normalized_scopes:
            from coordharness.coord.work_contracts import declare_write_set

            declared = declare_write_set(
                conn, claim_id=claim_id, scopes=normalized_scopes
            )
            result["write_set"] = [
                {"kind": scope.kind, "value": scope.value} for scope in declared
            ]
            result["write_set_conflicts"] = _write_set_conflicts_for_claim(
                conn, claim_id
            )
        work_row = conn.execute(
            "SELECT * FROM work_items WHERE work_id=?", (work_id,)
        ).fetchone()
        row_dict = dict(work_row) if work_row else proposed_work_row
        if row_created:
            quality_warnings = _new_row_quality_warnings(row_dict)
            if quality_warnings:
                result["quality_warnings"] = quality_warnings
            atomization = _atomization_warning(conn, row_dict)
            if atomization:
                result.setdefault("quality_warnings", []).append(atomization)
        unmet = _unmet_dependencies(conn, row_dict.get("depends_on"))
        if unmet:
            result["unmet_dependencies"] = unmet
        return result
    finally:
        conn.close()


@_emit_lifecycle_rejection("correct_tier")
def _tool_correct_tier(
    work_id: str,
    *,
    expected_version: int,
    expected_tier: str,
    new_tier: str,
    reason: str,
    refs: list[str],
    operation_id: str,
    actor: str | None = None,
    session_id: str | None = None,
    db_path: str | None = None,
    env: dict | None = None,
) -> dict[str, Any]:

    _identity, resolved_actor, resolved_sid, _labels = _resolve_tool_identity(
        actor=actor, session_id=session_id, env=env
    )
    conn = _get_conn(db_path)
    try:
        return coord_db.correct_work_tier(
            conn,
            work_id=work_id,
            actor=resolved_actor,
            session_id=resolved_sid,
            expected_version=expected_version,
            expected_tier=expected_tier,
            new_tier=new_tier,
            reason=reason,
            refs=refs,
            operation_id=operation_id,
        )
    finally:
        conn.close()


@_emit_lifecycle_rejection("resume_parked")
def _tool_resume_parked(
    work_id: str,
    *,
    expected_version: int,
    reason: str,
    refs: list[str],
    operation_id: str,
    actor: str | None = None,
    session_id: str | None = None,
    db_path: str | None = None,
    env: dict | None = None,
) -> dict[str, Any]:

    _identity, resolved_actor, resolved_sid, _labels = _resolve_tool_identity(
        actor=actor, session_id=session_id, env=env
    )
    conn = _get_conn(db_path)
    try:
        return coord_db.resume_parked_work(
            conn,
            work_id=work_id,
            actor=resolved_actor,
            session_id=resolved_sid,
            expected_version=expected_version,
            reason=reason,
            refs=refs,
            operation_id=operation_id,
        )
    finally:
        conn.close()


@_emit_lifecycle_rejection("heartbeat")
def _tool_heartbeat(
    claim_id: str | None = None,
    work_id: str | None = None,
    actor: str | None = None,
    session_id: str | None = None,
    step: str | None = None,
    db_path: str | None = None,
    env: dict | None = None,
) -> dict[str, Any]:
    conn = _get_conn(db_path)
    try:
        row, caller_actor, caller_sid = _resolve_lifecycle_claim_row(
            conn,
            verb="heartbeat",
            claim_id=claim_id,
            work_id=work_id,
            actor=actor,
            session_id=session_id,
            env=env,
        )
        resolved_claim_id = row["claim_id"]
        policy = _mcp_policy(
            conn,
            action="heartbeat",
            work_id=row["work_id"] if row else "",
            session_id=row["session_id"] if row else None,
            payload={"step": step, "run_event_category": "lifecycle"},
        )
        if policy.get("blocked"):
            raise ValueError(f"policy blocked heartbeat {resolved_claim_id}: {policy.get('block_reason')}")
        coord_db.heartbeat_claim(
            conn,
            resolved_claim_id,
            lease_s=INTERACTIVE_LEASE_S,
            step=step,
            session_id=caller_sid,
            actor=caller_actor,
        )
        run_event_id = _record_mcp_lifecycle_run_event(
            conn,
            work_id=row["work_id"] if row else None,
            session_id=row["session_id"] if row else None,
            actor=row["actor"] if row else None,
            verb="heartbeat",
            action="heartbeat",
            status="applied",
            step=step,
            claim_id=resolved_claim_id,
        )
        _refresh_native_cockpit(conn)
        return {
            "claim_id": resolved_claim_id,
            "work_id": row["work_id"],
            "session_id": row["session_id"],
            "renewed": True,
            "policy": _compact_policy_response(policy),
            "run_event_id": run_event_id,
        }
    finally:
        conn.close()


def _tool_release(
    claim_id: str | None = None,
    work_id: str | None = None,
    actor: str | None = None,
    session_id: str | None = None,
    status: str = "released",
    reason: str | None = None,
    next_step: str | None = None,
    resume_when: str | None = None,
    resume_predicate: str | None = None,
    resume_manual: bool = False,
    db_path: str | None = None,
    env: dict | None = None,
    _verb: str = "release",
) -> dict[str, Any]:
    allowed = {"released", "unclaimed", "paused", "blocked"}
    if status not in allowed:
        raise ValueError(f"status must be one of {allowed}, got {status!r}")
    if status == "paused":
        next_step, resume_when = require_park_resume_contract(
            next_step=next_step,
            resume_when=resume_when,
        )
    canonical_resume_predicate = None
    if status in {"paused", "blocked"}:
        canonical_resume_predicate = normalize_resume_trigger_contract(
            resume_when=resume_when,
            resume_predicate=resume_predicate,
            resume_manual=resume_manual,
        )
    conn = _get_conn(db_path)
    try:
        row, caller_actor, caller_sid = _resolve_lifecycle_claim_row(
            conn,
            verb=_verb,
            claim_id=claim_id,
            work_id=work_id,
            actor=actor,
            session_id=session_id,
            env=env,
        )
        resolved_claim_id = row["claim_id"]
        policy = _mcp_policy(
            conn,
            action="release",
            work_id=row["work_id"] if row else "",
            session_id=row["session_id"] if row else None,
            payload={
                "status": status,
                "reason": reason,
                "next_step": next_step,
                "resume_when": resume_when,
                "resume_predicate_json": canonical_resume_predicate,
                "run_event_category": "lifecycle",
            },
        )
        if policy.get("blocked"):
            raise ValueError(f"policy blocked release {resolved_claim_id}: {policy.get('block_reason')}")
        coord_db.release_claim(
            conn,
            resolved_claim_id,
            status=status,
            reason=reason,
            next_step=next_step,
            resume_when=resume_when,
            resume_predicate_json=resume_predicate,
            resume_manual=resume_manual,
            session_id=caller_sid,
            actor=caller_actor,
        )
        run_event_id = _record_mcp_lifecycle_run_event(
            conn,
            work_id=row["work_id"] if row else None,
            session_id=row["session_id"] if row else None,
            actor=row["actor"] if row else None,
            verb=_verb,
            action=status,
            status="applied",
            step=reason,
            claim_id=resolved_claim_id,
        )
        return {
            "claim_id": resolved_claim_id,
            "work_id": row["work_id"],
            "session_id": row["session_id"],
            "status": status,
            "next_step": str(next_step or "").strip() or None,
            "resume_when": str(resume_when or "").strip() or None,
            "resume_predicate_json": canonical_resume_predicate,
            "policy": _compact_policy_response(policy),
            "run_event_id": run_event_id,
        }
    finally:
        conn.close()


def _tool_recover_blocked(
    *,
    work_id: str,
    note: str,
    actor: str | None = None,
    session_id: str | None = None,
    db_path: str | None = None,
    env: dict | None = None,
) -> dict[str, Any]:
    conn = _get_conn(db_path)
    try:
        _identity, resolved_actor, resolved_sid, _label_fields = _resolve_tool_identity(
            actor=actor,
            session_id=session_id,
            env=env,
        )
        result = coord_db.recover_orphaned_block(
            conn,
            work_id=work_id,
            actor=resolved_actor,
            session_id=resolved_sid,
            note=note,
        )
        _refresh_native_cockpit(conn)
        return {
            **result,
            "actor": resolved_actor,
            "session_id": resolved_sid,
            "status": "queued",
        }
    finally:
        conn.close()


def _tool_classify_blocked(
    *,
    work_id: str,
    reason_class: str,
    expected_version: int,
    expected_reason_class: str | None = None,
    note: str | None = None,
    actor: str | None = None,
    session_id: str | None = None,
    db_path: str | None = None,
    env: dict | None = None,
) -> dict[str, Any]:
    conn = _get_conn(db_path)
    try:
        _identity, resolved_actor, resolved_sid, _label_fields = _resolve_tool_identity(
            actor=actor,
            session_id=session_id,
            env=env,
        )
        result = coord_db.classify_blocked_work(
            conn,
            work_id=work_id,
            reason_class=reason_class,
            expected_version=expected_version,
            expected_reason_class=expected_reason_class,
            actor=resolved_actor,
            session_id=resolved_sid,
            note=note,
        )
        _refresh_native_cockpit(conn)
        return {
            **result,
            "actor": resolved_actor,
            "session_id": resolved_sid,
            "status": "blocked",
        }
    finally:
        conn.close()


def _tool_park(
    claim_id: str | None = None,
    work_id: str | None = None,
    step: str | None = None,
    next_step: str | None = None,
    resume_when: str | None = None,
    resume_predicate: str | None = None,
    resume_manual: bool = False,
    actor: str | None = None,
    session_id: str | None = None,
    db_path: str | None = None,
    env: dict | None = None,
) -> dict[str, Any]:

    return _tool_release(
        claim_id=claim_id,
        work_id=work_id,
        status="paused",
        reason=step,
        next_step=next_step,
        resume_when=resume_when,
        resume_predicate=resume_predicate,
        resume_manual=resume_manual,
        actor=actor,
        session_id=session_id,
        db_path=db_path,
        env=env,
        _verb="park",
    )


def _tool_block(
    claim_id: str | None = None,
    work_id: str | None = None,
    step: str | None = None,
    next_step: str | None = None,
    resume_when: str | None = None,
    resume_predicate: str | None = None,
    resume_manual: bool = False,
    actor: str | None = None,
    session_id: str | None = None,
    db_path: str | None = None,
    env: dict | None = None,
) -> dict[str, Any]:

    return _tool_release(
        claim_id=claim_id,
        work_id=work_id,
        status="blocked",
        reason=step,
        next_step=next_step,
        resume_when=resume_when,
        resume_predicate=resume_predicate,
        resume_manual=resume_manual,
        actor=actor,
        session_id=session_id,
        db_path=db_path,
        env=env,
        _verb="block",
    )


_COMPLETION_PROOF_MISSING_ERROR = (
    "complete_claim artifact proof does not exist or is incomplete"
)


def _complete_claim_with_proof_index_refresh(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    declared_proof: str,
    artifact_path: str | None,
    artifact_kind: str,
    caller_actor: str,
    caller_session_id: str,
) -> tuple[str, dict[str, object] | None]:
    complete_kwargs = {
        "artifact_path": artifact_path,
        "artifact_kind": artifact_kind,
        "receipt_source": "mcp_coord_server.complete",
        # The asking session, not the stored holder: the database re-checks
        # ownership, and it cannot do that against the row it just read.
        "session_id": caller_session_id,
        "actor": caller_actor,
    }
    try:
        return coord_db.complete_claim(conn, claim_id, **complete_kwargs), None
    except ValueError as exc:
        if (
            _COMPLETION_PROOF_MISSING_ERROR not in str(exc)
            or Path(declared_proof).suffix.lower() != ".md"
        ):
            raise

    refresh = job_status.refresh_completion_proof_index(_HARNESS)
    try:
        proof = coord_db.complete_claim(conn, claim_id, **complete_kwargs)
    except ValueError as retry_exc:
        if _COMPLETION_PROOF_MISSING_ERROR not in str(retry_exc):
            raise
        generation = str(refresh.get("generation_after") or "git-index-unavailable")
        started_iso = datetime.fromtimestamp(
            _SERVER_STARTED_AT, tz=timezone.utc
        ).isoformat()
        raise ValueError(
            f"{retry_exc}; "
            "proof_index_error=missing_incomplete_or_uncustodied_after_refresh; "
            f"proof_index_generation={generation}; "
            f"server_started_at={_SERVER_STARTED_AT:.9f}; "
            f"server_started_at_utc={started_iso}; "
            f"server_instance_id={_SERVER_INSTANCE_ID}; "
            "proof_index_refresh_attempted=true"
        ) from retry_exc
    return proof, refresh


@_emit_lifecycle_rejection("complete")
def _tool_complete(
    claim_id: str | None = None,
    work_id: str | None = None,
    actor: str | None = None,
    session_id: str | None = None,
    artifact_path: str | None = None,
    artifact_kind: str = "done_signal",
    db_path: str | None = None,
    env: dict | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    note_text = str(note or "").strip() or None
    conn = _get_conn(db_path)
    try:
        row, caller_actor, caller_sid = _resolve_lifecycle_claim_row(
            conn,
            verb="complete",
            claim_id=claim_id,
            work_id=work_id,
            actor=actor,
            session_id=session_id,
            env=env,
        )
        resolved_claim_id = row["claim_id"]
        policy = _mcp_policy(
            conn,
            action="done",
            work_id=row["work_id"] if row else "",
            session_id=row["session_id"] if row else None,
            payload={
                "row": dict(row) if row else {},
                "artifact_path": artifact_path,
                "artifact_kind": artifact_kind,
                "run_event_category": "lifecycle",
            },
        )
        if policy.get("blocked"):
            raise ValueError(f"policy blocked complete {resolved_claim_id}: {policy.get('block_reason')}")
        review_request = coord_db.request_completion_review_if_needed(
            conn,
            claim_id=resolved_claim_id,
        )
        if review_request is not None:
            if not review_request.get("replayed"):
                _refresh_native_cockpit(conn)
            return {
                **review_request,
                "session_id": row["session_id"],
                "completed": False,
                "policy": _compact_policy_response(policy),
            }
        declared_proof = str(row["done_signal"] or "").strip()
        proof, proof_index_refresh = _complete_claim_with_proof_index_refresh(
            conn,
            resolved_claim_id,
            declared_proof=declared_proof,
            artifact_path=artifact_path,
            artifact_kind=artifact_kind,
            caller_actor=caller_actor,
            caller_session_id=caller_sid,
        )
        canonical_receipt = conn.execute(
            "SELECT event_id FROM events WHERE idempotency_key=?",
            (f"coord-complete:{resolved_claim_id}",),
        ).fetchone()
        if canonical_receipt is None:
            raise RuntimeError("completed claim is missing its canonical atomic receipt")
        canonical_event_id = int(canonical_receipt["event_id"])
        run_event_id = _record_mcp_lifecycle_run_event(
            conn,
            work_id=row["work_id"] if row else None,
            session_id=row["session_id"] if row else None,
            actor=row["actor"] if row else None,
            verb="complete",
            action="done",
            status="applied",
            step=artifact_path,
            claim_id=resolved_claim_id,
            metadata={"note": note_text} if note_text else None,
        )
        completion = {
            "claim_id": resolved_claim_id,
            "work_id": row["work_id"],
            "session_id": row["session_id"],
            "status": "completed",
            "artifact_path": proof,
            "canonical_event_id": canonical_event_id,
            "policy": _compact_policy_response(policy),
            "run_event_id": run_event_id,
        }
        if proof_index_refresh is not None:
            completion["proof_index_refresh"] = proof_index_refresh
        if note_text:
            completion["note"] = note_text
            try:
                proposal_path = coord_db.write_done_memory_proposal(
                    row["work_id"],
                    note_text,
                    refs=[artifact_path] if artifact_path else [],
                    actor=row["actor"] if row else None,
                )
                if proposal_path:
                    completion["memory_proposal"] = proposal_path
            except Exception:
                pass
        return completion
    finally:
        conn.close()


def _tool_board(
    group_by: str = "module",
    limit: int = _DEFAULT_BOARD_LIMIT,
    full: bool = False,
    status: str | None = None,
    compact: bool = True,
    db_path: str | None = None,
) -> dict[str, Any]:
    if harness_config.is_strict_deployment():
        from coordharness.coord.exact_query_core import load_query_snapshot

        snapshot = load_query_snapshot(db_path)
        raw_rows = snapshot.flat_ranked_rows(include_quarantine=True)
        query_receipt = snapshot.receipt()
        exact_mode = query_receipt.get("mode") != "legacy_noncanonical_fixture"
    else:
        conn = _get_read_conn(db_path)
        try:
            raw_rows = coord_db.board_rows(conn, group_by=group_by)
        finally:
            conn.close()
        query_receipt = _query_core_receipt_for_profile(db_path)
        exact_mode = False
    try:
        total_count = len(raw_rows)
        eligible = list(raw_rows)
        requested_status = str(status or "").strip().lower()
        if requested_status == "open":
            open_statuses = {"running", "blocked", "attention", "queued", "planned"}
            eligible = [
                row
                for row in eligible
                if str(row.get("status") or "").strip().lower() in open_statuses
            ]
        elif requested_status and requested_status not in {"all", "current"}:
            eligible = [
                row
                for row in eligible
                if str(row.get("status") or "").strip().lower() == requested_status
            ]
        if requested_status == "all":
            eligible.sort(key=coord_db.board_recent_rank)
            lens = "recent"
        else:
            eligible.sort(key=lambda row: tuple(row.get("query_rank_key") or ()))
            lens = "current"
        eligible_count = len(eligible)
        if full:
            raise ValueError(
                "board(full=True) is disabled: MCP reads must not write exports. "
                "Use the CLI/admin board export for bulk history, or use bounded "
                "work search/context expansion for exact older context."
            )
        requested_limit = max(0, int(limit))
        inline_limit = min(requested_limit, _MAX_BOARD_INLINE_LIMIT)
        selected = eligible[:inline_limit]
        if compact:
            from coordharness.coord.board_context import compact_row

            returned = []
            for row in selected:
                card = compact_row(row)
                if not exact_mode:
                    card.pop("plane", None)
                card["work_id"] = str(row.get("work_id") or "")
                card["group"] = row.get(group_by) or "(ungrouped)"
                returned.append(card)
        else:
            returned = selected
        response = {
            "rows": returned,
            "count": len(returned),
            "returned": len(returned),
            "total_count": total_count,
            "eligible_count": eligible_count,
            "truncated": len(returned) < eligible_count,
            "full": bool(full),
            "limit": None if full else inline_limit,
            "requested_limit": None if full else requested_limit,
            "max_inline_limit": _MAX_BOARD_INLINE_LIMIT,
            "lens": lens,
            "group_by": group_by,
            "status_filter": requested_status or None,
            "compact": bool(compact),
            "classification_semantics": (
                "exact_authority_heads_unknown_quarantined"
                if exact_mode
                else (
                    "generic_coord_db_derived_read_lens"
                    if not harness_config.is_strict_deployment()
                    else "legacy_derived_read_lens_not_r4_authority"
                )
            ),
            "subject_plane_filter_applied": exact_mode,
            "query_core": query_receipt,
        }
        _measure_knowledge_response(response)
        return response
    finally:
        pass


def _normalize_write_scopes(raw_scopes: Any, *, action: str) -> list[tuple[str, str]]:
    """Normalize caller-supplied write scopes, refusing the ungrantable ones.

    Accepts the two shapes an MCP client can express naturally: a list of
    ``{"kind": ..., "value": ...}`` objects, and a list of ``"kind=value"``
    strings where a bare value means ``path``. The kind prefix is only honoured
    when it names a kind this module knows, because a path may contain ``=``.

    Normalization runs before anything is written, so an ungrantable scope
    refuses the whole call rather than leaving a partial declaration behind.
    """
    from coordharness.coord.work_contracts import SCOPE_KINDS, normalize_scope

    if isinstance(raw_scopes, (str, Mapping)):
        raw_scopes = [raw_scopes]
    scopes: list[tuple[str, str]] = []
    for entry in list(raw_scopes or []):
        if isinstance(entry, Mapping):
            kind = str(entry.get("kind") or "path")
            value = str(entry.get("value") or "")
        else:
            raw = str(entry or "").strip()
            head, sep, tail = raw.partition("=")
            if sep and head.strip().lower() in SCOPE_KINDS:
                kind, value = head.strip().lower(), tail
            else:
                kind, value = "path", raw
        if not str(value).strip():
            continue
        scopes.append(normalize_scope(kind, value).as_tuple())
    if not scopes:
        raise ValueError(
            f"{action} requires at least one write scope, for example "
            '"src/billing/" or {"kind": "table", "value": "orders"}'
        )
    if len(scopes) > _MAX_WRITE_SCOPES:
        raise ValueError(
            f"{action} write scopes are bounded to {_MAX_WRITE_SCOPES} entries; "
            "declare a prefix rather than enumerating files"
        )
    return scopes


def _write_set_conflicts_for_claim(conn, claim_id: str) -> dict[str, Any]:
    """The overlap findings that name this claim, as plain data.

    Advisory. Declaring a write set reports who else is in the same part of the
    tree; it does not refuse the claim, and it is not a lock. Deciding what to
    do about an overlap is the agents' call.
    """
    from coordharness.coord.work_contracts import write_set_overlaps

    report = write_set_overlaps(conn)
    mine = [
        finding
        for finding in report.findings
        if claim_id in (finding.claim_a, finding.claim_b)
    ]
    return {
        "count": len(mine),
        "findings": [finding.describe() for finding in mine],
        "scanned_claims": report.scanned_claims,
    }


def _tool_declare_write_set(
    claim_id: str,
    scopes: Any,
    actor: str | None = None,
    session_id: str | None = None,
    db_path: str | None = None,
    env: dict | None = None,
) -> dict[str, Any]:
    """Declare which scopes a held claim intends to write.

    The board coordinates rows, not files: two valid claims on two different
    work items can edit the same module and neither one hears about it until a
    merge conflict. A declared write set is what makes that answerable in
    advance, and until this tool existed the copy-paste agent prompt in
    docs/agent-protocol.md instructed agents to declare something no client
    could declare.
    """
    from coordharness.coord.work_contracts import declare_write_set

    clean_claim = str(claim_id or "").strip()
    if not clean_claim:
        raise ValueError("declare_write_set requires the claim_id to declare against")
    normalized = _normalize_write_scopes(scopes, action="declare_write_set")
    identity, resolved_actor, resolved_sid, label_fields = _resolve_tool_identity(
        actor=actor,
        session_id=session_id,
        env=env,
    )
    conn = _get_conn(db_path)
    try:
        coord_db.register_session(
            conn,
            resolved_sid,
            resolved_actor,
            runner_type=identity.get("runner_type"),
            **label_fields,
        )
        row = conn.execute(
            "SELECT work_id, session_id, status FROM claims WHERE claim_id=?",
            (clean_claim,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown claim_id {clean_claim!r}")
        # A write set is a statement about what THIS session is about to edit,
        # so it is refused on another session's claim. Without the check one
        # agent could declare scopes on a peer's claim and the overlap report
        # would name a collision neither of them intends.
        holder = str(row["session_id"] or "")
        if holder != resolved_sid:
            raise ValueError(
                f"claim {clean_claim!r} is held by session {holder!r}, not "
                f"{resolved_sid!r}; declare a write set on your own claim"
            )
        declared = declare_write_set(conn, claim_id=clean_claim, scopes=normalized)
        return {
            "verb": "declare_write_set",
            "claim_id": clean_claim,
            "work_id": str(row["work_id"]),
            "actor": resolved_actor,
            "session_id": resolved_sid,
            "write_set": [
                {"kind": scope.kind, "value": scope.value} for scope in declared
            ],
            "write_set_conflicts": _write_set_conflicts_for_claim(conn, clean_claim),
            "advisory": True,
            "lifecycle_mutation": False,
        }
    finally:
        conn.close()


def _tool_write_set_conflicts(
    include_expired: bool = False,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Which currently-held claims declare overlapping write scopes.

    Read-only, and opened read-only: asking who collides must not be able to
    change who collides. Claims that declared nothing are reported by id under
    ``undeclared_claims`` rather than counted as clean -- an undeclared claim is
    unknown, not safe.
    """
    from coordharness.coord.work_contracts import write_set_overlaps

    conn = _get_read_conn(db_path)
    try:
        report = write_set_overlaps(conn, include_expired=bool(include_expired))
    finally:
        conn.close()
    payload = report.as_dict()
    payload["include_expired"] = bool(include_expired)
    payload["advisory"] = True
    payload["read_only"] = True
    return payload


def _parse_route_budgets(budgets: Any) -> dict[str, int]:
    """Accept the two shapes a client can express a budget in, and no others.

    ``{"claude": 5_000_000}`` and ``["claude=5000000"]`` mean the same thing.
    A budget that is not a positive whole number of tokens is refused here
    rather than passed down: ``advise`` would raise on it anyway, and the
    message naming the caller's own argument is the useful one.
    """
    if not budgets:
        return {}
    pairs: list[tuple[str, Any]] = []
    if isinstance(budgets, Mapping):
        pairs = list(budgets.items())
    else:
        for entry in list(budgets):
            if isinstance(entry, Mapping):
                pairs.extend(entry.items())
                continue
            name, _, raw = str(entry).partition("=")
            pairs.append((name, raw))
    parsed: dict[str, int] = {}
    for raw_name, raw_value in pairs:
        name = str(raw_name or "").strip()
        text_value = str(raw_value).strip()
        if not name or not text_value.isdigit():
            raise ValueError(
                f"route budgets take PROVIDER=TOKENS with a positive whole "
                f"number of tokens, got {raw_name!r}={raw_value!r}"
            )
        parsed[name] = int(text_value)
    return parsed


def _tool_route(
    usage_db: str,
    budgets: Any = None,
    days: int = 7,
    require_complete: bool = False,
) -> dict[str, Any]:
    """Which provider has headroom, on measured usage. Advice, never an action.

    This is the MCP face of ``coord route``, and it is deliberately the same
    function underneath: an agent asking which lane has room had to ask the
    operator to shell out for it, which is the one question a coordination
    server should be able to answer itself.

    Read-only in the strict sense. It writes no board row, records no usage, and
    refuses a ``usage_db`` that does not exist rather than creating one --
    ``UsageLedger`` initializes an empty ledger at any path it is handed, so a
    typo would otherwise mint a second, empty accounting store and then route
    off it.

    The coverage refusal is passed through, not re-derived. ``summarize_rows``
    records an absent ``coverage_state`` as ``"unknown"`` and treats anything
    that is not ``"complete"`` as incomplete; ``ProviderUsage.complete`` then
    demands every expected day present AND every observation complete. This tool
    reports ``coverage_state: "unknown"`` whenever that strictness has not been
    met for every provider it advised on, so a caller reading one field still
    gets the refusal rather than the headline number.
    """
    from coordharness.usage import routing
    from coordharness.usage.ledger import UsageLedger

    path = Path(str(usage_db or "").strip()).expanduser()
    if not str(usage_db or "").strip():
        raise ValueError("route requires usage_db: the path of an existing usage ledger")
    if not path.exists():
        raise ValueError(
            f"route usage_db {str(path)!r} does not exist; this tool only reads a "
            "ledger and will not create one"
        )
    parsed_budgets = _parse_route_budgets(budgets)
    ledger = UsageLedger(path)
    try:
        advice = routing.advise_from_ledger(
            ledger,
            parsed_budgets,
            days=int(days),
            require_complete=bool(require_complete),
        )
    finally:
        ledger.close()
    payload = advice.as_dict()
    # "complete" only when there is something to be complete ABOUT and every
    # provider met the strict test. An empty advice -- no rows, or no declared
    # budget -- is unknown, because absence of evidence is not evidence of full
    # coverage. This mirrors ProviderUsage.complete, which returns False for an
    # empty coverage_states map for exactly the same reason.
    verdicts = list(advice.verdicts)
    payload["coverage_state"] = (
        "complete"
        if verdicts and all(verdict.usage.complete for verdict in verdicts)
        else "unknown"
    )
    payload["usage_db"] = str(path)
    payload["days"] = int(days)
    payload["require_complete"] = bool(require_complete)
    payload["budgets"] = dict(sorted(parsed_budgets.items()))
    payload["read_only"] = True
    payload["advisory"] = True
    payload["lifecycle_mutation"] = False
    payload["rendered"] = routing.render(advice)
    return payload


def _tool_runs(
    work_id: str | None = None,
    session_id: str | None = None,
    state: str | None = None,
    limit: int = 100,
    db_path: str | None = None,
) -> dict[str, Any]:
    conn = _get_read_conn(db_path)
    try:
        payload = coord_db.runs_read_model(
            conn,
            work_id=work_id,
            session_id=session_id,
            state=state,
            limit=limit,
        )
        payload["filters"] = {
            "work_id": work_id,
            "session_id": session_id,
            "state": state,
            "limit": limit,
        }
        return payload
    finally:
        conn.close()


def _tool_inbox(
    actor: str | None = None,
    session_id: str | None = None,
    limit: int = 20,
    advance: bool = False,
    backlog: bool = False,
    directed_only: bool = False,
    db_path: str | None = None,
    env: dict | None = None,
) -> dict[str, Any]:
    """The inbox as agents actually read it, so it must answer their question.

    This surface reads newest-first for the same reason the CLI does: at any
    limit smaller than the backlog, the oldest-first window is exactly the one
    that excludes the message that just arrived, and an agent that reads five of
    fifty old events concludes nothing came in. ``backlog=True`` restores queue
    order for a caller that means to drain in the order things were written.

    The acknowledgement is computed as the maximum event in the window that was
    actually returned, never the last element of the list. Those are the same
    row only under queue order; flipping the reading makes ``msgs[-1]`` the
    OLDEST event shown, which would ack behind what the caller was just told.
    """
    identity = _resolve(env)
    resolved_actor = actor or identity["actor"]
    resolved_sid = session_id or identity.get("session_id") or ""
    conn = _get_conn(db_path) if advance else _get_read_conn(db_path)
    try:
        msgs = coord_db.read_inbox(
            conn, recipient_actor=resolved_actor, session_id=resolved_sid,
            limit=limit, newest_first=not backlog, directed_only=directed_only,
        )
        # Counted before any acknowledgement, so these describe the queue the
        # caller was handed rather than the one its own read just drained.
        unread = coord_db.unread_inbox_counts(
            conn, recipient_actor=resolved_actor, session_id=resolved_sid
        )
        scope_unread = unread["directed"] if directed_only else unread["total"]
        acked_through: int | None = None
        skipped_by_ack = 0
        if advance and msgs:
            acked_through = max(int(m["event_id"]) for m in msgs)
            # The cursor is a watermark, not a per-message read flag: acking the
            # newest event shown also marks everything older as seen. Under the
            # newest-first reading that can be a whole backlog, so say how many
            # unread events this ack passed over without ever showing them.
            skipped_by_ack = max(
                0,
                _unread_at_or_below(
                    conn,
                    recipient_actor=resolved_actor,
                    session_id=resolved_sid,
                    event_id=acked_through,
                )
                - len(msgs),
            )
            coord_db.advance_cursor(
                conn, resolved_actor, acked_through, session_id=resolved_sid
            )
        return {
            "actor": resolved_actor,
            "messages": msgs,
            "count": len(msgs),
            "unread_total": unread["total"],
            "directed_unread": unread["directed"],
            "broadcast_unread": unread["broadcast"],
            "not_shown": max(0, scope_unread - len(msgs)),
            "order": "backlog" if backlog else "newest_first",
            "scope": "directed" if directed_only else "all",
            "acked_through": acked_through,
            "skipped_by_ack": skipped_by_ack,
        }
    finally:
        conn.close()


def _unread_at_or_below(
    conn, *, recipient_actor: str, session_id: str, event_id: int
) -> int:
    """Events past the cursor and no newer than ``event_id``.

    Used to report what an acknowledgement swept up silently; it counts the same
    two legs the inbox reads, so a broadcast skipped by the ack is disclosed even
    when the read itself asked for directed messages only.
    """
    cursor = coord_db.get_cursor(conn, recipient_actor, session_id)
    selectors = [f"actor:{recipient_actor}"]
    if session_id:
        selectors.append(f"session:{session_id}")
    placeholders = ",".join("?" * len(selectors))
    row = conn.execute(
        f"SELECT COUNT(*) FROM events"
        f" WHERE (to_selector IN ({placeholders}) OR to_selector IS NULL)"
        f" AND event_id > ? AND event_id <= ?",
        (*selectors, cursor, int(event_id)),
    ).fetchone()
    return int(row[0] if row else 0)


def _typed_handoff_tool_response(
    result: dict[str, Any],
    *,
    refs_count: int,
    constraints_count: int,
    target_intent: str,
    policy: dict[str, Any],
    writer_status: str,
) -> dict[str, Any]:
    response = {
        **coord_db.compact_existing_work_handoff_result(result),
        "refs_count": refs_count,
        "constraints_count": constraints_count,
        "target_intent": target_intent,
        "policy": policy,
        "writer_status": writer_status,
    }
    for _ in range(4):
        response_bytes = len(
            json.dumps(response, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        )
        if response.get("response_bytes") == response_bytes:
            break
        response["response_bytes"] = response_bytes
    if int(response.get("response_bytes") or 0) > 12_000:
        raise RuntimeError("typed handoff bounded response invariant exceeded 12000 bytes")
    return response


def _validate_typed_handoff_transport_fence(
    *,
    expected_protocol_epoch: str,
    expected_server_build_sha256: str,
    expected_server_instance_id: str,
    expected_client_profile_id: str,
    env: dict | None,
) -> dict[str, Any]:

    profile = deferred_tools.client_profile_attestation(env)
    observed = {
        "protocol_epoch": _MCP_PROTOCOL_EPOCH,
        "server_build_sha256": _SERVER_BUILD_SHA256,
        "server_instance_id": _SERVER_INSTANCE_ID,
        "client_profile_id": profile["profile_id"],
    }
    expected = {
        "protocol_epoch": str(expected_protocol_epoch or "").strip(),
        "server_build_sha256": str(expected_server_build_sha256 or "").strip(),
        "server_instance_id": str(expected_server_instance_id or "").strip(),
        "client_profile_id": str(expected_client_profile_id or "").strip(),
    }
    missing = [name for name, value in expected.items() if not value]
    if missing:
        raise ValueError(f"typed handoff transport fence requires non-empty fields: {missing}")
    mismatches = [
        name for name in expected
        if expected[name] != observed[name]
    ]
    if mismatches:
        raise ValueError(
            "typed handoff transport fence mismatch: "
            + ",".join(sorted(mismatches))
        )
    return profile


def _tool_handoff_existing(
    *,
    work_id: str,
    task: str,
    why: str,
    acceptance: str,
    owner_lane: str,
    operation_id: str,
    expected_version: int,
    expected_assignee: str,
    expected_head_event_ids: list[int],
    expected_protocol_epoch: str,
    expected_server_build_sha256: str,
    expected_server_instance_id: str,
    expected_client_profile_id: str,
    refs: list[str] | None = None,
    constraints: list[str] | None = None,
    target_intent: str = "queued",
    actor: str | None = None,
    session_id: str | None = None,
    db_path: str | None = None,
    env: dict | None = None,
    _allow_unattested_dark_test: bool = False,
) -> dict[str, Any]:

    profile = _validate_typed_handoff_transport_fence(
        expected_protocol_epoch=expected_protocol_epoch,
        expected_server_build_sha256=expected_server_build_sha256,
        expected_server_instance_id=expected_server_instance_id,
        expected_client_profile_id=expected_client_profile_id,
        env=env,
    )
    if not profile["attested"] and not _allow_unattested_dark_test:
        raise ValueError(
            "typed handoff requires an attested client profile: "
            f"state={profile['state']}"
        )

    clean_work_id = str(work_id or "").strip()
    clean_task = str(task or "").strip()
    clean_why = str(why or "").strip()
    clean_acceptance = str(acceptance or "").strip()
    clean_owner = str(owner_lane or "").strip().lower()
    clean_intent = str(target_intent or "").strip().lower()
    clean_refs = [str(ref).strip() for ref in (refs or []) if str(ref).strip()]
    clean_constraints = [
        str(value).strip() for value in (constraints or []) if str(value).strip()
    ]
    required = {
        "work_id": clean_work_id,
        "task": clean_task,
        "why": clean_why,
        "acceptance": clean_acceptance,
        "operation_id": str(operation_id or "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(f"typed handoff requires non-empty fields: {missing}")
    byte_limits = {
        "task": (clean_task, 1_000),
        "why": (clean_why, 2_048),
        "acceptance": (clean_acceptance, 4_096),
    }
    for name, (value, cap) in byte_limits.items():
        if len(value.encode("utf-8")) > cap:
            raise ValueError(f"typed handoff {name} exceeds {cap} UTF-8 bytes")
    if clean_owner not in _lane_set():
        raise ValueError(f"typed handoff owner_lane must be {_lanes_display()}")
    if clean_intent not in {"queued", "blocked"}:
        raise ValueError("typed handoff target_intent must be queued|blocked")
    if len(clean_refs) > 32 or any(len(ref.encode("utf-8")) > 2_048 for ref in clean_refs):
        raise ValueError("typed handoff refs are bounded to 32 pointers of 2048 bytes")
    if len(clean_constraints) > 16 or any(
        len(value.encode("utf-8")) > 512 for value in clean_constraints
    ):
        raise ValueError("typed handoff constraints are bounded to 16 entries of 512 bytes")

    resolved_actor, resolved_sid = _resolve_process_bound_identity(
        actor=actor,
        session_id=session_id,
        env=env,
        action="typed handoff",
    )
    if profile["attested"] and profile["expected_actor"] != resolved_actor:
        raise ValueError(
            "typed handoff client profile actor mismatch: "
            f"profile={profile['profile_id']} expected={profile['expected_actor']} "
            f"observed={resolved_actor}"
        )
    writer_status = (
        "graduated_profile_attested"
        if "handoff_existing" in _server_tool_catalog(env=env)["promoted"]
        else "dark_deferred_not_graduated"
    )
    dao_fields = {
        "work_id": clean_work_id,
        "actor": resolved_actor,
        "session_id": resolved_sid,
        "owner_lane": clean_owner,
        "target_intent": clean_intent,
        "task": clean_task,
        "why": clean_why,
        "acceptance": clean_acceptance,
        "constraints": clean_constraints,
        "refs": clean_refs,
        "operation_id": str(operation_id).strip(),
        "expected_version": expected_version,
        "expected_assignee": expected_assignee,
        "expected_head_event_ids": expected_head_event_ids,
    }
    conn = _get_conn(db_path)
    try:
        replay = coord_db.lookup_existing_work_handoff_replay(conn, **dao_fields)
        if replay is not None:
            return _typed_handoff_tool_response(
                replay,
                refs_count=len(clean_refs),
                constraints_count=len(clean_constraints),
                target_intent=clean_intent,
                policy={
                    "replay": True,
                    "mutable_policy_bypassed": True,
                    "reason": "immutable lost-response receipt",
                },
                writer_status=writer_status,
            )
        policy = _mcp_policy(
            conn,
            action="handoff",
            work_id=clean_work_id,
            actor=resolved_actor,
            session_id=resolved_sid,
            payload={
                "target_intent": clean_intent,
                "output_bytes": 6_000,
                "request_bytes_cap": 12_000,
            },
        )
        if policy.get("blocked"):
            raise ValueError(
                f"policy blocked typed handoff {clean_work_id}: {policy.get('block_reason')}"
            )
        result = coord_db.post_existing_work_handoff(conn, **dao_fields)
        if not result.get("replayed"):
            _refresh_native_cockpit(conn)
        return _typed_handoff_tool_response(
            result,
            refs_count=len(clean_refs),
            constraints_count=len(clean_constraints),
            target_intent=clean_intent,
            policy=_compact_policy_response(policy),
            writer_status=writer_status,
        )
    finally:
        conn.close()


@_emit_lifecycle_rejection("request_audit")
def _tool_request_audit(
    work_id: str,
    task: str,
    why: str,
    refs: list[str] | None = None,
    acceptance: str | None = None,
    request_kind: str = "standard",
    actor: str | None = None,
    session_id: str | None = None,
    db_path: str | None = None,
    env: dict | None = None,
) -> dict[str, Any]:
    clean_task = str(task or "").strip()
    if not clean_task:
        raise ValueError("request_audit requires a non-empty task")
    clean_why = str(why or "").strip()
    if not clean_why:
        raise ValueError("request_audit requires a non-empty why")
    clean_refs = [str(ref).strip() for ref in (refs or []) if str(ref).strip()]
    if len(clean_refs) > 32:
        raise ValueError("request_audit refs are bounded to 32 pointers")
    clean_request_kind = str(request_kind or "standard").strip().lower()
    if clean_request_kind not in {"standard", "flag_repair"}:
        raise ValueError("request_audit request_kind must be standard|flag_repair")
    if clean_request_kind == "flag_repair":
        resolved_actor, resolved_sid = _resolve_process_bound_identity(
            actor=actor,
            session_id=session_id,
            env=env,
            action="flag_repair request_audit",
        )
        identity: dict[str, Any] = _resolve(env)
        label_fields: dict[str, Any] = {}
    else:
        identity, resolved_actor, resolved_sid, label_fields = _resolve_tool_identity(
            actor=actor,
            session_id=session_id,
            env=env,
        )
    opposite_lane = _counterpart_lane(resolved_actor)
    if opposite_lane is None:
        raise ValueError(
            "request_audit requires a second configured lane; COORD_LANES names "
            f"only {_lanes_display()}"
        )
    conn = _get_conn(db_path)
    try:
        if clean_request_kind == "standard":
            coord_db.register_session(
                conn,
                resolved_sid,
                resolved_actor,
                runner_type=identity.get("runner_type"),
                **label_fields,
            )
        row = conn.execute(
            "SELECT assignee FROM work_items WHERE work_id=?", (work_id,)
        ).fetchone()
        if row is None:
            raise ValueError(
                f"request_audit work_id not found: {work_id}; audit requests are "
                "events on an existing author row"
            )
        assignee = str(row["assignee"] or "").strip().lower()
        if assignee in _lane_set() and assignee == opposite_lane:
            raise ValueError(
                f"request_audit self-target refused: work {work_id} is assigned to "
                f"{assignee}, the same lane this request targets ({opposite_lane}); a "
                "lane cannot be asked to audit its own work — request cross-eyes on "
                "work your lane owns"
            )
        payload = {
            "task": clean_task,
            "why": clean_why,
            "acceptance": str(acceptance or "").strip() or None,
            "schema_version": 1,
            "source": "mcp_coord_server.request_audit",
            "event_only": True,
        }
        repair_result: dict[str, Any] = {}
        if clean_request_kind == "flag_repair":
            repair_result = coord_db.post_flag_repair_audit_request(
                conn,
                work_id=work_id,
                actor=resolved_actor,
                session_id=resolved_sid,
                to_selector=f"actor:{opposite_lane}",
                task=clean_task,
                why=clean_why,
                refs=clean_refs,
                acceptance=acceptance,
                source="mcp_coord_server.request_audit",
            )
            event_id = repair_result["event_id"]
        else:
            event_id = coord_db.post_event(
                conn,
                kind="audit_request",
                actor=resolved_actor,
                session_id=resolved_sid,
                to_selector=f"actor:{opposite_lane}",
                work_id=work_id,
                refs_json=json.dumps(clean_refs),
                payload_json=json.dumps(payload, sort_keys=True),
            )
        _refresh_native_cockpit(conn)
        return {
            "verb": "request_audit",
            "work_id": work_id,
            "event_id": event_id,
            "request_kind": clean_request_kind,
            "to_selector": f"actor:{opposite_lane}",
            "target_lane": opposite_lane,
            "actor": resolved_actor,
            "session_id": resolved_sid,
            "assignee_unchanged": assignee or None,
            **{
                key: repair_result[key]
                for key in (
                    "negative_verdict_event_id",
                    "remediation_event_ids",
                    "superseded_event_ids",
                    "replayed",
                )
                if key in repair_result
            },
        }
    finally:
        conn.close()


def _tool_note(
    work_id: str,
    body: str,
    title: str | None = None,
    refs: list[str] | None = None,
    actor: str | None = None,
    session_id: str | None = None,
    to_session_id: str | None = None,
    db_path: str | None = None,
    env: dict | None = None,
) -> dict[str, Any]:
    """Post one neutral, lifecycle-free note about an existing row.

    ``to_session_id`` narrows the address from a lane to one live session. The
    default is unchanged -- the opposite lane -- because that is what a note
    across the pen split means; the session address exists for the case the lane
    address cannot express, which is a fleet of same-lane sessions where
    ``actor:claude`` reaches all of them and is acted on by none.
    """
    clean_work_id = str(work_id or "").strip()
    if not clean_work_id:
        raise ValueError("note requires an existing work_id")
    clean_body = str(body or "").strip()
    if not clean_body:
        raise ValueError("note requires a non-empty body")
    if len(clean_body) > 2048:
        raise ValueError("note body exceeds 2048 characters; use pointer refs")
    clean_title = str(title or "").strip() or f"Note: {clean_work_id}"
    if len(clean_title) > 200:
        raise ValueError("note title exceeds 200 characters")
    clean_refs = [str(ref).strip() for ref in (refs or []) if str(ref).strip()]
    if len(clean_refs) > 32:
        raise ValueError("note refs are bounded to 32 pointers")
    identity, resolved_actor, resolved_sid, label_fields = _resolve_tool_identity(
        actor=actor,
        session_id=session_id,
        env=env,
    )
    if resolved_actor not in _lane_set():
        raise ValueError(f"note actor must be {_lanes_display()}")
    opposite_lane = _counterpart_lane(resolved_actor)
    if opposite_lane is None:
        raise ValueError(
            "note requires a second configured lane; COORD_LANES names only "
            f"{_lanes_display()}"
        )
    clean_target_session = str(to_session_id or "").strip()
    # Provisional: the session target is only validated against the database
    # below, and an unknown or dead session refuses rather than posting. The
    # selector is fixed here because it is part of the idempotency hash, so two
    # identical calls -- lane-addressed and session-addressed -- stay distinct
    # requests rather than the second replaying the first.
    to_selector = (
        f"session:{clean_target_session}"
        if clean_target_session
        else f"actor:{opposite_lane}"
    )
    request = {
        "schema_version": 1,
        "work_id": clean_work_id,
        "actor": resolved_actor,
        "session_id": resolved_sid,
        "to_selector": to_selector,
        "title": clean_title,
        "body": clean_body,
        "refs": clean_refs,
    }
    request_sha256 = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    idempotency_key = f"mcp-note:{clean_work_id}:{request_sha256[:24]}"
    payload = {
        "schema_version": 1,
        "source": "mcp_coord_server.note",
        "event_only": True,
        "neutral_note": True,
        "lifecycle_mutation": False,
        "verdict_semantics": False,
        "request_sha256": request_sha256,
    }
    conn = _get_conn(db_path)
    try:
        coord_db.register_session(
            conn,
            resolved_sid,
            resolved_actor,
            runner_type=identity.get("runner_type"),
            **label_fields,
        )
        row = conn.execute(
            "SELECT work_id FROM work_items WHERE work_id=?", (clean_work_id,)
        ).fetchone()
        if row is None:
            raise ValueError(
                f"note work_id not found: {clean_work_id}; notes attach to existing rows"
            )
        if clean_target_session:
            # Refuses an unknown or expired session rather than writing a
            # selector nobody will ever match. The same check the CLI runs, in
            # coord_db, so the two surfaces cannot disagree about who is
            # addressable.
            coord_db.resolve_note_session_target(conn, clean_target_session)
        event_id = coord_db.post_event(
            conn,
            kind="note",
            actor=resolved_actor,
            session_id=resolved_sid,
            to_selector=to_selector,
            work_id=clean_work_id,
            title=clean_title,
            body=clean_body,
            refs_json=json.dumps(clean_refs),
            payload_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
            idempotency_key=idempotency_key,
        )
        replayed = event_id is None
        if replayed:
            prior = conn.execute(
                "SELECT event_id FROM events WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if prior is None:
                raise ValueError("note idempotency replay missing prior event")
            event_id = int(prior["event_id"])
        if not replayed:
            _refresh_native_cockpit(conn)
        return {
            "verb": "note",
            "work_id": clean_work_id,
            "event_id": event_id,
            "to_selector": to_selector,
            # target_lane keeps naming the opposite lane whether or not a
            # session was addressed, so a caller that only ever read this field
            # keeps reading the same thing; target_session says which of that
            # lane's sessions was named, and is null when none was.
            "target_lane": opposite_lane,
            "target_session": clean_target_session or None,
            "actor": resolved_actor,
            "session_id": resolved_sid,
            "replayed": replayed,
            "event_only": True,
            "lifecycle_mutation": False,
            "verdict_semantics": False,
        }
    finally:
        conn.close()


def _tool_decision(
    ruling: str,
    work_id: str | None = None,
    binds: list[str] | None = None,
    scope: str = "global",
    refs: list[str] | None = None,
    actor: str | None = None,
    session_id: str | None = None,
    db_path: str | None = None,
    env: dict | None = None,
    supersedes_event_id: int | str | None = None,
    valid_from: float | None = None,
    stale_if: list[str] | None = None,
    memory_candidate: bool = False,
) -> dict[str, Any]:
    clean_ruling = str(ruling or "").strip()
    if not clean_ruling:
        raise ValueError("decision requires a non-empty ruling")
    clean_refs = [str(ref).strip() for ref in (refs or []) if str(ref).strip()]
    if len(clean_refs) > 32:
        raise ValueError("decision refs are bounded to 32 pointers")
    clean_binds = [str(b).strip() for b in (binds or []) if str(b).strip()]
    scope = coord_db.normalize_decision_scope(scope)
    identity, resolved_actor, resolved_sid, label_fields = _resolve_tool_identity(
        actor=actor,
        session_id=session_id,
        env=env,
    )
    wid = str(work_id or "").strip() or None
    conn = _get_conn(db_path)
    try:
        coord_db.register_session(
            conn,
            resolved_sid,
            resolved_actor,
            runner_type=identity.get("runner_type"),
            **label_fields,
        )
        event_id = coord_db.post_decision_event(
            conn,
            ruling=clean_ruling,
            actor=resolved_actor,
            session_id=resolved_sid,
            work_id=wid,
            binds=clean_binds,
            scope=scope,
            refs=clean_refs,
            source="mcp_coord_server.decision",
            supersedes_event_id=supersedes_event_id,
            valid_from=valid_from,
            stale_if=stale_if,
            memory_candidate=memory_candidate,
        )
        _refresh_native_cockpit(conn)
        return {
            "verb": "decision",
            "event_id": event_id,
            "work_id": wid,
            "scope": scope,
            "binds": clean_binds,
            "actor": resolved_actor,
            "session_id": resolved_sid,
            "memory_candidate": memory_candidate,
        }
    finally:
        conn.close()


def _tool_session_closeout(
    summary: str,
    successor_hints: list[str] | None = None,
    dead_ends: list[str] | None = None,
    waived_events: list | None = None,
    ack_dirty: bool = False,
    actor: str | None = None,
    session_id: str | None = None,
    db_path: str | None = None,
    knowledge_db: str | Path | None = None,
    env: dict | None = None,
) -> dict[str, Any]:
    clean_summary = str(summary or "").strip()
    if not clean_summary:
        raise ValueError("session_closeout requires a non-empty summary")
    identity, resolved_actor, resolved_sid, label_fields = _resolve_tool_identity(
        actor=actor,
        session_id=session_id,
        env=env,
    )
    dirty = coord_db.discover_dirty_worktree_files()
    conn = _get_conn(db_path)
    try:
        family = coord_db.related_session_ids(conn, resolved_sid, actor=resolved_actor)
        sidecars = coord_db.discover_closeout_job_sidecars(family)
        result = coord_db.session_closeout(
            conn,
            session_id=resolved_sid,
            actor=resolved_actor,
            summary=clean_summary,
            successor_hints=successor_hints,
            dead_ends=dead_ends,
            waived_events=waived_events,
            ack_dirty=ack_dirty,
            dirty_files=dirty,
            live_job_sidecars=sidecars,
            source="mcp_coord_server.session_closeout",
            knowledge_db_path=knowledge_db,
        )
        _refresh_native_cockpit(conn)
        return result
    finally:
        conn.close()


def _tool_audit(
    kind: str,
    title: str | None = None,
    body: str | None = None,
    work_id: str | None = None,
    to_selector: str | None = None,
    payload: dict | None = None,
    verdict: str | None = None,
    operation_id: str | None = None,
    severity: str | None = None,
    refs: list[str] | None = None,
    actor: str | None = None,
    session_id: str | None = None,
    db_path: str | None = None,
    env: dict | None = None,
) -> dict[str, Any]:
    kind = str(kind or "").strip().casefold()
    lifecycle_reserved = bool(
        kind in _GENERIC_AUDIT_RESERVED_KINDS
        or re.fullmatch(
            r"(?:"
            + "|".join(re.escape(lane) for lane in _configured_lanes())
            + r")_(?:claim|heartbeat|block|park|done)",
            kind,
        )
    )
    if lifecycle_reserved:
        raise ValueError(
            "generic audit cannot create handoff/lifecycle reserved events; use the typed "
            "coordination surface after graduation, or the coord CLI today"
        )
    normalized_verdict = str(verdict or "").strip().upper()
    clean_refs = [str(ref).strip() for ref in (refs or []) if str(ref).strip()]
    if len(clean_refs) > 32:
        raise ValueError("audit refs are bounded to 32 pointers")
    if any(len(ref.encode("utf-8")) > 2_048 for ref in clean_refs):
        raise ValueError("each audit ref is bounded to 2048 bytes")
    if kind == "audit_verdict":
        if normalized_verdict not in _AUDIT_VERDICTS:
            raise ValueError(
                f"audit_verdict requires verdict PASS|FLAG|BLOCKED, got {verdict!r}"
            )
        if not str(operation_id or "").strip():
            raise ValueError("audit_verdict requires a stable operation_id for replay safety")
        if not str(work_id or "").strip():
            raise ValueError("audit_verdict requires work_id")
        lane_selectors = {f"actor:{lane}" for lane in _configured_lanes()}
        if str(to_selector or "") not in lane_selectors:
            raise ValueError(
                "audit_verdict requires to_selector "
                + "|".join(sorted(lane_selectors))
            )
    elif verdict is not None or operation_id is not None:
        raise ValueError("verdict/operation_id are valid only when kind='audit_verdict'")
    resolved_actor, resolved_sid = _resolve_process_bound_identity(
        actor=actor,
        session_id=session_id,
        env=env,
        action="audit_verdict" if kind == "audit_verdict" else "generic audit",
    )
    conn = _get_conn(db_path)
    try:
        event_payload = dict(payload or {})
        if kind == "audit_verdict":
            if event_payload and event_payload.get("schema_version") != 1:
                raise ValueError("audit_verdict payload schema_version, when supplied, must be 1")
            unexpected = sorted(set(event_payload) - {"schema_version"})
            if unexpected:
                raise ValueError(
                    "audit_verdict payload is server-authored; remove reserved/custom keys: "
                    f"{unexpected}"
                )
            from coordharness.coord.agent_cli import audit_verdict_payload

            receiver_lane = str(to_selector).split(":", 1)[1]
            event_payload = audit_verdict_payload(
                work_id=str(work_id),
                actor=resolved_actor,
                verdict_value=normalized_verdict,
                severity=severity,
                refs=clean_refs,
                receiver_lane=receiver_lane,
                session_id=resolved_sid,
                source="coordharness-coord.mcp.audit_verdict",
            )
            request_envelope = {
                "schema_version": 1,
                "operation_id": str(operation_id).strip(),
                "work_id": str(work_id),
                "actor": resolved_actor,
                "session_id": resolved_sid,
                "to_selector": to_selector,
                "verdict": normalized_verdict,
                "severity": severity,
                "refs": clean_refs,
                "title": title,
                "body_sha256": hashlib.sha256((body or "").encode("utf-8")).hexdigest(),
            }
            request_sha256 = hashlib.sha256(
                json.dumps(
                    request_envelope,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            event_payload["operation_id"] = str(operation_id).strip()
            event_payload["operation_request_sha256"] = request_sha256
        if kind in _STRUCTURED_EVENT_KINDS and int(event_payload.get("schema_version") or 0) <= 0:
            raise ValueError(f"{kind} events require structured schema_version in payload")
        body_text = body or ""
        body_bytes = len(body_text.encode("utf-8"))
        payload_json_raw = json.dumps(event_payload, sort_keys=True, default=str)
        policy = _mcp_policy(
            conn,
            action="audit",
            work_id=work_id,
            actor=resolved_actor,
            session_id=resolved_sid,
            payload={
                "kind": kind,
                "output_bytes": body_bytes,
                "payload_bytes": len(payload_json_raw.encode("utf-8")),
                "run_event_category": "artifact",
            },
        )
        if policy.get("blocked"):
            raise ValueError(f"policy blocked audit {work_id or '<none>'}: {policy.get('block_reason')}")
        bounded_body = apply_output_budget(
            body_text,
            inline_limit=_AUDIT_INLINE_BODY_LIMIT,
            artifact_dir=_HARNESS / "data_local" / "policy_output",
            artifact_prefix=f"mcp-{kind}",
        )
        if bounded_body["truncated"]:
            event_payload["output_budget"] = {
                "truncated": True,
                "bytes": bounded_body["bytes"],
                "inline_limit": bounded_body["inline_limit"],
                "artifact_ref": bounded_body["artifact_ref"],
            }
        bounded_payload = _bounded_audit_payload_json(event_payload)
        if kind == "audit_verdict":
            result = coord_db.post_audit_verdict(
                conn,
                work_id=str(work_id),
                verdict=normalized_verdict,
                actor=resolved_actor,
                session_id=resolved_sid,
                to_selector=to_selector,
                severity=severity,
                trust="agent",
                title=title,
                body=bounded_body["text"] if body is not None else None,
                refs_json=json.dumps(clean_refs),
                payload_json=bounded_payload,
                operation_id=str(operation_id),
                request_sha256=request_sha256,
            )
            _refresh_native_cockpit(conn)
            return {
                **result,
                "kind": kind,
                "actor": resolved_actor,
                "to_selector": to_selector,
                "refs": clean_refs,
                "policy": _compact_policy_response(policy),
            }
        event_id = coord_db.post_event(
            conn,
            kind=kind,
            actor=resolved_actor,
            session_id=resolved_sid,
            to_selector=to_selector,
            work_id=work_id,
            severity=severity,
            trust="agent",
            title=title,
            body=bounded_body["text"] if body is not None else None,
            refs_json=json.dumps(clean_refs),
            payload_json=bounded_payload,
        )
        return {
            "event_id": event_id,
            "kind": kind,
            "actor": resolved_actor,
            "refs": clean_refs,
            "policy": _compact_policy_response(policy),
        }
    finally:
        conn.close()


def _tool_verdict(
    work_id: str,
    verdict: str,
    refs: list[str],
    operation_id: str,
    severity: str | None = None,
    title: str | None = None,
    body: str | None = None,
    actor: str | None = None,
    session_id: str | None = None,
    db_path: str | None = None,
    env: dict | None = None,
) -> dict[str, Any]:

    resolved_actor, _resolved_sid = _resolve_process_bound_identity(
        actor=actor,
        session_id=session_id,
        env=env,
        action="verdict",
    )
    author_lane = _counterpart_lane(resolved_actor)
    if author_lane is None:
        raise ValueError(
            "verdict requires a second configured lane; COORD_LANES names only "
            f"{_lanes_display()}"
        )
    return _tool_audit(
        kind="audit_verdict",
        title=title,
        body=body,
        work_id=work_id,
        to_selector=f"actor:{author_lane}",
        verdict=verdict,
        operation_id=operation_id,
        severity=severity,
        refs=refs,
        actor=actor,
        session_id=session_id,
        db_path=db_path,
        env=env,
    )


def _next_work_candidates(
    *,
    actor: str,
    limit: int,
    include_blocked: bool,
    db_path: str | None = None,
    context_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    backlog = _read_json(_roadmap_path(), {"today_live_jobs": [], "items": [], "epics": []})
    allowed = set(_NEXT_WORK_STATUSES)
    if include_blocked:
        allowed.add("blocked")
    rows: list[tuple[tuple[int, float, int, str], dict[str, Any]]] = []
    seen: set[str] = set()
    for idx, row in enumerate(context_rows if context_rows is not None else _coord_context_rows(db_path)):
        rid = _row_id(row)
        if rid:
            seen.add(rid)
        status = str(row.get("status") or "planned").lower()
        if status not in allowed:
            continue
        if (
            str(row.get("resume_predicate_json") or "").strip()
            and row.get("continuation_ready_at") is None
        ):
            continue
        if not _assigned_to(row, actor):
            continue
        if not rid:
            continue
        item = _trim_backlog_row(row)
        item["section"] = "coord.db"
        rows.append(((_status_rank(status), _priority_sort_value(row.get("priority")), idx, rid), item))
    for idx, (section, row) in enumerate(_iter_backlog_items(backlog if isinstance(backlog, dict) else {})):
        if section == "epics":
            continue
        rid = _row_id(row)
        if not rid or rid in seen:
            continue
        status = str(row.get("status") or "planned").lower()
        if status not in allowed:
            continue
        if (
            str(row.get("resume_predicate_json") or "").strip()
            and row.get("continuation_ready_at") is None
        ):
            continue
        if not _assigned_to(row, actor):
            continue
        sort_key = (_status_rank(status), _priority_sort_value(row.get("priority")), idx, rid)
        item = _trim_backlog_row(row)
        item["section"] = section
        rows.append((sort_key, item))
    rows.sort(key=lambda pair: pair[0])
    return [item for _key, item in rows[:max(0, limit)]]


def _tool_preflight(
    actor: str | None = None,
    limit: int = 8,
    include_rows: bool = False,
    assigned_id_limit: int = _PREFLIGHT_ID_LIST_LIMIT,
    session_id: str | None = None,
    db_path: str | None = None,
    env: dict | None = None,
    client_transport: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from coordharness.coord.locked_paths import assert_deployment_locked_data_local

    assert_deployment_locked_data_local(_REPO_ROOT)
    resolved_actor = _resolve_actor(actor, env)
    if harness_config.is_strict_deployment():
        from coordharness.coord.exact_query_core import load_query_snapshot

        query_snapshot = load_query_snapshot(db_path) if _coord_context_enabled(db_path) else None
        context_rows = (
            [
                _coord_board_to_context_row(row)
                for row in query_snapshot.flat_ranked_rows(include_quarantine=True)
            ]
            if query_snapshot is not None
            else []
        )
    else:
        context_conn = _get_read_conn(db_path)
        try:
            context_rows = [
                _coord_board_to_context_row(row)
                for row in coord_db.board_rows(context_conn)
            ]
        finally:
            context_conn.close()
    backlog = _read_json(_roadmap_path(), {"today_live_jobs": [], "items": [], "epics": []})
    assigned_open: list[str] = []
    running: list[str] = []
    blocked: list[str] = []
    for _section, item in _iter_backlog_items(backlog if isinstance(backlog, dict) else {}):
        status = str(item.get("status") or "").lower()
        if _assigned_to(item, resolved_actor) and status not in _TERMINAL_ROADMAP_STATUSES:
            rid = _row_id(item)
            if rid:
                assigned_open.append(rid)
    for item in context_rows:
        status = str(item.get("status") or "").lower()
        if _assigned_to(item, resolved_actor) and status not in _TERMINAL_ROADMAP_STATUSES:
            rid = _row_id(item)
            if rid:
                assigned_open.append(rid)
        claim_status = str(item.get("claim_status") or "").lower()
        if _assigned_to(item, resolved_actor) and claim_status in _ACTIVE_PROGRESS_STATUSES:
            rid = _row_id(item)
            if claim_status == "blocked" and rid:
                blocked.append(rid)
            elif rid:
                running.append(rid)
    candidates = _next_work_candidates(
        actor=resolved_actor,
        limit=limit,
        include_blocked=False,
        db_path=db_path,
        context_rows=context_rows,
    )
    try:
        bounded_assigned_id_limit = max(
            0, min(int(assigned_id_limit), _PREFLIGHT_ID_LIST_LIMIT)
        )
    except (TypeError, ValueError):
        bounded_assigned_id_limit = 8
    assigned_ids, assigned_count, assigned_truncated = _capped_ids(
        assigned_open, limit=bounded_assigned_id_limit
    )
    running_ids, running_count, running_truncated = _capped_ids(running)
    blocked_ids, blocked_count, blocked_truncated = _capped_ids(blocked)
    out: dict[str, Any] = {
        "actor": resolved_actor,
        "roadmap": str(_roadmap_path()),
        "coord_db": db_path or os.environ.get("COORD_COORD_DB") or "default",
        "assigned_open_ids": assigned_ids,
        "assigned_open_count": assigned_count,
        "assigned_open_truncated": assigned_truncated,
        "running_ids": running_ids,
        "running_count": running_count,
        "running_truncated": running_truncated,
        "blocked_ids": blocked_ids,
        "blocked_count": blocked_count,
        "blocked_truncated": blocked_truncated,
        "id_list_limit": bounded_assigned_id_limit,
        "next_work_ids": [str(r.get("id")) for r in candidates],
        "capability_handshake": _capability_handshake(
            db_path, env=env, client_transport=client_transport
        ),
        "policy_epoch": _POLICY_EPOCH,
    }
    resume_conn = _get_read_conn(db_path)
    try:
        out.update(
            coord_db.open_audit_request_summary(
                resume_conn, recipient_lane=resolved_actor
            )
        )
        work_columns = {
            str(row[1])
            for row in resume_conn.execute("PRAGMA table_info(work_items)").fetchall()
        }
        if {"next_step", "resume_when"} <= work_columns:
            resume_where = (
                " FROM work_items WHERE archived_at IS NULL"
                " AND lower(COALESCE(assignee,'')) LIKE ?"
                " AND (COALESCE(next_step,'')<>'' OR COALESCE(resume_when,'')<>'')"
            )
            resume_count = int(
                resume_conn.execute(
                    "SELECT COUNT(*)" + resume_where,
                    (f"%{resolved_actor}%",),
                ).fetchone()[0]
            )
            resume_rows = [
                dict(row)
                for row in resume_conn.execute(
                    "SELECT work_id,intent_state,next_step,resume_when"
                    + resume_where
                    + " ORDER BY updated_at DESC, work_id LIMIT 20",
                    (f"%{resolved_actor}%",),
                ).fetchall()
            ]
            out["resume_intents"] = resume_rows
            out["resume_intents_count"] = resume_count
            out["resume_intents_truncated"] = resume_count > len(resume_rows)
        else:
            out["resume_intents"] = []
            out["resume_intents_count"] = 0
            out["resume_intents_truncated"] = False
        if session_id:
            clean_session_id = str(session_id).strip()
            session_actor = coord_db.expected_actor_for_session_id(clean_session_id)
            registered = resume_conn.execute(
                "SELECT actor FROM agent_sessions WHERE session_id=?",
                (clean_session_id,),
            ).fetchone()
            registered_actor = str(registered["actor"] if registered else "").strip().lower()
            if session_actor and session_actor != resolved_actor:
                raise ValueError(
                    f"preflight session_id {clean_session_id!r} belongs to {session_actor}, "
                    f"not resolved actor {resolved_actor}"
                )
            if registered_actor and registered_actor != resolved_actor:
                raise ValueError(
                    f"preflight session_id {clean_session_id!r} is registered to "
                    f"{registered_actor}, not resolved actor {resolved_actor}"
                )
            if not session_actor and not registered_actor:
                raise ValueError(
                    "preflight session_id must be actor-namespaced or already registered "
                    "to the resolved actor"
                )
            session_claim_count = int(
                resume_conn.execute(
                    "SELECT COUNT(*) FROM claims WHERE session_id=?"
                    " AND status='running' AND expires_at>?",
                    (clean_session_id, coord_db.db_now(resume_conn)),
                ).fetchone()[0]
            )
            out["session_open_claims"] = [
                dict(row)
                for row in resume_conn.execute(
                    "SELECT work_id, step, expires_at FROM claims"
                    " WHERE session_id=? AND status='running' AND expires_at>?"
                    " ORDER BY expires_at DESC LIMIT 50",
                    (clean_session_id, coord_db.db_now(resume_conn)),
                ).fetchall()
            ]
            out["session_open_claims_count"] = session_claim_count
            out["session_open_claims_truncated"] = (
                session_claim_count > len(out["session_open_claims"])
            )
        from coordharness.coord.projection import health_summary

        out["health_summary"] = health_summary(resume_conn)
    finally:
        resume_conn.close()
    if include_rows:
        out["next_work"] = [_compact_work_candidate(r) for r in candidates]
    if db_path is not None:
        conn = _get_read_conn(db_path)
        try:
            out["coord_board_count"] = len(coord_db.board_rows(conn))
        finally:
            conn.close()
    return out


def _tool_orient(
    actor: str | None = None,
    session_id: str | None = None,
    next_limit: int = 4,
    peer_limit: int = _DEFAULT_ORIENT_PEER_LIMIT,
    inbox_limit: int = _DEFAULT_ORIENT_INBOX_LIMIT,
    db_path: str | None = None,
    env: dict | None = None,
) -> dict[str, Any]:
    identity = _resolve(env)
    env_actor = str(identity.get("actor") or "").strip().lower()
    env_sid = str(identity.get("session_id") or "").strip()
    if not env_sid or env_sid.startswith("codex:pid:"):
        raise ValueError("orient requires a session_id or client session environment")
    if actor is not None and str(actor).strip().lower() != env_actor:
        raise ValueError("orient actor must match the per-client stdio process identity")
    if session_id is not None and str(session_id).strip() != env_sid:
        raise ValueError("orient session_id must match the per-client stdio process identity")
    resolved_actor = env_actor
    resolved_sid = env_sid
    expected_actor = coord_db.expected_actor_for_session_id(resolved_sid)
    if expected_actor and expected_actor != resolved_actor:
        raise ValueError(
            f"session_id {resolved_sid!r} requires actor={expected_actor!r}, "
            f"got actor={resolved_actor!r}"
        )

    labels = _session_label_fields(identity)
    conn = _get_conn(db_path)
    try:
        coord_db.register_session(
            conn,
            resolved_sid,
            resolved_actor,
            parent_session_id=identity.get("parent_session_id"),
            runner_type=identity.get("runner_type"),
            **labels,
        )
    finally:
        conn.close()

    next_limit = min(max(0, int(next_limit)), 12)
    peer_limit = min(max(0, int(peer_limit)), 12)
    inbox_limit = min(max(0, int(inbox_limit)), 20)
    preflight = _tool_preflight(
        actor=resolved_actor,
        limit=next_limit,
        include_rows=False,
        db_path=db_path,
        env=env,
    )

    conn = _get_read_conn(db_path)
    try:
        now = coord_db.db_now(conn)
        family_ids = coord_db.related_session_ids(conn, resolved_sid, actor=resolved_actor)
        family_placeholders = ",".join("?" for _ in family_ids)
        own_claim_total = int(
            conn.execute(
                f"SELECT COUNT(*) FROM claims c WHERE c.session_id IN ({family_placeholders})"
                " AND c.status IN ('running','paused','blocked')"
                " AND (c.expires_at IS NULL OR c.expires_at>?)",
                (*family_ids, now),
            ).fetchone()[0]
        )
        own_claims = [
            dict(row)
            for row in conn.execute(
                "SELECT c.claim_id, c.work_id, c.status, c.step, c.expires_at,"
                " w.display, w.title FROM claims c"
                " JOIN work_items w ON w.work_id=c.work_id"
                f" WHERE c.session_id IN ({family_placeholders})"
                " AND c.status IN ('running','paused','blocked')"
                " AND (c.expires_at IS NULL OR c.expires_at>?)"
                " ORDER BY COALESCE(c.heartbeat_at,c.acquired_at,0) DESC LIMIT 12",
                (*family_ids, now),
            ).fetchall()
        ]
        for claim in own_claims:
            claim["actor"] = resolved_actor

        peers: list[dict[str, Any]] = []
        peer_candidates = sorted(
            [
                row
                for row in coord_db.session_rollup(conn, at=now)
                if row.get("live") and str(row.get("session_id") or "") not in set(family_ids)
            ],
            key=lambda row: (str(row.get("actor") or ""), str(row.get("session_id") or "")),
        )
        peer_total = len(peer_candidates)
        for session in peer_candidates[:peer_limit]:
            peer_sid = str(session.get("session_id") or "")
            work_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT work_id, display, title, claim_status, claim_step"
                    " FROM v_work_owner WHERE owner_session_id=?"
                    " AND claim_status IN ('running','paused','blocked')"
                    " ORDER BY updated_at DESC LIMIT ?",
                    (peer_sid, _DEFAULT_ORIENT_WORK_LIMIT),
                ).fetchall()
            ]
            peers.append(
                {
                    "actor": session.get("actor"),
                    "session_id": peer_sid,
                    "human_label": session.get("human_label"),
                    "runner_type": session.get("runner_type"),
                    "work": work_rows,
                }
            )

        inbox_rows = coord_db.inbox_recent(
            conn,
            recipient_actor=resolved_actor,
            session_id=resolved_sid,
            limit=inbox_limit,
            exclude_kinds=coord_db.INBOX_NOISE_KINDS,
        )
        inbox = [_event_summary(dict(row), conn=conn) for row in inbox_rows]
        for event in inbox:
            event["recipient"] = resolved_actor
            event["acked"] = False
    finally:
        conn.close()

    from coordharness.coord.exact_query_core import load_query_snapshot

    query_snapshot = load_query_snapshot(db_path)
    context_capsule = query_snapshot.session_capsule(
        actor=resolved_actor,
        claims=own_claims,
        inbox_events=inbox,
    )

    return {
        "schema_version": 1,
        "actor": resolved_actor,
        "session_id": resolved_sid,
        "presence_registered": True,
        "own_claims": own_claims,
        "own_claims_returned": len(own_claims),
        "own_claims_total": own_claim_total,
        "own_claims_truncated": len(own_claims) < own_claim_total,
        "peer_sessions": peers,
        "peer_count": len(peers),
        "peer_count_total": peer_total,
        "peer_sessions_truncated": len(peers) < peer_total,
        "inbox_recent": inbox,
        "inbox_count": len(inbox),
        "inbox_limit": inbox_limit,
        "inbox_maybe_truncated": bool(inbox_limit and len(inbox) >= inbox_limit),
        "context_capsule": context_capsule,
        "query_core": query_snapshot.receipt(),
        "preflight": {
            "assigned_open_ids": preflight.get("assigned_open_ids", [])[:6],
            "assigned_open_count": preflight.get("assigned_open_count", 0),
            "running_ids": preflight.get("running_ids", [])[:6],
            "running_count": preflight.get("running_count", 0),
            "blocked_ids": preflight.get("blocked_ids", [])[:6],
            "blocked_count": preflight.get("blocked_count", 0),
            "next_work_ids": preflight.get("next_work_ids", []),
        },
    }


def _tool_next_work(
    actor: str | None = None,
    limit: int = 10,
    include_blocked: bool = False,
    include_details: bool = False,
    db_path: str | None = None,
    env: dict | None = None,
) -> dict[str, Any]:
    resolved_actor = _resolve_actor(actor, env)
    context_rows = _coord_context_rows(db_path)
    candidates = _next_work_candidates(
        actor=resolved_actor,
        limit=limit,
        include_blocked=include_blocked,
        db_path=db_path,
        context_rows=context_rows,
    )
    items = candidates if include_details else [_compact_work_candidate(r) for r in candidates]
    return {
        "actor": resolved_actor,
        "items": items,
        "count": len(items),
        "include_details": bool(include_details),
        "query_core": (
            _query_core_receipt_for_profile(db_path)
            if _coord_context_enabled(db_path)
            else {"mode": "disabled_for_injected_fixture_root", "production_eligible": False}
        ),
    }


def _tool_work_context(
    work_id: str,
    actor: str | None = None,
    event_limit: int = 8,
    include_event_details: bool = False,
    db_path: str | None = None,
    env: dict | None = None,
) -> dict[str, Any]:
    resolved_actor = _resolve_actor(actor, env)
    backlog = _read_json(_roadmap_path(), {"today_live_jobs": [], "items": [], "epics": []})
    try:
        section, row = _find_backlog_row(backlog if isinstance(backlog, dict) else {}, work_id)
    except ValueError:
        coord_matches = [r for r in _coord_context_rows(db_path) if _row_id(r) == work_id]
        if not coord_matches:
            return {
                "ok": False,
                "actor": resolved_actor,
                "work_id": work_id,
                "error": {
                    "code": "work_not_found",
                    "message": f"work_id {work_id!r} is not present in coord.db or the roadmap projection",
                    "recoverable": True,
                    "recovery": [
                        "call board with a bounded limit",
                        "call next_work for the current actor",
                        "create the first work item through the CLI if the board is empty",
                    ],
                },
            }
        section, row = "coord.db", coord_matches[0]
    parent_id = str(row.get("parent") or "").strip()
    epic_id = str(row.get("epic") or "").strip()
    parent = None
    epic = None
    if parent_id:
        try:
            parent = _trim_backlog_row(_find_backlog_row(backlog, parent_id)[1])
        except ValueError:
            parent = {"id": parent_id, "missing": True}
    if epic_id:
        try:
            epic = _trim_backlog_row(_find_backlog_row(backlog, epic_id)[1])
        except ValueError:
            epic = {"id": epic_id, "missing": True}

    conn = _get_read_conn(db_path)
    try:
        handoff_row = conn.execute(
            "SELECT version,assignee,intent_state,archived_at,done_signal"
            " FROM work_items WHERE work_id=?",
            (work_id,),
        ).fetchone()
        handoff_preconditions = None
        if handoff_row is not None:
            head_state = coord_db._typed_handoff_head_state_unlocked(conn, work_id)
            active_event_ids = list(head_state["active_event_ids"])
            quarantined_event_ids = list(head_state["quarantined_event_ids"])
            handoff_preconditions = {
                "expected_version": int(handoff_row["version"]),
                "expected_assignee": str(handoff_row["assignee"] or "").strip().lower(),
                "expected_head_event_ids": active_event_ids[:_HANDOFF_EVENT_ID_PREVIEW_LIMIT],
                "expected_head_event_ids_total": len(active_event_ids),
                "expected_head_event_ids_truncated": (
                    len(active_event_ids) > _HANDOFF_EVENT_ID_PREVIEW_LIMIT
                ),
                "quarantined_event_ids": (
                    quarantined_event_ids[:_HANDOFF_EVENT_ID_PREVIEW_LIMIT]
                ),
                "quarantined_event_ids_total": len(quarantined_event_ids),
                "quarantined_event_ids_truncated": (
                    len(quarantined_event_ids) > _HANDOFF_EVENT_ID_PREVIEW_LIMIT
                ),
                "writer_head_set_eligible": (
                    len(active_event_ids) <= _HANDOFF_EVENT_ID_PREVIEW_LIMIT
                ),
                "intent_state": handoff_row["intent_state"],
                "archived": handoff_row["archived_at"] is not None,
                "has_done_signal": bool(str(handoff_row["done_signal"] or "").strip()),
                "writer_status": "dark_deferred_not_graduated",
            }
        board_row = _coord_board_row(conn, work_id)
        board = [board_row] if board_row else []
        claim_rows = [
            dict(r) for r in conn.execute(
                "SELECT c.claim_id, c.work_id, c.session_id, c.status, c.step,"
                " c.acquired_at, c.heartbeat_at, c.expires_at, s.actor"
                " FROM claims c LEFT JOIN agent_sessions s ON s.session_id=c.session_id"
                " WHERE c.work_id=?"
                " ORDER BY COALESCE(c.heartbeat_at, c.acquired_at, 0) DESC"
                " LIMIT 10",
                (work_id,),
            ).fetchall()
        ]
        event_rows = conn.execute(
            "SELECT event_id, ts, kind, actor, session_id, to_selector, work_id,"
            " run_id, thread_id, severity, verdict, trust, title, body,"
            " refs_json, payload_json"
            " FROM events WHERE work_id=? ORDER BY event_id DESC LIMIT ?",
            (work_id, max(0, event_limit)),
        ).fetchall()
        if include_event_details:
            events = [_parse_event_json(dict(r), conn=conn) for r in event_rows]
        else:
            events = [_event_summary(dict(r), conn=conn) for r in event_rows]
    finally:
        conn.close()

    return {
        "actor": resolved_actor,
        "work_id": work_id,
        "section": section,
        "row": _trim_backlog_row(row),
        "parent": parent,
        "epic": epic,
        "coord_claims": claim_rows,
        "coord_board": board,
        "handoff_preconditions": handoff_preconditions,
        "events": events,
        "events_compact": not bool(include_event_details),
    }


def _tool_event_context(
    event_id: int | str,
    include_raw: bool = False,
    db_path: str | None = None,
) -> dict[str, Any]:
    conn = _get_read_conn(db_path)
    try:
        row = conn.execute(
            "SELECT event_id, ts, kind, actor, session_id, to_selector, work_id,"
            " run_id, thread_id, severity, verdict, trust, title, body,"
            " refs_json, payload_json"
            " FROM events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"event_id not found: {event_id}")
        return {"event": _parse_event_json(dict(row), include_raw=include_raw, conn=conn)}
    finally:
        conn.close()


def _tool_get_decision_context(
    work_id: str | None = None,
    scope: str | None = None,
    as_of: float | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    conn = _get_read_conn(db_path)
    try:
        heads, conflicts = coord_db._resolve_decision_heads_and_conflicts(
            conn, work_id=work_id, scope=scope, as_of=as_of
        )
    finally:
        conn.close()
    return {
        "work_id": work_id,
        "scope": scope,
        "heads": heads,
        "count": len(heads),
        "conflicts": conflicts,
    }


def _tool_inbox_recent(
    actor: str | None = None,
    session_id: str | None = None,
    limit: int = _DEFAULT_INBOX_RECENT_LIMIT,
    exclude_noise: bool = True,
    db_path: str | None = None,
    env: dict | None = None,
) -> dict[str, Any]:
    identity = _resolve(env)
    resolved_actor = actor or identity["actor"]
    resolved_sid = session_id or identity.get("session_id") or ""
    exclude = coord_db.INBOX_NOISE_KINDS if exclude_noise else ()
    conn = _get_read_conn(db_path)
    try:
        msgs = coord_db.inbox_recent(
            conn,
            recipient_actor=resolved_actor,
            session_id=resolved_sid,
            limit=limit,
            exclude_kinds=exclude,
        )
        parsed = [_parse_event_json(dict(m), conn=conn) for m in msgs]
        return {
            "actor": resolved_actor,
            "session_id": resolved_sid,
            "messages": parsed,
            "count": len(parsed),
            "exclude_noise": bool(exclude_noise),
        }
    finally:
        conn.close()


_PROVIDER_FIRST_ENV = {
    "pointer": "COORD_PROVIDER_FIRST_POINTER",
    "context_db": "COORD_CONTEXT_DB",
    "context_db_sha256": "COORD_CONTEXT_DB_SHA256",
    "context_generation_id": "COORD_CONTEXT_GENERATION_ID",
    "context_manifest_path": "COORD_CONTEXT_MANIFEST_PATH",
    "context_manifest_sha256": "COORD_CONTEXT_MANIFEST_SHA256",
    "verified_generation_path": "COORD_VERIFIED_GENERATION_PATH",
    "verified_manifest_sha256": "COORD_VERIFIED_MANIFEST_SHA256",
}


def _provider_first_args_from_env(env: dict[str, str] | None = None) -> dict[str, Any]:

    source = os.environ if env is None else env
    pointer_value = source.get(_PROVIDER_FIRST_ENV["pointer"])
    explicit = {
        key: source.get(flag)
        for key, flag in _PROVIDER_FIRST_ENV.items()
        if key != "pointer"
    }
    if pointer_value:
        if any(value not in (None, "") for value in explicit.values()):
            raise ValueError("provider-first pointer cannot be combined with explicit binding fields")
        pointer = Path(pointer_value)
        if pointer.is_symlink() or not pointer.is_file():
            raise ValueError("provider-first custody pointer must be a regular file")
        before = pointer.stat()
        raw = pointer.read_bytes()
        after = pointer.stat()
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after:
            raise RuntimeError("provider-first custody pointer changed during read")
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("provider-first custody pointer is invalid JSON") from exc
        if payload == {
            "schema": "coordharness.provider-first-runtime-pointer.v1",
            "state": "legacy_unbound",
        }:
            return {key: None for key in explicit}
        expected = {
            "schema",
            "context_db",
            "context_db_sha256",
            "context_generation_id",
            "context_manifest_path",
            "context_manifest_sha256",
            "verified_generation_path",
            "verified_manifest_sha256",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("provider-first custody pointer schema keys mismatch")
        if payload.get("schema") != "coordharness.provider-first-runtime-pointer.v1":
            raise ValueError("provider-first custody pointer schema is unsupported")
        return {key: payload[key] for key in explicit}
    return explicit


def _canonical_sha256(value: str | None, *, label: str) -> str:
    text = str(value or "")
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _resolve_provider_first_binding(
    *,
    context_db: str | Path | None,
    context_db_sha256: str | None,
    context_generation_id: str | None,
    context_manifest_path: str | Path | None,
    context_manifest_sha256: str | None,
    verified_generation_path: str | Path | None,
    verified_manifest_sha256: str | None,
) -> tuple[dict[str, Any], Any]:

    supplied = {
        "context_db": context_db,
        "context_db_sha256": context_db_sha256,
        "context_generation_id": context_generation_id,
        "context_manifest_path": context_manifest_path,
        "context_manifest_sha256": context_manifest_sha256,
        "verified_generation_path": verified_generation_path,
        "verified_manifest_sha256": verified_manifest_sha256,
    }
    present = {key for key, value in supplied.items() if value not in (None, "")}
    if not present:
        return (
            {
                "schema": "coordharness.mcp-provider-first-binding.v1",
                "state": "legacy_unbound",
                "enabled": False,
                "fallback_policy": "legacy_mixed_knowledge_db",
            },
            None,
        )
    if present != set(supplied):
        raise ValueError(
            "provider-first MCP binding is all-or-none; missing "
            f"{sorted(set(supplied) - present)}"
        )
    resolved_context = Path(context_db).resolve()
    resolved_manifest = Path(context_manifest_path).resolve()
    resolved_verified = Path(verified_generation_path).resolve()
    expected_db = _canonical_sha256(context_db_sha256, label="context_db_sha256")
    expected_manifest = _canonical_sha256(
        context_manifest_sha256, label="context_manifest_sha256"
    )
    expected_verified = _canonical_sha256(
        verified_manifest_sha256, label="verified_manifest_sha256"
    )
    generation_id = str(context_generation_id or "")
    if not re.fullmatch(r"kfts-pf-r1-sha256-[0-9a-f]{64}", generation_id):
        raise ValueError("context_generation_id is not an exact provider-first successor")
    if not resolved_context.is_file() or sha256_file(resolved_context) != expected_db:
        raise ValueError("provider-first context database hash mismatch")
    if not resolved_manifest.is_file() or sha256_file(resolved_manifest) != expected_manifest:
        raise ValueError("provider-first context manifest hash mismatch")
    if not resolved_verified.is_file() or sha256_file(resolved_verified) != expected_verified:
        raise ValueError("provider-first verified-generation manifest hash mismatch")
    manifest = json.loads(resolved_manifest.read_text())
    manifest_status = str(manifest.get("status") or "").strip()
    if (
        manifest.get("generation_id") != generation_id
        or manifest.get("documentary_database_sha256") != expected_db
        or not manifest_status
        or manifest.get("live_effects") is not False
    ):
        raise ValueError("provider-first context manifest contract mismatch")
    uri = f"file:{resolved_context}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("provider-first context database quick_check failed")
    finally:
        conn.close()
    verified_reader = None
    receipt = {
        "schema": "coordharness.mcp-provider-first-binding.v1",
        "state": "exact_bound",
        "enabled": True,
        "context_generation_id": generation_id,
        "context_db_path": str(resolved_context),
        "context_db_sha256": expected_db,
        "context_manifest_path": str(resolved_manifest),
        "context_manifest_sha256": expected_manifest,
        "context_manifest_status": manifest_status,
        "verified_generation_path": str(resolved_verified),
        "verified_manifest_sha256": expected_verified,
        "fallback_policy": "exact_providers_first_documentary_never_exact_fallback",
    }
    return receipt, verified_reader


def _tool_knowledge_search(
    query: str,
    limit: int | None = None,
    profile: str = "brief",
    work_id: str | None = None,
    manual: bool = False,
    sources: list[str] | None = None,
    include_diagnostics: bool = False,
    knowledge_db: str | Path | None = None,
    context_db: str | Path | None = None,
) -> dict[str, Any]:
    search_query = _bounded_text_bytes(" ".join(str(query or "").split()), 2_048)
    if work_id is not None and len(str(work_id).encode("utf-8")) > 200:
        raise ValueError("knowledge_search work_id is bounded to 200 bytes")
    profile_id = str(profile or "brief").strip() or "brief"
    profile_config = config_for_context_profile(profile_id, allow_manual=bool(manual))
    if limit is None:
        requested_limit = profile_config.max_hits
    else:
        requested_limit = max(1, int(limit))
    bounded_limit = max(
        1,
        min(requested_limit, profile_config.max_hits, _MAX_KNOWLEDGE_SEARCH_LIMIT),
    )
    requested_sources = []
    for source in sources or []:
        normalized = str(source or "").strip().lower()
        if normalized and normalized not in requested_sources:
            requested_sources.append(normalized)
    unknown_sources = sorted(set(requested_sources) - _KNOWLEDGE_PROVIDER_NAMES)
    if unknown_sources:
        raise ValueError(
            f"unknown knowledge source(s): {unknown_sources}; expected "
            f"{sorted(_KNOWLEDGE_PROVIDER_NAMES)}"
        )
    resolved_context_db = context_db if context_db is not None else knowledge_db
    if not requested_sources or "kfts" in requested_sources:
        stats_kwargs: dict[str, Any] = {"use_manifest": True, "scan_fallback": False}
        if resolved_context_db is not None:
            stats_kwargs["db_path"] = resolved_context_db
        stats = kfts.index_stats(**stats_kwargs)
    else:
        stats = {"queried": False, "reason": "kfts provider not selected"}
    config = type(profile_config)(
        max_hits=bounded_limit,
        per_provider_limit=max(1, min(bounded_limit, profile_config.per_provider_limit)),
        max_packet_bytes=(
            profile_config.max_packet_bytes
            if include_diagnostics
            else profile_config.max_packet_bytes * 2
        ),
        include_board_history=profile_config.include_board_history,
        compact_metadata=not bool(include_diagnostics),
    )
    provider_kwargs: dict[str, Any] = {
        "include_board_history": config.include_board_history or "board_history" in requested_sources,
    }
    if knowledge_db is not None:
        provider_kwargs["knowledge_db"] = knowledge_db
    if context_db is not None:
        provider_kwargs["context_db"] = context_db
    providers = default_context_providers(**provider_kwargs)
    if requested_sources:
        providers = [provider for provider in providers if provider.source in requested_sources]
    packet = ContextFederator(providers, config=config).search(search_query, work_id=work_id)
    federated = packet.to_dict()
    # The facts provider answers a missing or schema-less store with an empty
    # list and no error, which the composite then serves as a plausible zero.
    # Report the store condition instead of letting the zero stand for it.
    if any(provider.source == "facts" for provider in providers):
        fact_state = fact_store_state(knowledge_db)
        if fact_state["state"] != "ready":
            message = (
                f"fact store is {fact_state['state']}: {fact_state['detail']}"
            )
            for result in federated.get("provider_results") or []:
                if result.get("source") == "facts" and not result.get("error"):
                    result["error"] = message
            errors = federated.setdefault("errors", [])
            if not any(
                entry.get("source") == "facts" for entry in errors if isinstance(entry, dict)
            ):
                errors.append({"source": "facts", "error": message})
    context_meta = {
        key: federated.get(key)
        for key in ("schema_version", "query", "work_id", "provider_results", "errors", "truncated", "expansion")
    }
    if not include_diagnostics:
        context_meta = {
            "provider_results": [
                {
                    "source": result.get("source"),
                    "returned": result.get("returned"),
                    "elapsed_s": result.get("elapsed_s"),
                    "truncated": bool(result.get("truncated")),
                    "error": result.get("error"),
                }
                for result in context_meta.get("provider_results", [])
            ],
            "errors": context_meta.get("errors") or [],
            "truncated": bool(context_meta.get("truncated")),
            "read_first_pointer": (context_meta.get("expansion") or {}).get("read_first_pointer"),
        }
    results = [
        {
            "pointer": hit.get("pointer"),
            "title": hit.get("title"),
            "snippet": hit.get("snippet"),
            "source": hit.get("source"),
            "kind": hit.get("kind"),
            "score": hit.get("score"),
            "metadata": hit.get("metadata") or {},
        }
        for hit in federated.get("hits", [])
    ]
    compact_index_stats = {
        key: stats.get(key)
        for key in (
            "documents",
            "cards",
            "index_present",
            "schema_current",
            "source_file_count",
            "indexed_source_path_count",
            "newest_source_mtime",
            "newest_index_mtime",
            "stale",
            "freshness_basis",
            "queried",
            "reason",
        )
        if key in stats
    }
    if stats.get("stale_reasons"):
        compact_index_stats["stale_reasons"] = [
            _bounded_text_bytes(reason, 240)
            for reason in list(stats.get("stale_reasons") or [])[:3]
        ]
    response = {
        "query": _bounded_text_bytes(search_query, 512),
        "profile": profile_id,
        "work_id": work_id,
        "manual": bool(manual),
        "sources": [provider.source for provider in providers],
        "include_diagnostics": bool(include_diagnostics),
        "limit": bounded_limit,
        "results": results,
        "count": len(results),
        "context": context_meta,
        "index_stats": compact_index_stats,
        "index_refresh": {
            "automatic": False,
            # There is no rebuild script and no module entry point; naming one
            # sent readers after a file no checkout contains. Name the callable
            # that actually does it.
            "command": (
                'python -c "from coordharness.knowledge.kfts import rebuild_index; '
                'print(rebuild_index())"'
            ),
        },
        "byte_budget": profile_config.max_packet_bytes,
        "transport_reserve_bytes": _KNOWLEDGE_TRANSPORT_RESERVE_BYTES,
        "response_bytes": 0,
        "transport_bytes": 0,
    }
    byte_budget = int(response["byte_budget"])
    while not _knowledge_response_fits(response, byte_budget) and response["results"]:
        response["results"].pop()
        response["count"] = len(response["results"])
        response["context"]["truncated"] = True
    if not _knowledge_response_fits(response, byte_budget):
        response["context"]["provider_results"] = [
            {
                key: value
                for key, value in result.items()
                if key in {"source", "returned", "truncated", "error"}
            }
            for result in response["context"].get("provider_results", [])
        ]
        response["context"]["truncated"] = True
    if not _knowledge_response_fits(response, byte_budget):
        response["context"] = {
            "provider_results": [],
            "errors": [],
            "truncated": True,
            "read_first_pointer": None,
        }
    if not _knowledge_response_fits(response, byte_budget):
        response["index_stats"] = {
            "queried": compact_index_stats.get("queried", "kfts" in response["sources"]),
            "stale": compact_index_stats.get("stale"),
        }
        response["query"] = _bounded_text_bytes(response["query"], 160)
    _measure_knowledge_response(response)
    return response


def _tool_read_note(
    pointer: str,
    max_bytes: int = DEFAULT_POINTER_READ_BYTES,
    *,
    context_db: str | Path | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"max_bytes": max_bytes}
    if context_db is not None:
        kwargs["context_db"] = context_db
    payload = read_context_pointer(pointer, **kwargs)
    content = payload.get("content")
    return {
        **payload,
        "body": content,
    }


def _fact_to_dict(
    fact: facts.Fact,
    *,
    include_evidence: bool = False,
    evidence_max_bytes: int = 800,
    context_db: str | Path | None = None,
) -> dict[str, Any]:
    row = {
        "id": fact.id,
        "statement": fact.statement,
        "value": fact.value,
        "unit": fact.unit,
        "status": fact.status,
        "module": fact.module,
        "evidence_pointer": fact.evidence_pointer,
        "supersedes": fact.supersedes,
        "superseded_by": fact.superseded_by,
        "owner_lane": fact.owner_lane,
        "updated_at": fact.updated_at,
        "notes": fact.notes,
    }
    lifecycle = facts.fact_lifecycle(fact)
    if lifecycle is not None:
        row["fact_lifecycle"] = lifecycle
        row["retirement_marking"] = lifecycle["retirement_marking"]
    if include_evidence:
        evidence_kwargs: dict[str, Any] = {"max_bytes": evidence_max_bytes}
        if context_db is not None:
            evidence_kwargs["context_db"] = context_db
        row["evidence"] = facts.resolve_fact_evidence(fact, **evidence_kwargs)
    return row


def _fact_search_hit_to_dict(
    hit: facts.FactSearchHit,
    *,
    include_evidence: bool = False,
    evidence_max_bytes: int = 800,
    context_db: str | Path | None = None,
) -> dict[str, Any]:
    row = _fact_to_dict(
        hit.fact,
        include_evidence=include_evidence,
        evidence_max_bytes=evidence_max_bytes,
        context_db=context_db,
    )
    row.update(
        {
            "score": hit.score,
            "matched_terms": list(hit.matched_terms),
            "rank_reasons": list(hit.rank_reasons),
        }
    )
    return row


def _facts_db_path_for_tool(db_path: str | None) -> str | None:
    if db_path is None:
        return None
    requested = Path(db_path).resolve()
    canonical = facts.DEFAULT_INDEX_DB.resolve()
    if requested == canonical:
        return str(requested)
    if os.environ.get("COORD_ALLOW_TEST_KNOWLEDGE_DB") == "1":
        return str(requested)
    raise ValueError(
        f"facts_lookup db_path override is disabled for MCP/public use; "
        f"expected canonical {canonical}"
    )


class FactStoreUnavailable(RuntimeError):
    """The fact ledger cannot answer, so no count may be returned in its place.

    A missing store and a store with no matching rows are different answers.
    Both used to render as ``count: 0``; only one of them is a result.
    """

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = dict(state)
        super().__init__(
            f"fact store is {state.get('state')} at {state.get('path')}: "
            f"{state.get('detail')}"
        )


#: Tables ``facts.init_db`` creates; the ledger cannot answer without them.
_FACT_STORE_REQUIRED_TABLES = ("facts",)


def fact_store_state(db_path: str | Path | None = None) -> dict[str, Any]:
    """Classify the fact ledger before querying it.

    ``ready`` is the only state in which an empty result set means "no matching
    fact". ``absent`` / ``schema_missing`` / ``unreadable`` are conditions of the
    store, and reporting them as an empty result set is the serving-integrity
    failure this surface must not commit.
    """

    path = Path(db_path) if db_path else facts.DEFAULT_INDEX_DB
    state: dict[str, Any] = {"path": str(path), "state": "ready", "detail": None}
    if not path.exists():
        state.update(state="absent", detail="fact store file does not exist")
        return state
    try:
        conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        state.update(state="unreadable", detail=f"{type(exc).__name__}: {exc}")
        return state
    try:
        present = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchall()
        }
    except sqlite3.Error as exc:
        state.update(state="unreadable", detail=f"{type(exc).__name__}: {exc}")
        return state
    finally:
        conn.close()
    missing = [name for name in _FACT_STORE_REQUIRED_TABLES if name not in present]
    if missing:
        state.update(
            state="schema_missing",
            detail=f"fact store is missing table(s): {', '.join(missing)}",
        )
    return state


def _require_fact_store(db_path: str | Path | None) -> dict[str, Any]:
    state = fact_store_state(db_path)
    if state["state"] != "ready":
        raise FactStoreUnavailable(state)
    return state


def ensure_fact_store(db_path: str | Path | None = None) -> dict[str, Any]:
    """Create the fact ledger schema so a clean install has a real store.

    ``bootstrap_database`` builds coord.db only; without this the knowledge
    surface answers every query from a store that was never created.
    """

    state = fact_store_state(db_path)
    if state["state"] in {"ready", "unreadable"}:
        return {**state, "created": False}
    facts.init_db(db_path)
    return {**fact_store_state(db_path), "created": True}


def _tool_facts_lookup(
    query: str,
    mode: str = "auto",
    module: str | None = None,
    status: str | None = None,
    limit: int = 20,
    include_evidence: bool = False,
    evidence_max_bytes: int = 800,
    db_path: str | None = None,
    knowledge_db: str | Path | None = None,
    context_db: str | Path | None = None,
) -> dict[str, Any]:
    q = str(query or "").strip()
    if not q:
        raise ValueError("query is required")
    mode = str(mode or "auto").strip().lower()
    if mode not in {"auto", "current", "decided", "query"}:
        raise ValueError("mode must be one of auto|current|decided|query")
    try:
        bounded_limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        bounded_limit = 20
    try:
        bounded_evidence_bytes = max(1, min(int(evidence_max_bytes), 5_000))
    except (TypeError, ValueError):
        bounded_evidence_bytes = 800
    if db_path is not None and knowledge_db is not None:
        if Path(db_path).resolve() != Path(knowledge_db).resolve():
            raise ValueError("facts_lookup received conflicting db_path and knowledge_db")
    facts_db_path = (
        str(Path(knowledge_db).resolve())
        if knowledge_db is not None
        else _facts_db_path_for_tool(db_path)
    )
    store = _require_fact_store(facts_db_path)

    selected_mode = mode
    rows: list[dict[str, Any]] = []
    if mode in {"auto", "current"}:
        current = facts.current_value(q, module=module, db_path=facts_db_path)
        if current is not None:
            rows = [
                _fact_to_dict(
                    current,
                    include_evidence=include_evidence,
                    evidence_max_bytes=bounded_evidence_bytes,
                    context_db=context_db,
                )
            ]
            selected_mode = "current"
    if not rows and mode in {"auto", "decided"}:
        hits = facts.search_facts(
            q,
            statuses=("closed", "parked", "corrected"),
            db_path=facts_db_path,
            include_history=True,
            limit=bounded_limit,
        )
        if hits:
            rows = [
                _fact_search_hit_to_dict(
                    hit,
                    include_evidence=include_evidence,
                    evidence_max_bytes=bounded_evidence_bytes,
                    context_db=context_db,
                )
                for hit in hits
            ]
            selected_mode = "decided"
    if not rows and mode in {"auto", "query"}:
        hits = facts.search_facts(
            q,
            module=module,
            status=status,
            db_path=facts_db_path,
            limit=bounded_limit,
        )
        rows = [
            _fact_search_hit_to_dict(
                hit,
                include_evidence=include_evidence,
                evidence_max_bytes=bounded_evidence_bytes,
                context_db=context_db,
            )
            for hit in hits
        ]
        selected_mode = "query"

    return {
        "query": q,
        "mode": selected_mode,
        "requested_mode": mode,
        "module": module,
        "status": status,
        "limit": bounded_limit,
        "include_evidence": bool(include_evidence),
        "evidence_max_bytes": bounded_evidence_bytes if include_evidence else 0,
        "results": rows,
        "count": len(rows),
        "store": store,
    }


def _tool_facts_query(
    module: str | None = None,
    status: str | None = None,
    text: str | None = None,
    limit: int = read_surface.DEFAULT_FACTS_LIMIT,
    knowledge_db: str | Path | None = None,
) -> dict[str, Any]:
    return read_surface.facts_query(
        module=module,
        status=status,
        text=text,
        limit=limit,
        db_path=knowledge_db,
    )


def _tool_knowledge_index_status(
    use_manifest: bool = False,
    scan_fallback: bool = True,
    knowledge_db: str | Path | None = None,
) -> dict[str, Any]:
    return read_surface.knowledge_index_status(
        use_manifest=use_manifest,
        scan_fallback=scan_fallback,
        db_path=knowledge_db,
    )


def _tool_memory_proposals_list(
    status: str | None = None,
    kind: str | None = None,
    limit: int = read_surface.DEFAULT_PROPOSALS_LIMIT,
    knowledge_db: str | Path | None = None,
) -> dict[str, Any]:
    return read_surface.memory_proposals_list(
        status=status,
        kind=kind,
        limit=limit,
        db_path=knowledge_db,
    )


def _tool_memory_proposals_get(
    id: str,
    knowledge_db: str | Path | None = None,
) -> dict[str, Any]:
    return read_surface.memory_proposals_get(id=id, db_path=knowledge_db)


def build_server(
    db_path: str | None = None,
    *,
    knowledge_db: str | Path | None = None,
    context_db: str | Path | None = None,
    context_db_sha256: str | None = None,
    context_generation_id: str | None = None,
    context_manifest_path: str | Path | None = None,
    context_manifest_sha256: str | None = None,
    verified_generation_path: str | Path | None = None,
    verified_manifest_sha256: str | None = None,
) -> FastMCP:
    from coordharness.bootstrap import bootstrap_database

    bootstrap_database(db_path)
    # bootstrap_database builds coord.db only. Without this the fact ledger is
    # never created on a clean install, and every facts read then answers from
    # a store that does not exist.
    ensure_fact_store(knowledge_db)
    provider_binding, verified_reader = _resolve_provider_first_binding(
        context_db=context_db,
        context_db_sha256=context_db_sha256,
        context_generation_id=context_generation_id,
        context_manifest_path=context_manifest_path,
        context_manifest_sha256=context_manifest_sha256,
        verified_generation_path=verified_generation_path,
        verified_manifest_sha256=verified_manifest_sha256,
    )
    mcp = FastMCP(
        "coordharness-coord",
        instructions=(
            "Generic coordination control-plane for multi-agent software work. "
            "Backed by coord.db (SQLite-WAL). Use claim_work before doing any "
            "substantive work on a roadmap item, heartbeat during long work, "
            "park or block with durable resume intent when execution pauses, "
            "complete when done, and verdict for independent review. Use inbox "
            "to receive cross-agent messages and note for neutral mid-flight context. "
            "Use preflight at task start for bounded read-only orientation, then next_work, work_context, "
            "event_context, and inbox_recent for targeted expansion. Orient is an explicit presence/lease "
            "writer available only in client profiles that expose it; delegated subagents do not call it unless "
            "they operate as independent coord sessions. Writer tools mutate state through typed coordination "
            "operations; context reads use query-only connections and status is always DERIVED."
        ),
    )
    visible_tools = set(_server_tool_catalog()["visible"])

    def tool_visible(name: str) -> bool:
        return name in visible_tools

    @mcp.tool()
    def claim_work(
        work_id: str,
        step: str | None = None,
        tier: str | None = None,
        actor: str | None = None,
        session_id: str | None = None,
        parent: str | None = None,
        module: str | None = None,
        sublane: str | None = None,
        note: str | None = None,
        display: str | None = None,
        title: str | None = None,
        acceptance: list[str] | None = None,
        depends_on: list[str] | None = None,
        done_signal: str | None = None,
        priority: int | None = None,
        write_scopes: list[str] | None = None,
    ) -> dict:
        return _tool_claim(work_id=work_id, step=step, tier=tier, actor=actor,
                           session_id=session_id, db_path=db_path,
                           parent=parent, module=module, sublane=sublane, note=note,
                           display=display, title=title, acceptance=acceptance,
                           depends_on=depends_on, done_signal=done_signal,
                           priority=priority, write_scopes=write_scopes)

    @mcp.tool()
    def classify_blocked(
        work_id: str,
        reason_class: str,
        expected_version: int,
        expected_reason_class: str | None = None,
        note: str | None = None,
        actor: str | None = None,
        session_id: str | None = None,
    ) -> dict:
        return _tool_classify_blocked(
            work_id=work_id,
            reason_class=reason_class,
            expected_version=expected_version,
            expected_reason_class=expected_reason_class,
            note=note,
            actor=actor,
            session_id=session_id,
            db_path=db_path,
        )

    @mcp.tool()
    def recover_blocked(
        work_id: str,
        note: str,
        actor: str | None = None,
        session_id: str | None = None,
    ) -> dict:
        return _tool_recover_blocked(
            work_id=work_id,
            note=note,
            actor=actor,
            session_id=session_id,
            db_path=db_path,
        )

    @mcp.tool()
    def correct_tier(
        work_id: str,
        expected_version: int,
        expected_tier: str,
        new_tier: str,
        reason: str,
        refs: list[str],
        operation_id: str,
        actor: str | None = None,
        session_id: str | None = None,
    ) -> dict:

        return _tool_correct_tier(
            work_id,
            expected_version=expected_version,
            expected_tier=expected_tier,
            new_tier=new_tier,
            reason=reason,
            refs=refs,
            operation_id=operation_id,
            actor=actor,
            session_id=session_id,
            db_path=db_path,
        )

    @mcp.tool()
    def resume_parked(
        work_id: str,
        expected_version: int,
        reason: str,
        refs: list[str],
        operation_id: str,
        actor: str | None = None,
        session_id: str | None = None,
    ) -> dict:

        return _tool_resume_parked(
            work_id,
            expected_version=expected_version,
            reason=reason,
            refs=refs,
            operation_id=operation_id,
            actor=actor,
            session_id=session_id,
            db_path=db_path,
        )

    # The five lifecycle wrappers below deliberately do not accept or forward an
    # `env`. Over MCP the server's own process environment is not the caller's
    # identity, and a client-supplied `env` would be an identity the client
    # chose for itself twice over. `_resolve_tool_identity(env=None)` therefore
    # demands an explicit actor and session_id -- the same contract `claim_work`
    # already imposes -- and that asserted identity is what the claim-ownership
    # check downstream compares against `claims.session_id`.
    @mcp.tool()
    def heartbeat(
        claim_id: str | None = None,
        work_id: str | None = None,
        actor: str | None = None,
        session_id: str | None = None,
        step: str | None = None,
    ) -> dict:
        return _tool_heartbeat(
            claim_id=claim_id,
            work_id=work_id,
            actor=actor,
            session_id=session_id,
            step=step,
            db_path=db_path,
        )

    @mcp.tool()
    def release(
        claim_id: str | None = None,
        work_id: str | None = None,
        actor: str | None = None,
        session_id: str | None = None,
        status: str = "released",
        reason: str | None = None,
        next_step: str | None = None,
        resume_when: str | None = None,
        resume_predicate: str | None = None,
        resume_manual: bool = False,
    ) -> dict:
        return _tool_release(
            claim_id=claim_id,
            work_id=work_id,
            actor=actor,
            session_id=session_id,
            status=status,
            reason=reason,
            next_step=next_step,
            resume_when=resume_when,
            resume_predicate=resume_predicate,
            resume_manual=resume_manual,
            db_path=db_path,
        )

    @mcp.tool()
    def park(
        claim_id: str | None = None,
        work_id: str | None = None,
        step: str | None = None,
        next_step: str | None = None,
        resume_when: str | None = None,
        resume_predicate: str | None = None,
        resume_manual: bool = False,
        actor: str | None = None,
        session_id: str | None = None,
    ) -> dict:
        return _tool_park(
            claim_id=claim_id,
            work_id=work_id,
            step=step,
            next_step=next_step,
            resume_when=resume_when,
            resume_predicate=resume_predicate,
            resume_manual=resume_manual,
            actor=actor,
            session_id=session_id,
            db_path=db_path,
        )

    @mcp.tool()
    def block(
        claim_id: str | None = None,
        work_id: str | None = None,
        step: str | None = None,
        next_step: str | None = None,
        resume_when: str | None = None,
        resume_predicate: str | None = None,
        resume_manual: bool = False,
        actor: str | None = None,
        session_id: str | None = None,
    ) -> dict:
        return _tool_block(
            claim_id=claim_id,
            work_id=work_id,
            step=step,
            next_step=next_step,
            resume_when=resume_when,
            resume_predicate=resume_predicate,
            resume_manual=resume_manual,
            actor=actor,
            session_id=session_id,
            db_path=db_path,
        )

    @mcp.tool()
    def complete(
        claim_id: str | None = None,
        work_id: str | None = None,
        actor: str | None = None,
        session_id: str | None = None,
        artifact_path: str | None = None,
        artifact_kind: str = "done_signal",
        note: str | None = None,
    ) -> dict:
        return _tool_complete(
            claim_id=claim_id,
            work_id=work_id,
            actor=actor,
            session_id=session_id,
            artifact_path=artifact_path,
            artifact_kind=artifact_kind,
            db_path=db_path,
            note=note,
        )

    @mcp.tool()
    def board(
        group_by: str = "module",
        limit: int = _DEFAULT_BOARD_LIMIT,
        full: bool = False,
        status: str | None = None,
        compact: bool = True,
    ) -> dict:
        return _tool_board(
            group_by=group_by,
            limit=limit,
            full=full,
            status=status,
            compact=compact,
            db_path=db_path,
        )

    if tool_visible("runs"):
        @mcp.tool()
        def runs(
            work_id: str | None = None,
            session_id: str | None = None,
            state: str | None = None,
            limit: int = 100,
        ) -> dict:
            return _tool_runs(
                work_id=work_id,
                session_id=session_id,
                state=state,
                limit=limit,
                db_path=db_path,
            )

    @mcp.tool()
    def inbox(
        actor: str | None = None,
        session_id: str | None = None,
        limit: int = 20,
        advance: bool = False,
        backlog: bool = False,
        directed_only: bool = False,
    ) -> dict:
        return _tool_inbox(actor=actor, session_id=session_id, limit=limit,
                           advance=advance, backlog=backlog,
                           directed_only=directed_only, db_path=db_path)

    @mcp.tool()
    def audit(
        kind: str,
        title: str | None = None,
        body: str | None = None,
        work_id: str | None = None,
        to_selector: str | None = None,
        payload: dict | None = None,
        verdict: str | None = None,
        operation_id: str | None = None,
        severity: str | None = None,
        refs: list[str] | None = None,
        actor: str | None = None,
        session_id: str | None = None,
    ) -> dict:
        return _tool_audit(kind=kind, title=title, body=body, work_id=work_id,
                           to_selector=to_selector, payload=payload,
                           verdict=verdict, operation_id=operation_id,
                           severity=severity, refs=refs,
                           actor=actor, session_id=session_id, db_path=db_path)

    @mcp.tool()
    def request_audit(
        work_id: str,
        task: str,
        why: str,
        refs: list[str] | None = None,
        acceptance: str | None = None,
        request_kind: str = "standard",
        actor: str | None = None,
        session_id: str | None = None,
    ) -> dict:
        return _tool_request_audit(
            work_id=work_id,
            task=task,
            why=why,
            refs=refs,
            acceptance=acceptance,
            request_kind=request_kind,
            actor=actor,
            session_id=session_id,
            db_path=db_path,
        )

    @mcp.tool()
    def declare_write_set(
        claim_id: str,
        scopes: list[str],
        actor: str | None = None,
        session_id: str | None = None,
    ) -> dict:
        """Declare which scopes a held claim intends to write.

        Scopes are "path=PREFIX", "table=NAME" or "service=NAME"; a bare value
        is a path. Advisory: this reports overlaps, it never blocks a claim.
        """
        return _tool_declare_write_set(
            claim_id=claim_id,
            scopes=scopes,
            actor=actor,
            session_id=session_id,
            db_path=db_path,
        )

    @mcp.tool()
    def route(
        usage_db: str,
        budgets: dict[str, int] | None = None,
        days: int = 7,
        require_complete: bool = False,
    ) -> dict:
        """Which provider has headroom, on measured usage. Read-only advice.

        Reads the usage ledger at usage_db; writes nothing, and refuses a path
        that does not exist rather than creating a ledger. Returns
        coverage_state "unknown" whenever coverage is not provably complete,
        and recommended null with a reason when it cannot advise at all.
        """
        return _tool_route(
            usage_db=usage_db,
            budgets=budgets,
            days=days,
            require_complete=require_complete,
        )

    @mcp.tool()
    def write_set_conflicts(include_expired: bool = False) -> dict:
        """Which currently-held claims declare overlapping write scopes.

        Read-only. Names the specific rows, sessions and overlapping scopes;
        claims that declared nothing are listed as undeclared, not as clean.
        """
        return _tool_write_set_conflicts(
            include_expired=include_expired,
            db_path=db_path,
        )

    @mcp.tool()
    def note(
        work_id: str,
        body: str,
        title: str | None = None,
        refs: list[str] | None = None,
        actor: str | None = None,
        session_id: str | None = None,
        to_session_id: str | None = None,
    ) -> dict:
        """Neutral mid-flight message about an existing row.

        Addressed to the opposite lane by default. Pass to_session_id to
        address one live session instead, which is the only way to reach a
        single member of a same-lane fleet; a dead or unknown session is
        refused rather than posted into a void.
        """
        return _tool_note(
            work_id=work_id,
            body=body,
            title=title,
            refs=refs,
            actor=actor,
            session_id=session_id,
            to_session_id=to_session_id,
            db_path=db_path,
        )

    @mcp.tool()
    def decision(
        ruling: str,
        work_id: str | None = None,
        binds: list[str] | None = None,
        scope: str = "global",
        refs: list[str] | None = None,
        actor: str | None = None,
        session_id: str | None = None,
        supersedes_event_id: int | str | None = None,
        valid_from: float | None = None,
        stale_if: list[str] | None = None,
        memory_candidate: bool = False,
    ) -> dict:
        return _tool_decision(
            ruling=ruling,
            work_id=work_id,
            binds=binds,
            scope=scope,
            refs=refs,
            actor=actor,
            session_id=session_id,
            supersedes_event_id=supersedes_event_id,
            valid_from=valid_from,
            stale_if=stale_if,
            memory_candidate=memory_candidate,
            db_path=db_path,
        )

    @mcp.tool()
    def session_closeout(
        summary: str,
        successor_hints: list[str] | None = None,
        dead_ends: list[str] | None = None,
        waived_events: list | None = None,
        ack_dirty: bool = False,
        actor: str | None = None,
        session_id: str | None = None,
    ) -> dict:
        return _tool_session_closeout(
            summary=summary,
            successor_hints=successor_hints,
            dead_ends=dead_ends,
            waived_events=waived_events,
            ack_dirty=ack_dirty,
            actor=actor,
            session_id=session_id,
            db_path=db_path,
            knowledge_db=knowledge_db,
        )

    @mcp.tool()
    def verdict(
        work_id: str,
        verdict: str,
        refs: list[str],
        operation_id: str,
        severity: str | None = None,
        title: str | None = None,
        body: str | None = None,
        actor: str | None = None,
        session_id: str | None = None,
    ) -> dict:
        return _tool_verdict(
            work_id=work_id,
            verdict=verdict,
            refs=refs,
            operation_id=operation_id,
            severity=severity,
            title=title,
            body=body,
            actor=actor,
            session_id=session_id,
            db_path=db_path,
        )

    if tool_visible("handoff_existing"):
        @mcp.tool()
        def handoff_existing(
            work_id: str,
            task: str,
            why: str,
            acceptance: str,
            owner_lane: str,
            operation_id: str,
            expected_version: int,
            expected_assignee: str,
            expected_head_event_ids: list[int],
            refs: list[str],
            constraints: list[str],
            expected_protocol_epoch: str,
            expected_server_build_sha256: str,
            expected_server_instance_id: str,
            expected_client_profile_id: str,
            target_intent: str = "queued",
            actor: str | None = None,
            session_id: str | None = None,
        ) -> dict:
            return _tool_handoff_existing(
                work_id=work_id,
                task=task,
                why=why,
                acceptance=acceptance,
                owner_lane=owner_lane,
                operation_id=operation_id,
                expected_version=expected_version,
                expected_assignee=expected_assignee,
                expected_head_event_ids=expected_head_event_ids,
                expected_protocol_epoch=expected_protocol_epoch,
                expected_server_build_sha256=expected_server_build_sha256,
                expected_server_instance_id=expected_server_instance_id,
                expected_client_profile_id=expected_client_profile_id,
                refs=refs,
                constraints=constraints,
                target_intent=target_intent,
                actor=actor,
                session_id=session_id,
                db_path=db_path,
            )

    @mcp.tool()
    def orient(
        actor: str | None = None,
        session_id: str | None = None,
        next_limit: int = 4,
        peer_limit: int = _DEFAULT_ORIENT_PEER_LIMIT,
        inbox_limit: int = _DEFAULT_ORIENT_INBOX_LIMIT,
    ) -> dict:
        return _tool_orient(
            actor=actor,
            session_id=session_id,
            next_limit=next_limit,
            peer_limit=peer_limit,
            inbox_limit=inbox_limit,
            db_path=db_path,
        )

    @mcp.tool()
    def preflight(
        ctx: Context,
        actor: str | None = None,
        limit: int = 8,
        include_rows: bool = False,
        assigned_id_limit: int = 8,
        session_id: str | None = None,
    ) -> dict:
        result = _tool_preflight(
            actor=actor,
            limit=limit,
            include_rows=include_rows,
            assigned_id_limit=assigned_id_limit,
            session_id=session_id,
            db_path=db_path,
            client_transport=_extract_mcp_client_transport(ctx),
        )
        result["provider_first_binding"] = provider_binding
        return result

    @mcp.tool()
    def next_work(
        actor: str | None = None,
        limit: int = 10,
        include_blocked: bool = False,
        include_details: bool = False,
    ) -> dict:
        return _tool_next_work(
            actor=actor,
            limit=limit,
            include_blocked=include_blocked,
            include_details=include_details,
            db_path=db_path,
        )

    @mcp.tool()
    def work_context(
        work_id: str,
        actor: str | None = None,
        event_limit: int = 8,
        include_event_details: bool = False,
    ) -> dict:
        return _tool_work_context(
            work_id=work_id,
            actor=actor,
            event_limit=event_limit,
            include_event_details=include_event_details,
            db_path=db_path,
        )

    @mcp.tool()
    def event_context(event_id: int | str, include_raw: bool = False) -> dict:
        return _tool_event_context(event_id=event_id, include_raw=include_raw, db_path=db_path)

    @mcp.tool()
    def get_decision_context(
        work_id: str | None = None,
        scope: str | None = None,
        as_of: float | None = None,
    ) -> dict:
        return _tool_get_decision_context(
            work_id=work_id, scope=scope, as_of=as_of, db_path=db_path,
        )

    @mcp.tool()
    def inbox_recent(
        actor: str | None = None,
        session_id: str | None = None,
        limit: int = _DEFAULT_INBOX_RECENT_LIMIT,
        exclude_noise: bool = True,
    ) -> dict:
        return _tool_inbox_recent(
            actor=actor,
            session_id=session_id,
            limit=limit,
            exclude_noise=exclude_noise,
            db_path=db_path,
        )

    @mcp.tool(structured_output=False)
    def knowledge_search(
        query: str,
        limit: int | None = None,
        profile: str = "brief",
        work_id: str | None = None,
        manual: bool = False,
        sources: list[str] | None = None,
        include_diagnostics: bool = False,
    ) -> str:
        response = _tool_knowledge_search(
            query=query,
            limit=limit,
            profile=profile,
            work_id=work_id,
            manual=manual,
            sources=sources,
            include_diagnostics=include_diagnostics,
            knowledge_db=knowledge_db,
            context_db=context_db,
        )
        return _compact_json_text(response)

    @mcp.tool()
    def read_note(pointer: str, max_bytes: int = DEFAULT_POINTER_READ_BYTES) -> dict:
        return _tool_read_note(pointer=pointer, max_bytes=max_bytes, context_db=context_db)

    @mcp.tool()
    def facts_lookup(
        query: str,
        mode: str = "auto",
        module: str | None = None,
        status: str | None = None,
        limit: int = 20,
        include_evidence: bool = False,
        evidence_max_bytes: int = 800,
    ) -> dict:
        return _tool_facts_lookup(
            query=query,
            mode=mode,
            module=module,
            status=status,
            limit=limit,
            include_evidence=include_evidence,
            evidence_max_bytes=evidence_max_bytes,
            knowledge_db=knowledge_db,
            context_db=context_db,
        )

    @mcp.tool()
    def facts_query(
        module: str | None = None,
        status: str | None = None,
        text: str | None = None,
        limit: int = read_surface.DEFAULT_FACTS_LIMIT,
    ) -> dict:
        """Structured (non-fuzzy) filter over the fact ledger.

        Exact match on module/status; optional text delegates to ranked search.
        The reply reports match_mode so ranked hits are never read as exact ones.
        """
        return _tool_facts_query(
            module=module,
            status=status,
            text=text,
            limit=limit,
            knowledge_db=knowledge_db,
        )

    @mcp.tool()
    def knowledge_index_status(
        use_manifest: bool = False,
        scan_fallback: bool = True,
    ) -> dict:
        """Freshness/health of the KFTS document index backing knowledge_search."""
        return _tool_knowledge_index_status(
            use_manifest=use_manifest,
            scan_fallback=scan_fallback,
            knowledge_db=knowledge_db,
        )

    @mcp.tool()
    def memory_proposals_list(
        status: str | None = None,
        kind: str | None = None,
        limit: int = read_surface.DEFAULT_PROPOSALS_LIMIT,
    ) -> dict:
        """List queued memory proposals awaiting human review."""
        return _tool_memory_proposals_list(
            status=status,
            kind=kind,
            limit=limit,
            knowledge_db=knowledge_db,
        )

    @mcp.tool()
    def memory_proposals_get(id: str) -> dict:
        """Fetch one memory proposal by id."""
        return _tool_memory_proposals_get(id=id, knowledge_db=knowledge_db)

    return mcp


_ENTRY_USAGE = """usage: coord-mcp [--help] [--version]

Serve the coordination control plane over MCP on stdio.

The server speaks MCP on stdin/stdout and takes no other arguments; it is
configured entirely by environment. The database it resolved is printed to
stderr at startup so a misrouted COORD_DB is visible before the first tool
call rather than after it.

environment:
  COORD_DB, COORD_COORD_DB   override the coord.db location
  COORD_CLAIM_STRICT         1 = refuse claims on rows missing
                             done_signal/acceptance on every surface,
                             0 = warn and proceed on every surface
"""


def main(argv: list[str] | None = None) -> int:
    import sys

    from coordharness import __version__
    from coordharness.bootstrap import bootstrap_database

    args = list(sys.argv[1:] if argv is None else argv)
    if "--help" in args or "-h" in args:
        print(_ENTRY_USAGE, end="")
        return 0
    if "--version" in args or "-V" in args:
        print(f"coord-mcp {__version__}")
        return 0
    if args:
        # stdio is the protocol channel, so a usage error goes to stderr and
        # the exit code, never to stdout.
        print(f"coord-mcp: unknown argument {args[0]!r}", file=sys.stderr)
        print(_ENTRY_USAGE, end="", file=sys.stderr)
        return 2

    db_override = os.environ.get("COORD_COORD_DB") or None
    bootstrap_database(db_override)
    print(
        f"coord-mcp {__version__}: COORD_DB={harness_config.coord_db_path()}",
        file=sys.stderr,
        flush=True,
    )
    mcp = build_server(**_provider_first_args_from_env())
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
