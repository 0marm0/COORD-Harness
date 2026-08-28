
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping

from coordharness import config as _harness_config
from coordharness.coord import coord_db
from coordharness.coord import work_query_v2 as pure_core
from coordharness.coord.config import connect_ro
from coordharness.coord.exact_authority import (
    AUTHORITY_KINDS,
    AUTHORITY_SCHEMA_EPOCH,
    EXACT_PLANES,
    RECORD_KINDS,
    SOURCE_SCHEMA_EPOCH,
    canonical_bytes,
)
from coordharness.coord.r4_plane_authority import R4PlaneAuthority
from coordharness.coord.work_query_v2 import ResponseBudget, WorkFilters


SNAPSHOT_SCHEMA = "coord-exact-query-snapshot.v1"
QUERY_CORE_ADAPTER_VERSION = "exact-query-core.r1"


class ExactQueryCoreError(RuntimeError):
    pass


_LIVE_SUCCESSOR_GENERATION_RE = re.compile(
    r"coord-live-declarations-r1:([a-f0-9]{32})"
)
_LIVE_ADJUDICATION_REQUEST_KEYS = (
    "schema_version",
    "writer_contract",
    "work_id",
    "actor",
    "session_id",
    "operation_id",
    "expected_plane_head_sha256",
    "compensates_plane_head_sha256",
    "declaration",
)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _source_sha256(path: Path) -> str:
    return _sha256(path.read_bytes())


