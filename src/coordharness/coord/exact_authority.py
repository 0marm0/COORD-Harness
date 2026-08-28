
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping


MIGRATION_VERSION = 2
MIGRATION_NAME = "coord_v2_exact_authority"
MIGRATION_PATH = Path(__file__).resolve().parent / "migrations" / "002_exact_authority.sql"
BASE_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
MANIFEST_SCHEMA = "coord-exact-authority-manifest.v1"
AUTHORITY_SCHEMA_EPOCH = "coord-exact-authority.r1"
SOURCE_SCHEMA_EPOCH = "coord-source-seq.r1"
AUTHORITY_KINDS = ("plane", "lineage", "value_pin")
EXACT_PLANES = frozenset({"product", "harness", "infrastructure", "shared"})
RECORD_KINDS = frozenset(
    {"work", "telemetry", "fact", "decision", "artifact", "memory", "document", "code", "conversation"}
)
QUARANTINE_DECLARATION_POLICY = {
    "schema": "coord-new-work-authority-declaration.v1",
    "classification_state": "needs_review",
    "subject_plane": None,
    "record_kind": "work",
    "policy": "unknown_or_mixed_never_defaults_to_shared",
}

_V2_SCHEMA_OBJECT_NAMES = frozenset({
    "coord_authority_generations", "coord_authority_heads", "coord_authority_policy",
    "coord_authority_receipts", "coord_authority_versions", "coord_source_state",
    "v_coord_exact_authority_heads",
    "coord_authority_generations_no_delete", "coord_authority_generations_no_update",
    "coord_authority_heads_consistent_insert", "coord_authority_heads_consistent_update",
    "coord_authority_policy_no_delete", "coord_authority_policy_no_insert",
    "coord_authority_receipts_no_delete", "coord_authority_receipts_no_update",
    "coord_authority_versions_no_delete", "coord_authority_versions_no_update",
    "coord_exact_new_work_declaration", "coord_exact_new_work_materialize_heads",
    "coord_source_state_guard_update", "coord_source_state_no_delete",
    "coord_seq_agent_sessions_ad", "coord_seq_agent_sessions_ai", "coord_seq_agent_sessions_au",
    "coord_seq_artifacts_ad", "coord_seq_artifacts_ai", "coord_seq_artifacts_au",
    "coord_seq_authority_generations_ai", "coord_seq_authority_heads_ad",
    "coord_seq_authority_heads_ai", "coord_seq_authority_heads_au",
    "coord_seq_authority_policy_au", "coord_seq_authority_versions_ai",
    "coord_seq_claims_ad", "coord_seq_claims_ai", "coord_seq_claims_au",
    "coord_seq_display_titles_ad", "coord_seq_display_titles_ai", "coord_seq_display_titles_au",
    "coord_seq_events_ad", "coord_seq_events_ai", "coord_seq_events_au",
    "coord_seq_runs_ad", "coord_seq_runs_ai", "coord_seq_runs_au",
    "coord_seq_work_items_ad", "coord_seq_work_items_ai", "coord_seq_work_items_au",
})


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _migration_checksum() -> str:
    return sha256_file(MIGRATION_PATH)


def _execute_sql_script(conn: sqlite3.Connection, sql: str) -> None:

    pending = ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            pending = ""
            if statement:
                conn.execute(statement)
    if pending.strip():
        raise RuntimeError("incomplete exact-authority migration statement")


def _schema_object_hashes(conn: sqlite3.Connection) -> dict[str, str]:
    placeholders = ",".join("?" for _ in _V2_SCHEMA_OBJECT_NAMES)
    rows = conn.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL "
        f"AND name IN ({placeholders}) ORDER BY type,name",
        tuple(sorted(_V2_SCHEMA_OBJECT_NAMES)),
    ).fetchall()
    return {
        f"{row[0]}:{row[1]}": sha256_bytes(str(row[2]).encode("utf-8"))
        for row in rows
    }


