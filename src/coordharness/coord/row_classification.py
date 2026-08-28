from __future__ import annotations

import json
import os
import re
from typing import Any


_UUID_ONLY_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_THREAD_ID_RE = re.compile(r"019e[0-9a-f-]{20,}", re.I)
_HEX_ID_RE = re.compile(r"[0-9a-f]{12,40}", re.I)
_JOB_OBSERVER_RE = re.compile(r"job:[0-9a-f]{10,16}", re.I)
_TECHNICAL_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[_-][a-z0-9]+)+", re.I)
_OBS_NOTE_RE = re.compile(r"\b(?:job_id|run_id|name)\s*=\s*obs[_:-]?[a-z0-9_-]*", re.I)

_CONTROL_PLANE_MODULES = {
    "ops",
    "harness",
    "agent-runtime",
    "coordination",
    "context",
    "context-memory",
    "memory-context",
    "coord-memory-verifier",
    "orchestration",
    "dashboard",
    "runtime-telemetry",
}
_CONTROL_PLANE_DOMAINS = {"ops", "harness", "control_plane"}
_SEMANTIC_SYSTEMS = {"control_plane", "product", "provenance", "shared", "telemetry"}


def _configured_tokens(name: str) -> set[str]:
    return {
        token.strip().lower()
        for token in os.environ.get(name, "").split(",")
        if token.strip()
    }


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, sort_keys=True, ensure_ascii=True)
        except TypeError:
            return str(value)
    return str(value)


def row_id(row: dict[str, Any]) -> str:
    return _text(row.get("work_id") or row.get("id") or row.get("roadmap_id")).strip()


def has_active_claim(row: dict[str, Any]) -> bool:
    return _text(row.get("claim_status")).lower() in {"running", "paused", "blocked"}


def has_non_generated_label(row: dict[str, Any], work_id: str | None = None) -> bool:
    work_id = (work_id or row_id(row)).strip()
    generated = {work_id.lower()}
    if work_id.lower().startswith("job:"):
        generated.add(work_id[4:].lower())
    for key in ("title", "display"):
        value = _text(row.get(key)).strip().lower()
        if value and value not in generated:
            return True
    return False


def has_operator_metadata(row: dict[str, Any]) -> bool:
    for key in (
        "assignee",
        "assigned_by",
        "done_signal",
        "blocked_reason_class",
        "parent_id",
    ):
        if _text(row.get(key)).strip():
            return True
    if row.get("has_artifact") or row.get("artifact_exists"):
        return True
    acceptance = _text(row.get("acceptance_json")).strip()
    return bool(acceptance and acceptance not in {"[]", "{}", "null", "None"})


def is_observer_import(row: dict[str, Any]) -> bool:
    work_id = row_id(row).lower()
    note = _text(row.get("note")).lower()
    return work_id.startswith("job:obs_") or (
        "legacy jobs.db import" in note and bool(_OBS_NOTE_RE.search(note))
    )


def is_technical_shadow_id(work_id: str) -> bool:
    lower_id = work_id.lower()
    return bool(
        _JOB_OBSERVER_RE.fullmatch(lower_id)
        or _THREAD_ID_RE.fullmatch(lower_id)
        or _UUID_ONLY_RE.fullmatch(lower_id)
        or _HEX_ID_RE.fullmatch(lower_id)
        or _TECHNICAL_TOKEN_RE.fullmatch(lower_id)
    )


def is_telemetry_orphan(row: dict[str, Any]) -> bool:
    if has_active_claim(row):
        return False
    work_id = row_id(row)
    if not work_id:
        return False
    if is_observer_import(row):
        return not (has_operator_metadata(row) or has_non_generated_label(row, work_id))
    visibility = _text(row.get("visibility") or "").strip().lower()
    if visibility in {"hidden", "diagnostic", "internal", "session"} and not has_operator_metadata(row):
        return True
    note = _text(row.get("note")).lower()
    if "legacy jobs.db import" in note or "quarantined" in note:
        if is_technical_shadow_id(work_id) and not (has_operator_metadata(row) or has_non_generated_label(row, work_id)):
            return True
    return False


def derive_semantic_system(row: dict[str, Any]) -> str:
    declared = _text(row.get("semantic_system")).strip().lower()
    if declared:
        if declared not in _SEMANTIC_SYSTEMS:
            raise ValueError(f"unsupported semantic_system {declared!r}")
        return declared
    if is_telemetry_orphan(row):
        return "telemetry"
    module = _text(row.get("module") or row.get("module_key")).strip().lower()
    domain = _text(row.get("domain") or row.get("epic") or row.get("domain_id")).strip().lower()
    surface = _text(row.get("surface") or row.get("row_kind") or row.get("work_kind")).strip().lower()
    origin = _text(row.get("origin") or row.get("source")).strip().lower()
    if surface in {"local", "local_job"} or origin in {"job_progress", "local_process", "sidecar", "process"}:
        return "telemetry"
    if module in _CONTROL_PLANE_MODULES or domain in _CONTROL_PLANE_DOMAINS:
        return "control_plane"
    product_modules = _configured_tokens("COORD_PRODUCT_MODULES")
    product_domains = _configured_tokens("COORD_PRODUCT_DOMAINS")
    if module in product_modules or domain in product_domains:
        return "product"
    if module in {"docs", "guide", "strategy"} or domain in {"docs", "strategy"}:
        return "provenance"
    return "shared"
