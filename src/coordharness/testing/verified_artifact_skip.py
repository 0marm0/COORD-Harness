
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

import pytest


_MARKER = "COORD_UNMEASURED::"


@dataclass(frozen=True)
class UnmeasuredSkip:
    guard_id: str
    artifact: str | None
    expected: bool
    reason: str


def skip_unmeasured(
    *,
    guard_id: str,
    reason: str,
    artifact: str | Path | None = None,
    expected: bool | None = None,
) -> None:

    if expected is None:
        expected = os.environ.get("COORD_EXPECT_VERIFIED_ARTIFACTS") == "1"

    payload = {
        "artifact": str(artifact) if artifact is not None else None,
        "expected": bool(expected),
        "guard_id": guard_id,
        "reason": reason,
    }
    message = _MARKER + json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )
    if expected:
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


def require_verified_artifact(
    path: str | Path,
    *,
    guard_id: str,
    expected: bool | None = None,
) -> Path:

    artifact = Path(path)
    if not artifact.exists():
        skip_unmeasured(
            guard_id=guard_id,
            artifact=artifact,
            expected=expected,
            reason="required containment artifact is not materialized",
        )
    return artifact


def parse_unmeasured_skip(reason: str) -> UnmeasuredSkip | None:

    marker_at = reason.find(_MARKER)
    if marker_at < 0:
        return None
    raw = reason[marker_at + len(_MARKER) :]
    try:
        payload, _end = json.JSONDecoder().raw_decode(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return UnmeasuredSkip(
        guard_id=str(payload.get("guard_id") or "unknown"),
        artifact=(
            str(payload["artifact"])
            if payload.get("artifact") is not None
            else None
        ),
        expected=bool(payload.get("expected")),
        reason=str(payload.get("reason") or "unspecified"),
    )


def _verified_artifact_nodeid(nodeid: str) -> bool:
    normalized = nodeid.replace("\\", "/")
    return "/verified_artifacts/" in f"/{normalized}"


def _skip_reason(report: Any) -> str:
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) >= 3:
        return str(longrepr[2])
    return str(longrepr)


_records: list[tuple[str, UnmeasuredSkip]] = []
_failed_records: list[tuple[str, UnmeasuredSkip]] = []
_untyped_records: list[tuple[str, str]] = []
_collected = 0


def pytest_configure(config: Any) -> None:
    del config
    _records.clear()
    _failed_records.clear()
    _untyped_records.clear()
    global _collected
    _collected = 0


def pytest_collection_finish(session: Any) -> None:
    global _collected
    _collected = sum(
        1 for item in session.items if _verified_artifact_nodeid(str(item.nodeid))
    )


def pytest_runtest_logreport(report: Any) -> None:
    if report.when not in {"setup", "call"}:
        return
    if getattr(report, "wasxfail", None):
        return
    reason = _skip_reason(report)
    typed = parse_unmeasured_skip(reason)
    if report.failed and typed is not None:
        _failed_records.append((str(report.nodeid), typed))
    elif report.skipped and typed is not None:
        _records.append((str(report.nodeid), typed))
    elif report.skipped and _verified_artifact_nodeid(str(report.nodeid)):
        _untyped_records.append((str(report.nodeid), reason))


def pytest_terminal_summary(terminalreporter: Any) -> None:
    if not (_collected or _records or _failed_records or _untyped_records):
        return
    skipped_tests = len(_records) + len(_untyped_records)
    rate = skipped_tests / _collected if _collected else 0.0
    expected_guards = {
        item.guard_id
        for _nodeid, item in (*_records, *_failed_records)
        if item.expected
    }
    terminalreporter.write_sep(
        "=",
        (
            "VERIFIED_ARTIFACT_SKIP_TRIPWIRE "
            f"collected={_collected} skipped={skipped_tests} rate={rate:.6f} "
            f"typed_unmeasured={len(_records)} "
            f"typed_unmeasured_failures={len(_failed_records)} "
            f"untyped_unmeasured={len(_untyped_records)} "
            f"expected_missing_guards={len(expected_guards)}"
        ),
    )
    for nodeid, item in _records:
        terminalreporter.write_line(
            "UNMEASURED "
            f"guard={item.guard_id} expected={str(item.expected).lower()} "
            f"artifact={item.artifact or '-'} nodeid={nodeid}"
        )
    for nodeid, item in _failed_records:
        terminalreporter.write_line(
            "UNMEASURED_FAILURE "
            f"guard={item.guard_id} expected=true "
            f"artifact={item.artifact or '-'} nodeid={nodeid}"
        )
    for nodeid, reason in _untyped_records:
        terminalreporter.write_line(
            f"UNMEASURED guard=unclassified expected=false nodeid={nodeid} "
            f"reason={reason}"
        )