def expected_schema_object_hashes() -> dict[str, str]:
    fixture = sqlite3.connect(":memory:", isolation_level=None)
    try:
        fixture.executescript(BASE_SCHEMA_PATH.read_text(encoding="utf-8"))
        fixture.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))
        return _schema_object_hashes(fixture)
    finally:
        fixture.close()


def apply_exact_authority_schema(conn: sqlite3.Connection) -> dict[str, Any]:

    checksum = _migration_checksum()
    row = conn.execute(
        "SELECT name, checksum FROM schema_migrations WHERE version=?",
        (MIGRATION_VERSION,),
    ).fetchone()
    if row is not None:
        stored_name, stored_checksum = str(row[0]), str(row[1])
        if stored_name != MIGRATION_NAME or stored_checksum != checksum:
            raise RuntimeError(
                "coord exact-authority migration ledger mismatch: "
                f"{stored_name}/{stored_checksum} != {MIGRATION_NAME}/{checksum}"
            )
        return verify_exact_authority_schema(conn, migration_applied=False)

    work_columns = {
        str(info[1]) for info in conn.execute("PRAGMA table_info(work_items)").fetchall()
    }
    if "authority_declaration_json" in work_columns:
        raise RuntimeError(
            "work_items authority column exists without migration v2 ledger row; "
            "refusing ambiguous repair"
        )

    own_transaction = not conn.in_transaction
    try:
        if own_transaction:
            conn.execute("BEGIN IMMEDIATE")
        _execute_sql_script(conn, MIGRATION_PATH.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations(version,name,applied_at,checksum) "
            "VALUES (?,?,(julianday('now')-2440587.5)*86400.0,?)",
            (MIGRATION_VERSION, MIGRATION_NAME, checksum),
        )
        result = verify_exact_authority_schema(conn, migration_applied=True)
        if own_transaction:
            conn.execute("COMMIT")
    except BaseException:
        if own_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    return result


def verify_exact_authority_schema(
    conn: sqlite3.Connection, *, migration_applied: bool | None = None
) -> dict[str, Any]:
    required_tables = {
        "coord_authority_policy",
        "coord_authority_generations",
        "coord_authority_receipts",
        "coord_authority_versions",
        "coord_authority_heads",
        "coord_source_state",
    }
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    missing = required_tables - tables
    if missing:
        raise RuntimeError(f"exact-authority schema incomplete: {sorted(missing)}")
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(work_items)").fetchall()
    }
    if "authority_declaration_json" not in columns:
        raise RuntimeError("work_items.authority_declaration_json is missing")
    triggers = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'coord_%'"
        ).fetchall()
    }
    required_triggers = {
        "coord_exact_new_work_declaration",
        "coord_authority_versions_no_update",
        "coord_authority_versions_no_delete",
        "coord_authority_generations_no_update",
        "coord_authority_generations_no_delete",
        "coord_authority_heads_consistent_insert", "coord_authority_heads_consistent_update",
        "coord_source_state_no_delete",
        "coord_seq_work_items_ai",
        "coord_seq_work_items_au",
        "coord_seq_work_items_ad",
        "coord_seq_claims_ai",
        "coord_seq_claims_au",
        "coord_seq_claims_ad",
        "coord_seq_events_ai",
        "coord_seq_events_au",
        "coord_seq_events_ad",
        "coord_seq_authority_heads_ai",
        "coord_seq_authority_heads_au",
        "coord_seq_authority_heads_ad",
        "coord_seq_agent_sessions_ai", "coord_seq_agent_sessions_au", "coord_seq_agent_sessions_ad",
        "coord_seq_runs_ai", "coord_seq_runs_au", "coord_seq_runs_ad",
        "coord_seq_artifacts_ai", "coord_seq_artifacts_au", "coord_seq_artifacts_ad",
        "coord_seq_display_titles_ai", "coord_seq_display_titles_au", "coord_seq_display_titles_ad",
        "coord_seq_authority_versions_ai", "coord_seq_authority_generations_ai",
        "coord_seq_authority_policy_au",
        "coord_authority_policy_no_delete", "coord_authority_policy_no_insert",
        "coord_authority_receipts_no_update", "coord_authority_receipts_no_delete",
        "coord_source_state_guard_update", "coord_exact_new_work_materialize_heads",
    }
    if required_triggers - triggers:
        raise RuntimeError(
            f"exact-authority triggers incomplete: {sorted(required_triggers - triggers)}"
        )
    state = conn.execute(
        "SELECT source_id, source_schema_epoch, source_change_seq FROM coord_source_state "
        "WHERE source_name='context_traversal'"
    ).fetchone()
    if state is None or len(str(state[0])) != 32 or str(state[1]) != SOURCE_SCHEMA_EPOCH:
        raise RuntimeError("context traversal source singleton is malformed")
    observed_hashes = _schema_object_hashes(conn)
    expected_hashes = expected_schema_object_hashes()
    if observed_hashes != expected_hashes:
        changed = sorted(
            key
            for key in set(observed_hashes) | set(expected_hashes)
            if observed_hashes.get(key) != expected_hashes.get(key)
        )
        raise RuntimeError(f"exact-authority schema object DDL drift: {changed}")
    return {
        "migration_version": MIGRATION_VERSION,
        "migration_name": MIGRATION_NAME,
        "migration_sha256": _migration_checksum(),
        "migration_applied": migration_applied,
        "source_id": str(state[0]),
        "source_change_seq": int(state[2]),
        "trigger_count": len(required_triggers),
        "schema_object_hashes": observed_hashes,
    }


