
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping


R4_SUBJECT_PLANES = frozenset({"product", "harness", "infrastructure", "shared"})
R4_RECORD_KINDS = frozenset(
    {"work", "telemetry", "fact", "decision", "artifact", "memory", "document", "code", "conversation"}
)
_SHA256_RE = re.compile(r"[a-f0-9]{64}")
_VERIFIED_CONSTRUCTION = object()


@dataclass(frozen=True)
class PlaneRecord:
    work_id: str
    subject_plane: str | None
    domain: str
    record_kind: str
    confidence: float
    evidence_token: str
    classification_state: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "subject_plane": self.subject_plane,
            "domain": self.domain,
            "record_kind": self.record_kind,
            "confidence": self.confidence,
            "evidence_token": self.evidence_token,
            "classification_state": self.classification_state,
        }


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _row_id(row: Mapping[str, Any]) -> str:
    return str(row.get("work_id") or row.get("id") or row.get("roadmap_id") or "").strip()


def _record_from_manifest_row(row: Mapping[str, Any]) -> PlaneRecord:
    work_id = _row_id(row)
    if not work_id:
        raise ValueError("R4 manifest row requires work_id")
    raw_plane = str(row.get("subject_plane") or "").strip()
    if raw_plane == "needs_review":
        plane = None
        state = "needs_review"
    elif raw_plane in R4_SUBJECT_PLANES:
        plane = raw_plane
        state = "adjudicated"
    else:
        raise ValueError(f"invalid R4 subject_plane for {work_id}: {raw_plane!r}")
    record_kind = str(row.get("record_kind") or "").strip()
    if record_kind not in R4_RECORD_KINDS:
        raise ValueError(f"invalid R4 record_kind for {work_id}: {record_kind!r}")
    domain = str(row.get("domain") or "").strip()
    if not domain and state == "adjudicated":
        raise ValueError(f"R4 manifest row requires domain for {work_id}")
    try:
        confidence = float(row.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"R4 manifest confidence must be numeric for {work_id}") from exc
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"R4 manifest confidence out of range for {work_id}")
    evidence_token = str(row.get("evidence_token") or "").strip()
    if not evidence_token:
        raise ValueError(f"R4 manifest row requires evidence_token for {work_id}")
    return PlaneRecord(
        work_id=work_id,
        subject_plane=plane,
        domain=domain,
        record_kind=record_kind,
        confidence=confidence,
        evidence_token=evidence_token,
        classification_state=state,
    )


