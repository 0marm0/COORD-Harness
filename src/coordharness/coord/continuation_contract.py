
from __future__ import annotations

import json
import re
from typing import Any


PARK_RESUME_CONTRACT_MISSING = "park_resume_contract_missing"
RESUME_TRIGGER_CONTRACT_INVALID = "resume_trigger_contract_invalid"
SUPPORTED_RESUME_PREDICATE_TYPES = frozenset(
    {
        "event_exists",
        "artifact_exists",
        "artifact_readable",
        "sha_matches",
        "verdict_posted",
        "dark_hold_exit",
        "all_of",
        "any_of",
    }
)
MANUAL_RESUME_PREDICATE = {"type": "manual"}
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SQLITE_MAX_INT = (1 << 63) - 1


class ParkContractError(ValueError):

    def __init__(self, missing_fields: list[str]) -> None:
        self.code = PARK_RESUME_CONTRACT_MISSING
        self.missing_fields = list(missing_fields)
        super().__init__(
            f"{self.code}: non-empty "
            + " and ".join(self.missing_fields)
            + " required before parking work"
        )


class ResumeTriggerContractError(ValueError):

    def __init__(self, detail: str) -> None:
        self.code = RESUME_TRIGGER_CONTRACT_INVALID
        self.detail = str(detail)
        super().__init__(f"{self.code}: {self.detail}")


def _validate_typed_predicate(value: Any) -> None:
    if not isinstance(value, dict):
        raise ResumeTriggerContractError("resume predicate must be a JSON object")
    predicate_type = str(value.get("type") or "").strip()
    if predicate_type not in SUPPORTED_RESUME_PREDICATE_TYPES:
        raise ResumeTriggerContractError(
            f"unsupported resume predicate type {predicate_type or '<missing>'!r}"
        )
    if predicate_type == "event_exists":
        _require_nonempty_string(value, "work_id", predicate_type)
        after_event_id = value.get("after_event_id")
        if (
            "after_event_id" not in value
            or isinstance(after_event_id, bool)
            or not isinstance(after_event_id, int)
            or after_event_id < 0
            or after_event_id > _SQLITE_MAX_INT
        ):
            raise ResumeTriggerContractError(
                "event_exists requires after_event_id as a non-negative SQLite integer"
            )
        return
    if predicate_type in {"artifact_exists", "artifact_readable"}:
        _require_nonempty_string(value, "path", predicate_type)
        return
    if predicate_type == "sha_matches":
        path = value.get("path")
        ref = value.get("ref")
        if not (
            (isinstance(path, str) and path.strip())
            or (isinstance(ref, str) and ref.strip())
        ):
            raise ResumeTriggerContractError(
                "sha_matches requires a non-empty path or ref"
            )
        sha256 = value.get("sha256")
        if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
            raise ResumeTriggerContractError(
                "sha_matches requires sha256 as exactly 64 hexadecimal characters"
            )
        return
    if predicate_type == "verdict_posted":
        for field in ("work_id", "verdict", "from_lane"):
            _require_nonempty_string(value, field, predicate_type)
        return
    if predicate_type == "dark_hold_exit":
        for field in ("hold_id", "work_id"):
            _require_nonempty_string(value, field, predicate_type)
        return
    if predicate_type in {"all_of", "any_of"}:
        children = value.get("predicates")
        if not isinstance(children, list) or not children:
            raise ResumeTriggerContractError(
                "composite resume predicates require non-empty predicates"
            )
        for child in children:
            _validate_typed_predicate(child)


def _require_nonempty_string(
    predicate: dict[str, Any],
    field: str,
    predicate_type: str,
) -> str:
    value = predicate.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ResumeTriggerContractError(
            f"{predicate_type} requires non-empty string field {field}"
        )
    return value.strip()


def normalize_resume_trigger_contract(
    *,
    resume_when: Any,
    resume_predicate: Any = None,
    resume_manual: bool = False,
) -> str | None:

    if not isinstance(resume_manual, bool):
        raise ResumeTriggerContractError("resume_manual must be a boolean")
    clean_resume_when = str(resume_when or "").strip()
    clean_predicate = str(resume_predicate or "").strip()
    if clean_predicate and resume_manual:
        raise ResumeTriggerContractError(
            "--resume-predicate and --resume-manual are mutually exclusive"
        )
    if not clean_resume_when:
        if clean_predicate or resume_manual:
            raise ResumeTriggerContractError(
                "resume_when is required when a resume trigger is declared"
            )
        return None
    if resume_manual:
        return json.dumps(
            MANUAL_RESUME_PREDICATE,
            sort_keys=True,
            separators=(",", ":"),
        )
    if not clean_predicate:
        raise ResumeTriggerContractError(
            "resume_when requires --resume-predicate or explicit --resume-manual"
        )
    try:
        predicate = json.loads(clean_predicate)
    except json.JSONDecodeError as exc:
        raise ResumeTriggerContractError(
            f"--resume-predicate must be valid JSON: {exc}"
        ) from exc
    _validate_typed_predicate(predicate)
    return json.dumps(predicate, sort_keys=True, separators=(",", ":"))


def require_park_resume_contract(
    *,
    next_step: Any,
    resume_when: Any,
) -> tuple[str, str]:

    clean_next_step = str(next_step or "").strip()
    clean_resume_when = str(resume_when or "").strip()
    missing_fields = [
        name
        for name, value in (
            ("next_step", clean_next_step),
            ("resume_when", clean_resume_when),
        )
        if not value
    ]
    if missing_fields:
        raise ParkContractError(missing_fields)
    return clean_next_step, clean_resume_when