def new_work_quarantine_declaration(
    work_id: str, *, writer: str, source_kind: str
) -> str:

    core = {
        **QUARANTINE_DECLARATION_POLICY,
        "work_id": str(work_id),
        "writer": str(writer),
        "authority_source_kind": str(source_kind),
    }
    return canonical_bytes(
        {**core, "authority_source_sha256": sha256_bytes(canonical_bytes(core))}
    ).decode("utf-8")


def new_work_exact_declaration(
    work_id: str,
    *,
    subject_plane: str,
    domain: str,
    program_id: str,
    workstream_id: str,
    episode_id: str,
    span_id: str,
    pinned: bool,
    semantic_value_state: str = "unrated",
    writer: str,
) -> str:

    if subject_plane not in EXACT_PLANES:
        raise ValueError("new work requires one exact subject plane")
    if semantic_value_state != "unrated":
        raise ValueError("rated semantic value is not admitted until a typed value schema exists")
    required = {
        "domain": domain,
        "program_id": program_id,
        "workstream_id": workstream_id,
        "episode_id": episode_id,
        "span_id": span_id,
        "writer": writer,
    }
    if any(not str(value).strip() for value in required.values()):
        raise ValueError("new exact declaration requires complete domain and lineage")
    core = {
        "schema": "coord-new-work-authority-declaration.v1",
        "classification_state": "adjudicated",
        "work_id": str(work_id),
        "subject_plane": subject_plane,
        "domain": str(domain),
        "record_kind": "work",
        "program_id": str(program_id),
        "workstream_id": str(workstream_id),
        "episode_id": str(episode_id),
        "span_id": str(span_id),
        "pinned": bool(pinned),
        "semantic_value_state": semantic_value_state,
        "authority_source_kind": "creator_declaration",
        "writer": str(writer),
        "policy": "explicit_typed_authority_no_inference",
    }
    return canonical_bytes(
        {**core, "authority_source_sha256": sha256_bytes(canonical_bytes(core))}
    ).decode("utf-8")


def live_authority_adjudication_declaration(
    work_id: str,
    *,
    subject_plane: str,
    domain: str,
    program_id: str,
    workstream_id: str,
    episode_id: str,
    span_id: str,
    pinned: bool,
    writer: str,
) -> str:

    raw = new_work_exact_declaration(
        work_id,
        subject_plane=subject_plane,
        domain=domain,
        program_id=program_id,
        workstream_id=workstream_id,
        episode_id=episode_id,
        span_id=span_id,
        pinned=pinned,
        writer=writer,
    )
    declaration = json.loads(raw)
    declaration["authority_source_kind"] = "controller_adjudication"
    core = {
        key: value
        for key, value in declaration.items()
        if key != "authority_source_sha256"
    }
    return canonical_bytes(
        {**core, "authority_source_sha256": sha256_bytes(canonical_bytes(core))}
    ).decode("utf-8")