class R4PlaneAuthority:

    def __init__(
        self,
        *,
        source_sha256: str,
        records: Mapping[str, PlaneRecord],
        source_rows: int,
        _construction_token: object | None = None,
    ) -> None:
        if _construction_token is not _VERIFIED_CONSTRUCTION:
            raise TypeError("R4PlaneAuthority must be constructed by from_bytes/from_path")
        self.source_sha256 = source_sha256
        self._records = dict(records)
        self.source_rows = int(source_rows)
        self.manifest_verified = True

    @classmethod
    def from_bytes(cls, raw: bytes, *, expected_sha256: str) -> "R4PlaneAuthority":
        expected = str(expected_sha256 or "").strip().lower()
        if not _SHA256_RE.fullmatch(expected):
            raise ValueError("expected_sha256 must be 64 lowercase hex characters")
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected:
            raise ValueError(f"R4 authority manifest hash mismatch: {actual} != {expected}")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
            raise ValueError("R4 authority manifest requires top-level rows list")
        records: dict[str, PlaneRecord] = {}
        for raw_row in payload["rows"]:
            if not isinstance(raw_row, Mapping):
                raise ValueError("R4 authority manifest row must be an object")
            record = _record_from_manifest_row(raw_row)
            prior = records.get(record.work_id)
            if prior is not None and prior != record:
                raise ValueError(f"conflicting duplicate R4 authority for {record.work_id}")
            records[record.work_id] = record
        return cls(
            source_sha256=actual,
            records=records,
            source_rows=len(payload["rows"]),
            _construction_token=_VERIFIED_CONSTRUCTION,
        )

    @classmethod
    def from_path(cls, path: str | Path, *, expected_sha256: str) -> "R4PlaneAuthority":
        return cls.from_bytes(Path(path).read_bytes(), expected_sha256=expected_sha256)

    @property
    def distinct_work_ids(self) -> int:
        return len(self._records)

    def resolve(self, work_id: str) -> dict[str, Any]:
        key = str(work_id or "").strip()
        record = self._records.get(key)
        if record is None:
            return {
                "work_id": key,
                "subject_plane": None,
                "subject_plane_exact": False,
                "classification_state": "unknown",
                "quarantined": True,
                "quarantine_reason": "missing_adjudicated_r4_authority",
                "authority_source_sha256": self.source_sha256,
            }
        result = record.as_dict()
        result.update(
            {
                "subject_plane_exact": record.subject_plane is not None,
                "quarantined": record.subject_plane is None,
                "quarantine_reason": (
                    "adjudicated_needs_review" if record.subject_plane is None else None
                ),
                "authority_source_sha256": self.source_sha256,
            }
        )
        return result

    def overlay_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(row)
        metadata = dict(out.get("metadata")) if isinstance(out.get("metadata"), Mapping) else {}
        work_id = _row_id(out)
        authority = self.resolve(work_id)
        declared = {
            str(value)
            for value in (out.get("subject_plane"), metadata.get("subject_plane"))
            if value not in (None, "")
        }
        authority_plane = authority.get("subject_plane")
        conflict = bool(declared and (authority_plane is None or declared != {authority_plane}))
        if authority_plane is not None and not conflict:
            out["subject_plane"] = authority_plane
            metadata["subject_plane"] = authority_plane
            out["record_kind"] = authority["record_kind"]
            out["r4_domain"] = authority["domain"]
        elif conflict:
            out["subject_plane"] = "mixed"
            metadata["subject_plane"] = "mixed"
            authority = {
                **authority,
                "subject_plane": None,
                "subject_plane_exact": False,
                "classification_state": "conflict",
                "quarantined": True,
                "quarantine_reason": "declared_plane_conflicts_with_adjudicated_authority",
                "declared_planes": sorted(declared),
            }
        else:
            out.pop("subject_plane", None)
            metadata.pop("subject_plane", None)
        metadata["r4_plane_authority"] = authority
        out["metadata"] = metadata
        return out

    def overlay_rows(self, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        overlaid = [self.overlay_row(row) for row in rows]
        states = Counter(
            str(row.get("metadata", {}).get("r4_plane_authority", {}).get("classification_state") or "unknown")
            for row in overlaid
        )
        return {
            "rows": overlaid,
            "receipt": {
                "source_sha256": self.source_sha256,
                "source_rows": self.source_rows,
                "distinct_authority_work_ids": self.distinct_work_ids,
                "input_rows": len(overlaid),
                "classification_states": dict(sorted(states.items())),
                "inference_used": False,
                "writes": 0,
            },
        }


def validate_new_plane_declaration(declaration: Mapping[str, Any]) -> dict[str, Any]:

    plane = str(declaration.get("subject_plane") or "").strip()
    record_kind = str(declaration.get("record_kind") or "").strip()
    source_kind = str(declaration.get("authority_source_kind") or "").strip()
    source_sha256 = str(declaration.get("authority_source_sha256") or "").strip().lower()
    if plane not in R4_SUBJECT_PLANES:
        raise ValueError(f"explicit subject_plane must be one of {sorted(R4_SUBJECT_PLANES)}")
    if record_kind not in R4_RECORD_KINDS:
        raise ValueError(f"explicit record_kind must be one of {sorted(R4_RECORD_KINDS)}")
    if source_kind not in {"controller_adjudication", "creator_declaration", "migration_manifest"}:
        raise ValueError("untrusted R4 authority_source_kind")
    if not _SHA256_RE.fullmatch(source_sha256):
        raise ValueError("explicit R4 declaration requires a canonical authority_source_sha256")
    return {
        "subject_plane": plane,
        "record_kind": record_kind,
        "authority_source_kind": source_kind,
        "authority_source_sha256": source_sha256,
        "subject_plane_exact": True,
        "inference_used": False,
    }