def _validate_live_successor_generation(
    conn,
    *,
    generation_row,
    kind: str,
    work_id: str,
    payload: Mapping[str, Any],
    content_sha256: str,
) -> None:

    generation_id = str(generation_row["h_generation"] or "")
    match = _LIVE_SUCCESSOR_GENERATION_RE.fullmatch(generation_id)
    if match is None:
        raise ExactQueryCoreError(
            f"{kind} head has malformed live-successor generation for {work_id}"
        )
    if str(generation_row["generation_schema_version"] or "") != "coord-live-declarations.r1":
        raise ExactQueryCoreError(
            f"{kind} live-successor generation schema mismatch for {work_id}"
        )
    manifest_sha = str(generation_row["generation_manifest_sha256"] or "").lower()
    if not re.fullmatch(r"[a-f0-9]{64}", manifest_sha) or match.group(1) != manifest_sha[:32]:
        raise ExactQueryCoreError(
            f"{kind} live-successor generation id/hash mismatch for {work_id}"
        )
    try:
        sources = json.loads(str(generation_row["generation_sources_json"] or ""))
    except (TypeError, ValueError) as exc:
        raise ExactQueryCoreError(
            f"{kind} live-successor sources are malformed for {work_id}"
        ) from exc
    if not isinstance(sources, dict) or sources != {
        "operation_id": sources.get("operation_id") if isinstance(sources, dict) else None,
        "request_sha256": manifest_sha,
        "stream": "coord-live-declarations-r1",
    }:
        raise ExactQueryCoreError(
            f"{kind} live-successor source binding mismatch for {work_id}"
        )
    operation_id = str(sources.get("operation_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,199}", operation_id):
        raise ExactQueryCoreError(
            f"{kind} live-successor operation id is malformed for {work_id}"
        )
    event_rows = conn.execute(
        "SELECT actor,session_id,payload_json FROM events"
        " WHERE kind='authority_adjudication' AND work_id=?"
        " AND json_extract(payload_json,'$.generation_id')=?"
        " AND json_extract(payload_json,'$.request_sha256')=?",
        (work_id, generation_id, manifest_sha),
    ).fetchall()
    if len(event_rows) != 1:
        raise ExactQueryCoreError(
            f"{kind} live-successor receipt cardinality mismatch for {work_id}"
        )
    event = event_rows[0]
    try:
        receipt = json.loads(str(event["payload_json"] or ""))
    except (TypeError, ValueError) as exc:
        raise ExactQueryCoreError(
            f"{kind} live-successor receipt is malformed for {work_id}"
        ) from exc
    if not isinstance(receipt, dict):
        raise ExactQueryCoreError(
            f"{kind} live-successor receipt is not an object for {work_id}"
        )
    request = {key: receipt.get(key) for key in _LIVE_ADJUDICATION_REQUEST_KEYS}
    if _sha256(canonical_bytes(request)) != manifest_sha:
        raise ExactQueryCoreError(
            f"{kind} live-successor request hash mismatch for {work_id}"
        )
    if (
        receipt.get("generation_id") != generation_id
        or receipt.get("operation_id") != operation_id
        or receipt.get("work_id") != work_id
        or receipt.get("declaration") != dict(payload)
        or not isinstance(receipt.get("new_heads"), dict)
        or receipt["new_heads"].get(kind) != content_sha256
        or receipt.get("actor") != event["actor"]
        or receipt.get("session_id") != event["session_id"]
        or str(generation_row["generation_published_by"] or "") != str(event["session_id"] or "")
        or payload.get("authority_source_kind") != "controller_adjudication"
    ):
        raise ExactQueryCoreError(
            f"{kind} live-successor payload/receipt binding mismatch for {work_id}"
        )


QUERY_CORE_BUILD_MANIFEST = {
    "schema": "coord-query-core-build.v1",
    "adapter_version": QUERY_CORE_ADAPTER_VERSION,
    "pure_schema_version": pure_core.SCHEMA_VERSION,
    "ranking_version": pure_core.RANKING_VERSION,
    "pure_core_source_sha256": _source_sha256(Path(pure_core.__file__).resolve()),
    "authority_schema_epoch": AUTHORITY_SCHEMA_EPOCH,
    "source_schema_epoch": SOURCE_SCHEMA_EPOCH,
}
QUERY_CORE_BUILD_SHA256 = _sha256(canonical_bytes(QUERY_CORE_BUILD_MANIFEST))

CANONICAL_COORD_DB = _harness_config.coord_db_path()


def _object_payload(raw: Any, *, kind: str, work_id: str) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise ExactQueryCoreError(
            f"{kind} authority payload is invalid JSON for {work_id}"
        ) from exc
    if not isinstance(value, dict):
        raise ExactQueryCoreError(f"{kind} authority payload is not an object for {work_id}")
    if str(value.get("work_id") or "") != work_id:
        raise ExactQueryCoreError(f"{kind} authority payload work_id mismatch for {work_id}")
    return value


def _validate_payload(kind: str, payload: Mapping[str, Any], work_id: str) -> None:
    if kind == "plane":
        state = str(payload.get("classification_state") or "")
        plane = str(payload.get("subject_plane") or "")
        if state == "adjudicated" and plane not in EXACT_PLANES:
            raise ExactQueryCoreError(f"plane authority is not exact for {work_id}")
        if state == "needs_review" and plane not in {"", "needs_review"}:
            raise ExactQueryCoreError(f"needs-review plane is malformed for {work_id}")
        if state not in {"adjudicated", "needs_review"}:
            raise ExactQueryCoreError(f"plane classification_state is invalid for {work_id}")
        if str(payload.get("record_kind") or "") not in RECORD_KINDS:
            raise ExactQueryCoreError(f"plane record_kind is invalid for {work_id}")
        return
    if kind == "lineage":
        required = ("program_id", "workstream_id", "episode_id", "span_id")
        if any(not str(payload.get(field) or "").strip() for field in required):
            raise ExactQueryCoreError(f"lineage authority is incomplete for {work_id}")
        typed_declared = (
            payload.get("authority_source_kind") in {
                "creator_declaration",
                "controller_adjudication",
            }
            and payload.get("classification_state") == "adjudicated"
            and payload.get("policy") == "explicit_typed_authority_no_inference"
        )
        if payload.get("inference_used") is not False and not typed_declared:
            raise ExactQueryCoreError(f"lineage authority permits inference for {work_id}")
        return
    if kind == "value_pin":
        frozen_value = (
            "operator_pin_state" in payload and "semantic_value_state" in payload
        )
        typed_value = (
            payload.get("authority_source_kind") in {
                "creator_declaration",
                "controller_adjudication",
            }
            and payload.get("classification_state") == "adjudicated"
            and isinstance(payload.get("pinned"), bool)
            and payload.get("semantic_value_state") == "unrated"
            and payload.get("policy") == "explicit_typed_authority_no_inference"
        )
        if not frozen_value and not typed_value:
            raise ExactQueryCoreError(f"value/pin authority is incomplete for {work_id}")
        return
    raise ExactQueryCoreError(f"unsupported authority kind {kind!r}")


def _derived_status(row: dict[str, Any], *, snapshot_at: float) -> str:
    return coord_db.derive_work_status(row, snapshot_at)


def _pin_value(payload: Mapping[str, Any]) -> bool:
    if isinstance(payload.get("pinned"), bool):
        return bool(payload["pinned"])
    value = payload.get("operator_pin_state")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {
        "pinned",
        "explicitly_pinned",
        "operator_pinned",
    }


def _semantic_value(payload: Mapping[str, Any]) -> tuple[bool, int]:
    if str(payload.get("semantic_value_state") or "") != "adjudicated":
        return False, 0
    value = payload.get("semantic_value")
    if isinstance(value, bool):
        return False, 0
    try:
        score = max(0, min(int(value), 100))
    except (TypeError, ValueError):
        return False, 0
    return score >= 80, score


def _plane_authority(records: Iterable[Mapping[str, Any]]) -> R4PlaneAuthority:
    rows = []
    for record in records:
        payload = dict(record)
        if payload.get("classification_state") == "needs_review":
            payload["subject_plane"] = "needs_review"
        rows.append(
            {
                key: payload.get(key)
                for key in (
                    "work_id",
                    "subject_plane",
                    "domain",
                    "record_kind",
                    "confidence",
                    "evidence_token",
                )
            }
        )
        rows[-1]["confidence"] = payload.get(
            "confidence",
            0.0 if payload.get("classification_state") == "needs_review" else 1.0,
        )
        rows[-1]["evidence_token"] = payload.get("evidence_token") or (
            f"{payload.get('authority_source_kind') or 'exact_head'}:"
            f"{payload.get('authority_source_sha256') or _sha256(canonical_bytes(payload))}"
        )
    raw = canonical_bytes({"rows": sorted(rows, key=lambda row: str(row["work_id"]))})
    return R4PlaneAuthority.from_bytes(raw, expected_sha256=_sha256(raw))


@dataclass(frozen=True)
class ExactQuerySnapshot:

    source_id: str
    source_change_seq: int
    active_generation: str
    snapshot_at: float
    exact_rows: tuple[dict[str, Any], ...]
    quarantine_rows: tuple[dict[str, Any], ...]
    authority_head_counts: dict[str, int]
    _plane_authority: R4PlaneAuthority

    @property
    def build_sha256(self) -> str:
        return QUERY_CORE_BUILD_SHA256

    @property
    def ranking_version(self) -> str:
        return pure_core.RANKING_VERSION

    @property
    def source_fence(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_change_seq": self.source_change_seq,
            "source_schema_epoch": SOURCE_SCHEMA_EPOCH,
        }
    def receipt(self) -> dict[str, Any]:
        return {
            "schema": SNAPSHOT_SCHEMA,
            "query_core_build_sha256": self.build_sha256,
            "query_core_build_manifest": dict(QUERY_CORE_BUILD_MANIFEST),
            "ranking_version": self.ranking_version,
            "active_generation": self.active_generation,
            "source_fence": self.source_fence,
            "snapshot_at": self.snapshot_at,
            "exact_rows": len(self.exact_rows),
            "quarantine_rows": len(self.quarantine_rows),
            "retained_rows": len(self.exact_rows) + len(self.quarantine_rows),
            "authority_head_counts": dict(self.authority_head_counts),
            "read_contract": {
                "mode_ro": True,
                "query_only": True,
                "single_transaction": True,
                "writes": 0,
                "inference_used": False,
            },
        }

    def normalized_rows(self, *, include_quarantine: bool = True) -> list[dict[str, Any]]:
        rows = [json.loads(json.dumps(row)) for row in self.exact_rows]
        if include_quarantine:
            rows.extend(json.loads(json.dumps(row)) for row in self.quarantine_rows)
        return rows

    def query(
        self,
        *,
        filters: WorkFilters = WorkFilters(),
        budget: ResponseBudget = ResponseBudget(),
        cursor: str | None = None,
    ) -> dict[str, Any]:

        try:
            return pure_core.work_query_v2(
                self.normalized_rows(include_quarantine=True),
                filters=filters,
                budget=budget,
                source_change_seq=self.source_change_seq,
                source_id=self.source_id,
                source_change_seq_canonical=True,
                cursor=cursor,
                plane_authority=self._plane_authority,
            )
        except ValueError as exc:
            if cursor is not None and "continuation cursor" in str(exc):
                raise ExactQueryCoreError(str(exc)) from exc
            raise

    def ranked_cards(
        self,
        *,
        filters: WorkFilters = WorkFilters(),
        budget: ResponseBudget = ResponseBudget(max_wire_bytes=1_048_576, max_items=10_000),
    ) -> list[dict[str, Any]]:
        response = self.query(filters=filters, budget=budget)
        return [
            dict(card)
            for program in response["payload"]["programs"]
            for card in program["workstreams"]
        ]

    def flat_ranked_rows(self, *, include_quarantine: bool = True) -> list[dict[str, Any]]:

        cards = self.ranked_cards()
        card_keys = {
            (
                str(card["subject_plane"]),
                str(card["program_id"]),
                str(card["workstream_id"]),
            ): tuple(card["rank_signature"])
            for card in cards
        }
        ranked: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        for source in self.exact_rows:
            row = json.loads(json.dumps(source))
            key = card_keys[
                (
                    str(row["subject_plane"]),
                    str(row["program_id"]),
                    str(row["workstream_id"]),
                )
            ]
            flat_key = (0, *key, -int(row.get("updated_seq") or 0), str(row["work_id"]))
            row["query_rank_key"] = list(flat_key)
            ranked.append((flat_key, row))
        if include_quarantine:
            for source in self.quarantine_rows:
                row = json.loads(json.dumps(source))
                compatibility = coord_db.board_current_rank(row)
                flat_key = (1, *compatibility)
                row["query_rank_key"] = list(flat_key)
                ranked.append((flat_key, row))
        ranked.sort(key=lambda item: item[0])
        out = []
        for index, (_key, row) in enumerate(ranked, start=1):
            row["query_rank"] = index
            out.append(row)
        return out

    def session_capsule(
        self,
        *,
        actor: str,
        claims: Iterable[Mapping[str, Any]],
        inbox_events: Iterable[Mapping[str, Any]],
        budget: ResponseBudget = ResponseBudget(max_wire_bytes=15_360, max_items=30),
    ) -> dict[str, Any]:

        return pure_core.session_capsule_v2(
            actor=actor,
            claims=claims,
            inbox_events=inbox_events,
            work_rows=self.normalized_rows(include_quarantine=True),
            budget=budget,
            source_change_seq=self.source_change_seq,
            plane_authority=self._plane_authority,
        )

    def search_quarantine(
        self,
        *,
        query: str | None = None,
        exact_work_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:

        bounded_limit = max(0, min(int(limit), 1_000))
        needle = str(query or "").casefold().strip()
        matches = []
        for row in self.quarantine_rows:
            work_id = str(row.get("work_id") or "")
            if exact_work_id is not None and work_id != exact_work_id:
                continue
            if needle:
                haystack = "\n".join(
                    str(row.get(key) or "")
                    for key in ("work_id", "title", "display", "module", "domain")
                ).casefold()
                if needle not in haystack:
                    continue
            matches.append(row)
        matches.sort(
            key=lambda row: (
                -int(row.get("updated_seq") or 0),
                str(row.get("work_id") or ""),
            )
        )
        digest = _sha256(
            canonical_bytes(
                [
                    [row.get("work_id"), row.get("quarantine_reasons")]
                    for row in matches
                ]
            )
        )
        return {
            "schema": "coord-exact-query-quarantine-search.v1",
            "query_core_build_sha256": self.build_sha256,
            "source_fence": self.source_fence,
            "query": query,
            "exact_work_id": exact_work_id,
            "source_quarantine_rows_scanned": len(self.quarantine_rows),
            "match_count_before_cap": len(matches),
            "returned": min(len(matches), bounded_limit),
            "truncated": len(matches) > bounded_limit,
            "complete_match_digest_sha256": digest,
            "matches": [json.loads(json.dumps(row)) for row in matches[:bounded_limit]],
        }


@dataclass(frozen=True)
class LegacyFixtureQuerySnapshot:

    rows: tuple[dict[str, Any], ...]
    source_id: str = "legacy-noncanonical-fixture"
    source_change_seq: int = 0
    exact_rows: tuple[dict[str, Any], ...] = ()

    @property
    def quarantine_rows(self) -> tuple[dict[str, Any], ...]:
        return self.rows

    def receipt(self) -> dict[str, Any]:
        return {
            "schema": "coord-exact-query-legacy-fixture.v1",
            "mode": "legacy_noncanonical_fixture",
            "query_core_build_sha256": None,
            "retained_rows": len(self.rows),
            "exact_rows": 0,
            "quarantine_rows": len(self.rows),
            "production_eligible": False,
        }

    def flat_ranked_rows(self, *, include_quarantine: bool = True) -> list[dict[str, Any]]:
        if not include_quarantine:
            return []
        rows = [json.loads(json.dumps(row)) for row in self.rows]
        rows.sort(key=coord_db.board_current_rank)
        for index, row in enumerate(rows, start=1):
            row["query_rank"] = index
            row["query_rank_key"] = list(coord_db.board_current_rank(row))
            row["query_core_mode"] = "legacy_noncanonical_fixture"
        return rows

    def session_capsule(
        self,
        *,
        actor: str,
        claims: Iterable[Mapping[str, Any]],
        inbox_events: Iterable[Mapping[str, Any]],
        budget: ResponseBudget = ResponseBudget(max_wire_bytes=15_360, max_items=30),
    ) -> dict[str, Any]:
        return pure_core.session_capsule_v2(
            actor=actor,
            claims=claims,
            inbox_events=inbox_events,
            work_rows=(),
            budget=budget,
        )


def load_exact_query_snapshot(db_path: str | Path | None = None) -> ExactQuerySnapshot:

    conn = connect_ro(db_path)
    try:
        if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise ExactQueryCoreError("SQLite query_only guard did not engage")
        conn.execute("BEGIN")
        policy_rows = conn.execute(
            "SELECT schema_epoch,enforcement_mode,active_generation "
            "FROM coord_authority_policy WHERE policy_id='exact_authority'"
        ).fetchall()
        if len(policy_rows) != 1:
            raise ExactQueryCoreError("exact-authority policy singleton missing")
        policy = policy_rows[0]
        active_generation = str(policy["active_generation"] or "")
        if (
            str(policy["schema_epoch"]) != AUTHORITY_SCHEMA_EPOCH
            or str(policy["enforcement_mode"]) != "enforce"
            or not active_generation
        ):
            raise ExactQueryCoreError("exact-authority policy is not enforced and active")
        if conn.execute(
            "SELECT 1 FROM coord_authority_generations WHERE generation_id=?",
            (active_generation,),
        ).fetchone() is None:
            raise ExactQueryCoreError("active exact-authority generation is missing")

        source_rows = conn.execute(
            "SELECT source_id,source_schema_epoch,source_change_seq "
            "FROM coord_source_state WHERE source_name='context_traversal'"
        ).fetchall()
        if len(source_rows) != 1:
            raise ExactQueryCoreError("context-traversal source singleton missing")
        source = source_rows[0]
        source_id = str(source["source_id"] or "")
        if len(source_id) != 32 or str(source["source_schema_epoch"]) != SOURCE_SCHEMA_EPOCH:
            raise ExactQueryCoreError("context-traversal source singleton is malformed")
        source_change_seq = source["source_change_seq"]
        if type(source_change_seq) is not int or source_change_seq < 0:
            raise ExactQueryCoreError("context-traversal source sequence is malformed")

        snapshot_at = float(
            conn.execute("SELECT (julianday('now')-2440587.5)*86400.0").fetchone()[0]
        )
        work_rows = [dict(row) for row in conn.execute("SELECT * FROM v_work_owner").fetchall()]
        pid_rows = conn.execute(
            "SELECT work_id,pid,pid_started_at FROM runs "
            "WHERE state='live' AND work_id IS NOT NULL AND pid IS NOT NULL"
        ).fetchall()
        seen_pid_work: set[str] = set()
        live_pid_by_work: dict[str, int] = {}
        for run in pid_rows:
            work_id = str(run["work_id"] or "")
            if not work_id:
                continue
            seen_pid_work.add(work_id)
            if coord_db.pid_matches(run["pid"], run["pid_started_at"]):
                live_pid_by_work[work_id] = live_pid_by_work.get(work_id, 0) + 1
        for work_row in work_rows:
            work_id = str(work_row.get("work_id") or "")
            if work_id in seen_pid_work:
                work_row["live_pid_count"] = live_pid_by_work.get(work_id, 0)
        head_rows = conn.execute(
            "SELECT h.authority_kind,h.work_id,h.authority_version_id AS h_version_id,"
            "h.generation_id AS h_generation,h.content_sha256 AS h_sha,"
            "v.authority_version_id AS v_version_id,v.authority_kind AS v_kind,"
            "v.work_id AS v_work_id,v.generation_id AS v_generation,"
            "v.content_sha256 AS v_sha,v.payload_json,g.generation_id AS stored_generation,"
            "g.schema_version AS generation_schema_version,"
            "g.manifest_sha256 AS generation_manifest_sha256,"
            "g.sources_json AS generation_sources_json,"
            "g.published_by AS generation_published_by "
            "FROM coord_authority_heads h "
            "JOIN coord_authority_versions v ON v.authority_version_id=h.authority_version_id "
            "LEFT JOIN coord_authority_generations g ON g.generation_id=h.generation_id "
            "ORDER BY h.authority_kind,h.work_id"
        ).fetchall()

        heads: dict[str, dict[str, dict[str, Any]]] = {
            kind: {} for kind in AUTHORITY_KINDS
        }
        for row in head_rows:
            kind = str(row["authority_kind"])
            work_id = str(row["work_id"])
            if kind not in heads:
                raise ExactQueryCoreError(f"unsupported authority head kind {kind!r}")
            if work_id in heads[kind]:
                raise ExactQueryCoreError(f"duplicate {kind} authority head for {work_id}")
            if (
                int(row["h_version_id"]) != int(row["v_version_id"])
                or kind != str(row["v_kind"])
                or work_id != str(row["v_work_id"])
                or str(row["h_generation"]) != str(row["v_generation"])
                or str(row["h_sha"]) != str(row["v_sha"])
            ):
                raise ExactQueryCoreError(f"{kind} head/version mismatch for {work_id}")
            if row["stored_generation"] is None:
                raise ExactQueryCoreError(f"{kind} head generation is missing for {work_id}")
            payload = _object_payload(row["payload_json"], kind=kind, work_id=work_id)
            actual_sha = _sha256(canonical_bytes(payload))
            if actual_sha != str(row["h_sha"]):
                raise ExactQueryCoreError(f"{kind} content hash mismatch for {work_id}")
            _validate_payload(kind, payload, work_id)
            if str(row["h_generation"]) not in {
                active_generation,
                "coord-live-declarations-r1",
            }:
                _validate_live_successor_generation(
                    conn,
                    generation_row=row,
                    kind=kind,
                    work_id=work_id,
                    payload=payload,
                    content_sha256=actual_sha,
                )
            heads[kind][work_id] = payload

        known_work_ids = {str(row.get("work_id") or "") for row in work_rows}
        orphan_heads = {
            kind: sorted(set(records) - known_work_ids)
            for kind, records in heads.items()
            if set(records) - known_work_ids
        }
        if orphan_heads:
            raise ExactQueryCoreError(f"authority heads reference missing work rows: {orphan_heads}")

        plane_authority = _plane_authority(heads["plane"].values())
        exact: list[dict[str, Any]] = []
        quarantine: list[dict[str, Any]] = []
        for source_row in work_rows:
            row = dict(source_row)
            work_id = str(row.get("work_id") or "")
            row["effective_tier"] = coord_db.effective_review_tier_for_work(
                conn, work_id, row=row
            )
            plane = heads["plane"].get(work_id)
            lineage = heads["lineage"].get(work_id)
            value_pin = heads["value_pin"].get(work_id)
            reasons: list[str] = []
            if plane is None:
                reasons.append("missing_exact_plane_head")
            elif plane.get("classification_state") != "adjudicated":
                reasons.append("plane_needs_review")
            elif plane.get("record_kind") != "work":
                reasons.append("record_kind_not_work")
            if lineage is None:
                reasons.append("missing_exact_lineage_head")
            if value_pin is None:
                reasons.append("missing_exact_value_pin_head")

            row["status"] = _derived_status(row, snapshot_at=snapshot_at)
            row["proof_state"] = coord_db.derive_proof_state(row, snapshot_at)
            row["group"] = row.get("module") or "(ungrouped)"
            row["updated_seq"] = int(round(float(row.get("updated_at") or 0.0) * 1_000_000))
            row["query_core_build_sha256"] = QUERY_CORE_BUILD_SHA256
            row["authority_generation"] = active_generation
            row["require_exact_lineage"] = True
            if reasons:
                row.pop("subject_plane", None)
                row["quarantined"] = True
                row["quarantine_reasons"] = sorted(reasons)
                row["authority_quarantined"] = True
                row["authority_quarantine_reason"] = ";".join(sorted(reasons))
                row["authority_heads_present"] = sorted(
                    kind for kind, payload in (("plane", plane), ("lineage", lineage), ("value_pin", value_pin))
                    if payload is not None
                )
                quarantine.append(row)
                continue

            assert plane is not None and lineage is not None and value_pin is not None
            high_value, value_score = _semantic_value(value_pin)
            metadata = dict(row.get("metadata")) if isinstance(row.get("metadata"), Mapping) else {}
            metadata.update(
                {
                    "subject_plane": plane["subject_plane"],
                    "program_id": lineage["program_id"],
                    "workstream_id": lineage["workstream_id"],
                    "episode_id": lineage["episode_id"],
                    "span_id": lineage["span_id"],
                    "pinned": _pin_value(value_pin),
                    "high_value": high_value,
                    "value_score": value_score,
                    "exact_authority": {
                        "plane": plane,
                        "lineage": lineage,
                        "value_pin": value_pin,
                    },
                }
            )
            row["metadata"] = metadata
            row["subject_plane"] = plane["subject_plane"]
            row["program_id"] = lineage["program_id"]
            row["workstream_id"] = lineage["workstream_id"]
            row["episode_id"] = lineage["episode_id"]
            row["span_id"] = lineage["span_id"]
            row["pinned"] = _pin_value(value_pin)
            row["high_value"] = high_value
            row["value_score"] = value_score
            row["quarantined"] = False
            row["quarantine_reasons"] = []
            exact.append(row)

        fence_again = conn.execute(
            "SELECT source_id,source_change_seq FROM coord_source_state "
            "WHERE source_name='context_traversal'"
        ).fetchone()
        if (
            str(fence_again["source_id"]) != source_id
            or int(fence_again["source_change_seq"]) != source_change_seq
        ):
            raise ExactQueryCoreError("source fence changed inside read transaction")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    finally:
        conn.close()

    exact.sort(key=lambda row: str(row.get("work_id") or ""))
    quarantine.sort(key=lambda row: str(row.get("work_id") or ""))
    return ExactQuerySnapshot(
        source_id=source_id,
        source_change_seq=source_change_seq,
        active_generation=active_generation,
        snapshot_at=snapshot_at,
        exact_rows=tuple(exact),
        quarantine_rows=tuple(quarantine),
        authority_head_counts={kind: len(heads[kind]) for kind in AUTHORITY_KINDS},
        _plane_authority=plane_authority,
    )


def load_query_snapshot(
    db_path: str | Path | None = None,
) -> ExactQuerySnapshot | LegacyFixtureQuerySnapshot:

    configured = Path(
        db_path
        if db_path is not None
        else os.environ.get("COORD_COORD_DB") or CANONICAL_COORD_DB
    ).resolve()
    try:
        return load_exact_query_snapshot(db_path)
    except sqlite3.OperationalError as exc:
        if "no such table: coord_authority_policy" not in str(exc):
            raise
        if configured == CANONICAL_COORD_DB.resolve():
            raise ExactQueryCoreError(
                "canonical coord.db is missing exact-authority migration v2"
            ) from exc
        conn = connect_ro(db_path)
        try:
            rows = tuple(coord_db.board_rows(conn))
        finally:
            conn.close()
        return LegacyFixtureQuerySnapshot(rows=rows)


__all__ = [
    "ExactQueryCoreError",
    "ExactQuerySnapshot",
    "LegacyFixtureQuerySnapshot",
    "QUERY_CORE_ADAPTER_VERSION",
    "QUERY_CORE_BUILD_MANIFEST",
    "QUERY_CORE_BUILD_SHA256",
    "SNAPSHOT_SCHEMA",
    "load_exact_query_snapshot",
    "load_query_snapshot",
]