def _normalize_plane_row(row: Mapping[str, Any]) -> dict[str, Any]:
    work_id = str(row.get("work_id") or "").strip()
    plane = str(row.get("subject_plane") or "").strip()
    state = str(row.get("classification_state") or "").strip()
    if plane == "needs_review" or state == "needs_review":
        plane = "needs_review"
        state = "needs_review"
        confidence = 0.0
    elif plane in EXACT_PLANES:
        state = "adjudicated"
        confidence = float(row.get("confidence", 1.0))
    else:
        raise ValueError(f"invalid subject plane for {work_id}: {plane!r}")
    record_kind = str(row.get("record_kind") or "work").strip()
    if not work_id or record_kind not in RECORD_KINDS:
        raise ValueError(f"invalid plane authority row for {work_id!r}")
    evidence_token = str(row.get("evidence_token") or "").strip()
    if not evidence_token:
        raise ValueError(f"plane authority evidence token missing for {work_id}")
    return {
        "work_id": work_id,
        "subject_plane": plane,
        "domain": str(row.get("domain") or "").strip(),
        "record_kind": record_kind,
        "confidence": confidence,
        "evidence_token": evidence_token,
        "classification_state": state,
        "quarantined": state != "adjudicated",
        "quarantine_reason": (
            str(row.get("quarantine_reason") or "adjudicated_needs_review")
            if state != "adjudicated"
            else None
        ),
    }


def _dedupe_exact_rows(rows: Iterable[Mapping[str, Any]], *, kind: str) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for raw in rows:
        record = dict(raw)
        work_id = str(record.get("work_id") or "").strip()
        if not work_id:
            raise ValueError(f"{kind} authority row requires work_id")
        prior = records.get(work_id)
        if prior is not None and prior != record:
            raise ValueError(f"conflicting duplicate {kind} authority for {work_id}")
        records[work_id] = record
    return [records[key] for key in sorted(records)]


def build_exact_authority_manifest(
    *,
    base_manifest_path: str | Path,
    supplement_path: str | Path,
    hierarchy_spans_path: str | Path,
    hierarchy_manifest_path: str | Path,
    work_rows: Iterable[Mapping[str, Any]],
    controller_sha256: str,
    source_event_head: int,
    source_closure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:

    base_path = Path(base_manifest_path)
    supplement_file = Path(supplement_path)
    spans_path = Path(hierarchy_spans_path)
    hierarchy_path = Path(hierarchy_manifest_path)
    base = json.loads(base_path.read_text(encoding="utf-8"))
    supplement = json.loads(supplement_file.read_text(encoding="utf-8"))
    base_rows = list(base.get("rows") or [])
    supplement_rows = list(supplement.get("rows") or [])
    plane = _dedupe_exact_rows(
        [_normalize_plane_row(row) for row in [*base_rows, *supplement_rows]],
        kind="plane",
    )

    lineage_rows: list[dict[str, Any]] = []
    with spans_path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if not row.get("hierarchy_exact") or row.get("quarantined"):
                continue
            lineage_rows.append(
                {
                    "work_id": str(row["work_id"]),
                    "program_id": str(row["program_id"]),
                    "workstream_id": str(row["workstream_id"]),
                    "episode_id": str(row["episode_id"]),
                    "span_id": str(row["span_id"]),
                    "lineage_evidence": str(row.get("lineage_evidence") or ""),
                    "source_work_row_sha256": str(row.get("source_work_row_sha256") or ""),
                    "inference_used": False,
                }
            )
    lineage = _dedupe_exact_rows(lineage_rows, kind="lineage")

    value_pin_rows: list[dict[str, Any]] = []
    for raw in work_rows:
        row = dict(raw)
        work_id = str(row.get("work_id") or "").strip()
        if not work_id:
            continue
        operator_state = str(row.get("operator_state") or "").strip().lower()
        intent_state = str(row.get("intent_state") or "").strip().lower()
        value_pin_rows.append(
            {
                "work_id": work_id,
                "lifecycle_priority": int(row.get("priority") or 0),
                "blocking_state": intent_state == "blocked",
                "operator_pin_state": (
                    "pinned" if operator_state in {"pinned", "protected"} else "not_explicitly_pinned"
                ),
                "semantic_value": None,
                "semantic_value_state": "unadjudicated",
                "authority_fields": ["work_items.priority", "work_items.intent_state", "work_items.operator_state"],
                "policy": "structured_lifecycle_only; no prose_or_recency_value_inference",
            }
        )
    value_pin = _dedupe_exact_rows(value_pin_rows, kind="value_pin")

    hierarchy_manifest = json.loads(hierarchy_path.read_text(encoding="utf-8"))
    plane_states = Counter(str(row["classification_state"]) for row in plane)
    core = {
        "schema": MANIFEST_SCHEMA,
        "authority_schema_epoch": AUTHORITY_SCHEMA_EPOCH,
        "source_schema_epoch": SOURCE_SCHEMA_EPOCH,
        "controller_sha256": str(controller_sha256),
        "source_event_head": int(source_event_head),
        "source_closure": dict(source_closure or {}),
        "sources": {
            "base_plane": {"path": str(base_path), "sha256": sha256_file(base_path), "rows": len(base_rows)},
            "plane_supplement": {
                "path": str(supplement_file),
                "sha256": sha256_file(supplement_file),
                "rows": len(supplement_rows),
            },
            "hierarchy_manifest": {
                "path": str(hierarchy_path),
                "sha256": sha256_file(hierarchy_path),
                "generation_id": hierarchy_manifest.get("generation_id"),
            },
            "hierarchy_spans": {"path": str(spans_path), "sha256": sha256_file(spans_path)},
        },
        "counts": {
            "plane_source_rows": len(base_rows) + len(supplement_rows),
            "plane_distinct_work_ids": len(plane),
            "plane_exact": plane_states["adjudicated"],
            "plane_needs_review": plane_states["needs_review"],
            "lineage_exact": len(lineage),
            "value_pin_structured": len(value_pin),
        },
        "policy": {
            "unknown_or_mixed": "quarantine_never_shared",
            "semantic_value": "unadjudicated_unless_explicit",
            "new_work": "explicit_exact_declaration_or_writer_doorway_needs_review_quarantine",
            "versions": "append_only_heads_replaceable",
        },
        "query_dependency_manifest": {
            "tables": [
                "work_items", "claims", "agent_sessions", "runs", "events",
                "artifacts", "display_titles", "coord_authority_heads",
                "coord_authority_versions",
            ],
            "sequence_trigger_tables": [
                "work_items", "claims", "agent_sessions", "runs", "events",
                "artifacts", "display_titles", "coord_authority_heads",
            ],
            "immutable_join_tables": ["coord_authority_versions"],
            "migration_sha256": _migration_checksum(),
        },
        "authorities": {"plane": plane, "lineage": lineage, "value_pin": value_pin},
    }
    core_sha256 = sha256_bytes(canonical_bytes(core))
    return {"generation_id": f"coord-authority-sha256-{core_sha256}", "core_sha256": core_sha256, **core}


def validate_exact_authority_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if str(manifest.get("schema")) != MANIFEST_SCHEMA:
        raise ValueError("unsupported exact-authority manifest schema")
    core = {key: value for key, value in manifest.items() if key not in {"generation_id", "core_sha256"}}
    digest = sha256_bytes(canonical_bytes(core))
    if str(manifest.get("core_sha256")) != digest:
        raise ValueError("exact-authority manifest core hash mismatch")
    if str(manifest.get("generation_id")) != f"coord-authority-sha256-{digest}":
        raise ValueError("exact-authority generation id mismatch")
    authorities = manifest.get("authorities")
    if not isinstance(authorities, Mapping) or set(authorities) != set(AUTHORITY_KINDS):
        raise ValueError("manifest must carry plane, lineage, and value_pin authorities")
    for kind in AUTHORITY_KINDS:
        rows = authorities[kind]
        if not isinstance(rows, list):
            raise ValueError(f"{kind} authorities must be a list")
        _dedupe_exact_rows(rows, kind=kind)
    for row in authorities["plane"]:
        normalized = _normalize_plane_row(row)
        if normalized != row:
            raise ValueError(f"plane row is not canonical: {row.get('work_id')}")
    counts = dict(manifest.get("counts") or {})
    actual = {
        "plane_distinct_work_ids": len(authorities["plane"]),
        "plane_exact": sum(r["classification_state"] == "adjudicated" for r in authorities["plane"]),
        "plane_needs_review": sum(r["classification_state"] == "needs_review" for r in authorities["plane"]),
        "lineage_exact": len(authorities["lineage"]),
        "value_pin_structured": len(authorities["value_pin"]),
    }
    for key, value in actual.items():
        if int(counts.get(key, -1)) != value:
            raise ValueError(f"manifest count mismatch for {key}: {counts.get(key)} != {value}")
    return {"status": "PASS", "generation_id": manifest["generation_id"], **actual}


def audit_exact_authority_manifest(
    manifest: Mapping[str, Any], *, universe_work_ids: Iterable[str]
) -> dict[str, Any]:
    validated = validate_exact_authority_manifest(manifest)
    universe = {str(value) for value in universe_work_ids}
    authorities = manifest["authorities"]
    missing = {
        kind: sorted({str(row["work_id"]) for row in authorities[kind]} - universe)
        for kind in AUTHORITY_KINDS
    }
    if any(missing.values()):
        raise ValueError(f"authority records missing from work universe: {missing}")
    review_ids = sorted(
        str(row["work_id"])
        for row in authorities["plane"]
        if row["classification_state"] == "needs_review"
    )
    return {
        "schema": "coord-exact-authority-independent-audit.v1",
        "status": "PASS",
        "generation_id": manifest["generation_id"],
        "universe_work_ids": len(universe),
        "counts": validated,
        "coverage": {
            kind: len(authorities[kind]) / len(universe) if universe else 0.0
            for kind in AUTHORITY_KINDS
        },
        "needs_review_ids": review_ids,
        "unknown_inferred_shared": 0,
        "missing_from_universe": missing,
    }


def publish_exact_authority_manifest(
    conn: sqlite3.Connection,
    manifest: Mapping[str, Any],
    *,
    manifest_sha256: str,
    published_by: str,
    enforce_new_work_declarations: bool = True,
    manage_transaction: bool | None = None,
) -> dict[str, Any]:

    validation = validate_exact_authority_manifest(manifest)
    generation_id = str(manifest["generation_id"])
    authorities = manifest["authorities"]
    existing_work = {
        str(row[0]) for row in conn.execute("SELECT work_id FROM work_items").fetchall()
    }
    authority_work = {
        str(row["work_id"])
        for kind in AUTHORITY_KINDS
        for row in authorities[kind]
    }
    missing = sorted(authority_work - existing_work)
    if missing:
        raise ValueError(f"authority publication references missing work rows: {missing[:12]}")
    actual_manifest_sha256 = sha256_bytes(canonical_bytes(manifest))
    if manifest_sha256 != actual_manifest_sha256:
        raise ValueError(
            f"manifest_sha256 mismatch: {manifest_sha256} != {actual_manifest_sha256}"
        )
    own_transaction = (not conn.in_transaction) if manage_transaction is None else manage_transaction
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        prior = conn.execute(
            "SELECT manifest_sha256 FROM coord_authority_generations WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        if prior is not None:
            if str(prior[0]) != manifest_sha256:
                raise RuntimeError("generation id already exists with a different manifest hash")
            return {**validation, "published": False, "idempotent": True}
        now = float(
            conn.execute("SELECT (julianday('now')-2440587.5)*86400.0").fetchone()[0]
        )
        conn.execute(
            "INSERT INTO coord_authority_generations("
            "generation_id,schema_version,manifest_sha256,sources_json,counts_json,published_by,published_at"
            ") VALUES (?,?,?,?,?,?,?)",
            (
                generation_id,
                MANIFEST_SCHEMA,
                manifest_sha256,
                canonical_bytes(manifest["sources"]).decode(),
                canonical_bytes(manifest["counts"]).decode(),
                str(published_by),
                now,
            ),
        )
        live_stream_core = {
            "schema": "coord-live-declarations.r1",
            "policy": "append-only typed declarations after frozen generation activation",
        }
        live_stream_sha = sha256_bytes(canonical_bytes(live_stream_core))
        conn.execute(
            "INSERT OR IGNORE INTO coord_authority_generations("
            "generation_id,schema_version,manifest_sha256,sources_json,counts_json,published_by,published_at"
            ") VALUES ('coord-live-declarations-r1',?,?,?,?,?,?)",
            (
                "coord-live-declarations.r1",
                live_stream_sha,
                canonical_bytes(live_stream_core).decode(),
                canonical_bytes({"mode": "append_stream"}).decode(),
                str(published_by),
                now,
            ),
        )
        inserted = Counter()
        for kind in AUTHORITY_KINDS:
            for record in authorities[kind]:
                work_id = str(record["work_id"])
                payload = canonical_bytes(record).decode()
                content_sha = sha256_bytes(payload.encode())
                prior_head = conn.execute(
                    "SELECT v.head_version FROM coord_authority_heads h "
                    "JOIN coord_authority_versions v ON v.authority_version_id=h.authority_version_id "
                    "WHERE h.authority_kind=? AND h.work_id=?",
                    (kind, work_id),
                ).fetchone()
                head_version = (int(prior_head[0]) + 1) if prior_head else 1
                evidence_ref = str(
                    record.get("evidence_token")
                    or record.get("source_work_row_sha256")
                    or manifest["core_sha256"]
                )
                cursor = conn.execute(
                    "INSERT INTO coord_authority_versions("
                    "authority_kind,work_id,head_version,generation_id,payload_json,content_sha256,evidence_ref,created_at"
                    ") VALUES (?,?,?,?,?,?,?,?)",
                    (kind, work_id, head_version, generation_id, payload, content_sha, evidence_ref, now),
                )
                version_id = int(cursor.lastrowid)
                conn.execute(
                    "INSERT INTO coord_authority_heads("
                    "authority_kind,work_id,authority_version_id,generation_id,content_sha256,updated_at"
                    ") VALUES (?,?,?,?,?,?) ON CONFLICT(authority_kind,work_id) DO UPDATE SET "
                    "authority_version_id=excluded.authority_version_id,generation_id=excluded.generation_id,"
                    "content_sha256=excluded.content_sha256,updated_at=excluded.updated_at",
                    (kind, work_id, version_id, generation_id, content_sha, now),
                )
                inserted[kind] += 1
        conn.execute(
            "UPDATE coord_authority_policy SET enforcement_mode=?, active_generation=?, updated_at=? "
            "WHERE policy_id='exact_authority'",
            ("enforce" if enforce_new_work_declarations else "audit", generation_id, now),
        )
        if own_transaction:
            conn.execute("COMMIT")
    except BaseException:
        if own_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    state = conn.execute(
        "SELECT source_id,source_change_seq FROM coord_source_state WHERE source_name='context_traversal'"
    ).fetchone()
    return {
        **validation,
        "published": True,
        "idempotent": False,
        "inserted_versions": dict(inserted),
        "enforcement_mode": "enforce" if enforce_new_work_declarations else "audit",
        "source_id": str(state[0]),
        "source_change_seq": int(state[1]),
    }


def store_authority_receipt(
    conn: sqlite3.Connection,
    *,
    receipt_id: str,
    receipt_kind: str,
    generation_id: str | None,
    payload: Mapping[str, Any],
) -> dict[str, Any]:

    if receipt_kind not in {"activation", "rollback"}:
        raise ValueError("invalid authority receipt kind")
    raw = canonical_bytes(payload)
    state = conn.execute(
        "SELECT source_id,source_change_seq FROM coord_source_state "
        "WHERE source_name='context_traversal'"
    ).fetchone()
    now = float(
        conn.execute("SELECT (julianday('now')-2440587.5)*86400.0").fetchone()[0]
    )
    conn.execute(
        "INSERT INTO coord_authority_receipts("
        "receipt_id,receipt_kind,generation_id,source_id,source_change_seq,"
        "receipt_json,receipt_sha256,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            receipt_id,
            receipt_kind,
            generation_id,
            str(state[0]),
            int(state[1]),
            raw.decode(),
            sha256_bytes(raw),
            now,
        ),
    )
    return {
        "receipt_id": receipt_id,
        "receipt_kind": receipt_kind,
        "generation_id": generation_id,
        "source_id": str(state[0]),
        "source_change_seq": int(state[1]),
        "receipt_sha256": sha256_bytes(raw),
    }


def rollback_authority_activation(
    conn: sqlite3.Connection,
    *,
    expected_generation_id: str,
    expected_source_change_seq: int,
    target_generation_id: str | None,
    receipt_id: str,
    actor: str,
) -> dict[str, Any]:

    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        policy = conn.execute(
            "SELECT active_generation FROM coord_authority_policy "
            "WHERE policy_id='exact_authority'"
        ).fetchone()
        state = conn.execute(
            "SELECT source_id,source_change_seq FROM coord_source_state "
            "WHERE source_name='context_traversal'"
        ).fetchone()
        if policy is None or str(policy[0] or "") != expected_generation_id:
            raise RuntimeError("rollback generation CAS failed")
        if int(state[1]) != int(expected_source_change_seq):
            raise RuntimeError("rollback source sequence CAS failed; intervening writes exist")
        prior_counts = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT authority_kind,COUNT(*) FROM coord_authority_heads GROUP BY authority_kind"
            ).fetchall()
        }
        conn.execute("DELETE FROM coord_authority_heads")
        if target_generation_id:
            exists = conn.execute(
                "SELECT 1 FROM coord_authority_generations WHERE generation_id=?",
                (target_generation_id,),
            ).fetchone()
            if exists is None:
                raise RuntimeError("rollback target generation does not exist")
            conn.execute(
                "INSERT INTO coord_authority_heads("
                "authority_kind,work_id,authority_version_id,generation_id,content_sha256,updated_at"
                ") SELECT authority_kind,work_id,authority_version_id,generation_id,content_sha256,"
                "(julianday('now')-2440587.5)*86400.0 FROM coord_authority_versions "
                "WHERE generation_id=?",
                (target_generation_id,),
            )
        conn.execute(
            "UPDATE coord_authority_policy SET enforcement_mode=?,active_generation=?,"
            "updated_at=(julianday('now')-2440587.5)*86400.0 "
            "WHERE policy_id='exact_authority'",
            ("enforce" if target_generation_id else "audit", target_generation_id),
        )
        new_state = conn.execute(
            "SELECT source_id,source_change_seq FROM coord_source_state "
            "WHERE source_name='context_traversal'"
        ).fetchone()
        payload = {
            "schema": "coord-exact-authority-rollback-receipt.v1",
            "status": "PASS",
            "actor": actor,
            "rolled_back_generation": expected_generation_id,
            "target_generation": target_generation_id,
            "prior_head_counts": prior_counts,
            "source_id": str(new_state[0]),
            "source_change_seq": int(new_state[1]),
            "history_deleted": False,
        }
        stored = store_authority_receipt(
            conn,
            receipt_id=receipt_id,
            receipt_kind="rollback",
            generation_id=target_generation_id,
            payload=payload,
        )
        if own_transaction:
            conn.execute("COMMIT")
    except BaseException:
        if own_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    return {**payload, "stored_receipt": stored}
