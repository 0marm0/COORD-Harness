
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from coordharness import config as harness_config
from coordharness.coord.process_liveness import POSIX_LIVENESS_PROBE_AVAILABLE

def retired_test_basenames(repo_root: Path) -> frozenset[str]:
    return frozenset()


def retired_test_nodeids(repo_root: Path) -> frozenset[str]:
    return frozenset()

SCHEMA = "coordharness.pytest-sharded-gate.v2"
DEFAULT_SHARDS = 48
MAX_FILES_PER_SHARD = 8
MAX_SHARD_SECONDS = 290.0
MAX_SHARD_RSS_KIB = 4_750_000
MEMORY_SINGLETON_MARKER = "# pytest-gate: memory-singleton"
RECEIPT_RELATIVE = Path(".coordharness/pytest_gate/receipt.json")
RUNNING_RELATIVE = Path(".coordharness/pytest_gate/running.json")
OUTCOME_CLASSES = frozenset({"REAL_REGRESSION", "MEMORY_KILL", "STALE_TELEMETRY"})


def _contained_gate_path(raw: str | Path, repo_root: Path) -> Path:
    root = repo_root.resolve()
    path = Path(raw).expanduser()
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve(strict=False)
    allowed = (root, harness_config.state_dir().resolve(strict=False))
    if not any(resolved == base or base in resolved.parents for base in allowed):
        raise ValueError(f"pytest-gate path escapes project and state roots: {raw!r}")
    return resolved


def pytest_gate_paths(
    repo_root: Path, *, env: Mapping[str, str] | None = None
) -> tuple[Path, Path]:
    source = os.environ if env is None else env
    configured = str(source.get("COORD_PYTEST_GATE_DIR") or "").strip()
    if configured:
        directory = _contained_gate_path(configured, repo_root)
        return directory / "receipt.json", directory / "running.json"
    return (
        _contained_gate_path(RECEIPT_RELATIVE, repo_root),
        _contained_gate_path(RUNNING_RELATIVE, repo_root),
    )


def configured_test_roots(
    repo_root: Path, *, env: Mapping[str, str] | None = None
) -> tuple[Path, ...]:
    source = os.environ if env is None else env
    inline = str(source.get("COORD_PYTEST_ROOTS_JSON") or "").strip()
    file_value = str(source.get("COORD_PYTEST_ROOTS_FILE") or "").strip()
    if inline and file_value:
        raise ValueError("configure only one of COORD_PYTEST_ROOTS_JSON or FILE")
    if not inline and not file_value:
        parsed: object = ["tests"]
    else:
        if file_value:
            inline = _contained_gate_path(file_value, repo_root).read_text(encoding="utf-8")
        try:
            parsed = json.loads(inline)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid pytest-root JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError("pytest roots must be a JSON list")
    roots: list[Path] = []
    for raw in parsed:
        value = str(raw).strip()
        path = Path(value)
        if not value or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"pytest root must be repository-relative: {raw!r}")
        resolved = _contained_gate_path(path, repo_root)
        roots.append(resolved.relative_to(repo_root.resolve()))
    return tuple(dict.fromkeys(roots))

_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".coordharness",
        "__pycache__",
        ".venv",
        "node_modules",
        "runtime",
        "vendor_sources",
    }
)
_EXCLUDED_FILENAMES = frozenset({".coverage", ".DS_Store"})
_EXCLUDED_SUFFIXES = frozenset(
    {
        ".db",
        ".db-shm",
        ".db-wal",
        ".log",
        ".pid",
        ".sock",
        ".sqlite",
        ".sqlite3",
        ".tmp",
    }
)


def shard_exit_is_zero_selection(shard: dict[str, Any]) -> bool:

    counts = shard.get("counts") or {}
    return (
        shard.get("exit_code") == 5
        and shard.get("timed_out") is False
        and shard.get("rss_exceeded") is False
        and bool(shard.get("junit_sha256"))
        and all(
            int(counts.get(key) or 0) == 0
            for key in ("tests", "failures", "errors", "skipped")
        )
    )


def shard_exit_is_admissible(shard: dict[str, Any]) -> bool:

    return shard.get("exit_code") in {0, 1} or shard_exit_is_zero_selection(shard)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _eligible(path: Path, repo_root: Path) -> bool:
    try:
        relative = path.relative_to(repo_root)
    except ValueError:
        return False
    return (
        not any(part in _EXCLUDED_PARTS for part in relative.parts)
        and path.name not in _EXCLUDED_FILENAMES
        and not path.name.startswith("_tmp_")
        and not any(path.name.endswith(suffix) for suffix in _EXCLUDED_SUFFIXES)
    )


def _git_candidate_paths(root: Path) -> set[Path] | None:

    command = [
        "git",
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    ]
    result = subprocess.run(command, cwd=root, capture_output=True, check=False)
    if result.returncode != 0:
        return None
    return {root / os.fsdecode(raw) for raw in result.stdout.split(b"\0") if raw}


def input_manifest(repo_root: Path) -> dict[str, Any]:

    root = repo_root.resolve()
    paths = _git_candidate_paths(root)
    if paths is None:
        paths = set()
        for directory, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                name for name in dirnames if name not in _EXCLUDED_PARTS
            )
            parent = Path(directory)
            for name in filenames:
                paths.add(parent / name)
    paths = {
        path
        for path in paths
        if path.is_file() and not path.is_symlink() and _eligible(path, root)
    }

    files = []
    aggregate = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        digest = _sha256(path)
        files.append({"path": relative, "bytes": size, "sha256": digest})
        aggregate.update(
            json.dumps(
                [relative, size, digest],
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )
        aggregate.update(b"\n")
    return {
        "scope": "full_repo_tracked_untracked_nonignored",
        "excluded_parts": sorted(_EXCLUDED_PARTS),
        "file_count": len(files),
        "files": files,
        "sha256": aggregate.hexdigest(),
    }


def discover_test_files(repo_root: Path) -> list[str]:
    root = repo_root.resolve()
    retired = retired_test_basenames(root)
    found: list[str] = []
    for relative_root in configured_test_roots(root):
        base = root / relative_root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if not path.is_file() or path.is_symlink() or not _eligible(path, root):
                continue
            if path.name in retired:
                continue
            if path.name.startswith("test_") or path.name.endswith("_test.py"):
                found.append(path.relative_to(root).as_posix())
    return sorted(set(found))


def memory_singleton_test_files(
    repo_root: Path, test_files: list[str]
) -> list[str]:

    marker = MEMORY_SINGLETON_MARKER.encode("ascii")
    found: list[str] = []
    for relative in sorted(set(test_files)):
        raw = (repo_root / relative).read_bytes()
        if marker in raw.splitlines():
            found.append(relative)
    return found


def runner_is_live(sidecar: dict[str, Any]) -> bool | None:

    try:
        pid = int(sidecar.get("runner_pid"))
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if not POSIX_LIVENESS_PROBE_AVAILABLE:
        # os.kill(pid, 0) is a POSIX null-signal existence probe; on a
        # platform where signal 0 means something else (Windows aliases it
        # to CTRL_C_EVENT and sends a real console control event instead),
        # this refuses to call it at all rather than either fake a bool or
        # risk the side effect. "Cannot verify" is already this function's
        # own vocabulary for that -- see the PermissionError/OSError
        # branches below -- so this platform gap uses the same sentinel
        # instead of a new failure mode.
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError:
        return None
    expected = str(sidecar.get("runner_script") or "run_pytest_gate_shards.py")
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.returncode == 0 and expected in result.stdout


def canonical_gate_pair_findings(
    running: dict[str, Any], receipt: dict[str, Any]
) -> list[str]:

    findings: list[str] = []
    direct_fields = (
        "schema",
        "run_id",
        "artifact_root",
        "execution_root",
        "source_commit",
        "source_worktree_clean",
        "started_at",
        "state",
        "finished_at",
        "shard_count",
        "shard_cardinality",
        "shard_plan_sha256",
        "stopped_early",
        "totals",
        "partial_junit_totals",
        "totals_admissible",
        "completion_state",
        "infrastructure_stops",
        "outcome_classification_counts",
    )
    for field in direct_fields:
        if running.get(field) != receipt.get(field):
            findings.append(f"canonical pytest pair disagrees on {field}")
    mapped_fields = (
        ("completed_shards", "completed_shard_count"),
        ("input_manifest_sha256", "input_manifest_before_sha256"),
    )
    for running_field, receipt_field in mapped_fields:
        if running.get(running_field) != receipt.get(receipt_field):
            findings.append(
                "canonical pytest pair disagrees on "
                f"{running_field}/{receipt_field}"
            )
    return findings


def reconcile_running_sidecar(
    repo_root: Path,
    *,
    rewrite: bool = False,
) -> dict[str, Any] | None:

    root = repo_root.resolve()
    receipt_path, path = pytest_gate_paths(root)
    try:
        sidecar = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        receipt = {}
    if sidecar.get("state") != "RUNNING":
        pair_findings = canonical_gate_pair_findings(sidecar, receipt)
        sidecar["state_derived_from_liveness"] = True
        sidecar["canonical_pair_consistent"] = not pair_findings
        sidecar["canonical_pair_findings"] = pair_findings
        return sidecar
    runner_liveness = runner_is_live(sidecar)
    if runner_liveness is True:
        sidecar["state_derived_from_liveness"] = True
        sidecar["canonical_pair_consistent"] = None
        return sidecar
    if runner_liveness is None:
        sidecar["state_derived_from_liveness"] = False
        sidecar["runner_liveness"] = "UNAVAILABLE_PROCESS_INSPECTION"
        sidecar["canonical_pair_consistent"] = None
        return sidecar

    receipt_input_before = receipt.get("input_manifest_before_sha256") or next(
        (
            shard.get("input_manifest_sha256")
            for shard in (receipt.get("shards") or [])
            if isinstance(shard, dict) and shard.get("input_manifest_sha256")
        ),
        None,
    )
    same_run = all(
        (
            receipt.get("schema") == sidecar.get("schema"),
            receipt.get("run_id") == sidecar.get("run_id"),
            receipt.get("artifact_root") == sidecar.get("artifact_root"),
            receipt.get("execution_root") == sidecar.get("execution_root"),
            receipt.get("source_commit") == sidecar.get("source_commit"),
            receipt.get("started_at") == sidecar.get("started_at"),
            receipt_input_before == sidecar.get("input_manifest_sha256"),
        )
    )
    terminal = (
        str(receipt.get("state"))
        if same_run and receipt.get("state") in {"PASS", "FAIL"}
        else "INTERRUPTED"
    )
    if same_run and terminal in {"PASS", "FAIL"}:
        sidecar.update(
            {
                "state": terminal,
                "finished_at": receipt.get("finished_at"),
                "completed_shards": receipt.get("completed_shard_count"),
                "shard_cardinality": receipt.get("shard_cardinality"),
                "stopped_early": receipt.get("stopped_early"),
                "totals": receipt.get("totals"),
                "partial_junit_totals": receipt.get("partial_junit_totals"),
                "totals_admissible": receipt.get("totals_admissible"),
                "completion_state": receipt.get("completion_state"),
                "infrastructure_stops": receipt.get("infrastructure_stops"),
                "outcome_classification_counts": receipt.get(
                    "outcome_classification_counts"
                ),
                "state_derived_from_liveness": True,
                "reconciled_reason": "matching_terminal_receipt",
            }
        )
        pair_findings = canonical_gate_pair_findings(sidecar, receipt)
        sidecar["canonical_pair_consistent"] = not pair_findings
        sidecar["canonical_pair_findings"] = pair_findings
    else:
        sidecar.update(
            {
                "state": "INTERRUPTED",
                "state_derived_from_liveness": True,
                "reconciled_reason": "runner_process_not_live",
                "canonical_pair_consistent": False,
                "canonical_pair_findings": [
                    "dead canonical RUNNING sidecar has no matching terminal receipt"
                ],
            }
        )
    if rewrite:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(sidecar, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    return sidecar


def _junit_failure_rows(path: Path) -> list[dict[str, str]]:
    root = ET.parse(path).getroot()
    rows: list[dict[str, str]] = []
    for case in root.findall(".//testcase"):
        for kind in ("failure", "error"):
            node = case.find(kind)
            if node is None:
                continue
            rows.append(
                {
                    "kind": kind,
                    "classname": str(case.attrib.get("classname") or ""),
                    "name": str(case.attrib.get("name") or ""),
                    "message": str(node.attrib.get("message") or "")[:500],
                }
            )
    return rows


def classify_receipt_outcomes(
    repo_root: Path, receipt: dict[str, Any]
) -> list[dict[str, Any]]:

    root = repo_root.resolve()
    outcomes: list[dict[str, Any]] = []
    for shard in receipt.get("shards") or []:
        counts = shard.get("counts") or {}
        expected = int(counts.get("failures") or 0) + int(counts.get("errors") or 0)
        junit = root / str(shard.get("junit") or "")
        log = root / str(shard.get("log") or "")
        junit_ok = (
            junit.is_file()
            and shard.get("junit_sha256")
            and _sha256(junit) == shard.get("junit_sha256")
        )
        log_ok = (
            log.is_file()
            and shard.get("log_sha256")
            and _sha256(log) == shard.get("log_sha256")
        )
        parsed = _junit_failure_rows(junit) if junit_ok else []
        for row in parsed[:expected]:
            outcomes.append(
                {
                    "class": "REAL_REGRESSION",
                    "grain": "test_outcome",
                    "shard": int(shard.get("index") or 0),
                    "kind": row["kind"],
                    "test": "::".join(
                        part for part in (row["classname"], row["name"]) if part
                    ),
                    "evidence": {
                        "junit": str(shard.get("junit") or ""),
                        "junit_sha256_verified": True,
                        "message": row["message"],
                        "exit_code": shard.get("exit_code"),
                    },
                }
            )
        missing = expected - min(len(parsed), expected)
        if missing > 0 and not shard.get("rss_exceeded"):
            classification = (
                "STALE_TELEMETRY"
                if not junit_ok or not log_ok
                else "REAL_REGRESSION"
            )
            reason = (
                "missing_or_hash_mismatched_shard_artifact"
                if classification == "STALE_TELEMETRY"
                else "pytest_reported_unparsed_failure_or_error"
            )
            for offset in range(missing):
                outcomes.append(
                    {
                        "class": classification,
                        "grain": "test_outcome",
                        "shard": int(shard.get("index") or 0),
                        "kind": "aggregate_error",
                        "test": (
                            f"shard-{int(shard.get('index') or 0)}-"
                            f"unparsed-{offset + 1}"
                        ),
                        "evidence": {
                            "reason": reason,
                            "junit": str(shard.get("junit") or ""),
                            "junit_sha256_verified": bool(junit_ok),
                            "log_sha256_verified": bool(log_ok),
                            "exit_code": shard.get("exit_code"),
                            "rss_exceeded": False,
                            "peak_rss_kib": int(shard.get("peak_rss_kib") or 0),
                        },
                    }
                )

        if shard.get("rss_exceeded"):
            outcomes.append(
                {
                    "class": "MEMORY_KILL",
                    "grain": "shard_infrastructure",
                    "shard": int(shard.get("index") or 0),
                    "kind": "rss_guard",
                    "test": None,
                    "evidence": {
                        "reason": "runner_rss_guard_terminated_process_group",
                        "junit": str(shard.get("junit") or ""),
                        "junit_sha256_verified": bool(junit_ok),
                        "log_sha256_verified": bool(log_ok),
                        "exit_code": shard.get("exit_code"),
                        "rss_exceeded": True,
                        "peak_rss_kib": int(shard.get("peak_rss_kib") or 0),
                    },
                }
            )
        elif (
            missing == 0
            and (
                shard.get("timed_out") is True
                or not shard_exit_is_admissible(shard)
                or not junit_ok
                or not log_ok
            )
        ):
            outcomes.append(
                {
                    "class": "STALE_TELEMETRY",
                    "grain": "shard_infrastructure",
                    "shard": int(shard.get("index") or 0),
                    "kind": "inadmissible_shard",
                    "test": None,
                    "evidence": {
                        "reason": "inadmissible_or_missing_shard_artifact",
                        "junit": str(shard.get("junit") or ""),
                        "junit_sha256_verified": bool(junit_ok),
                        "log_sha256_verified": bool(log_ok),
                        "exit_code": shard.get("exit_code"),
                        "rss_exceeded": False,
                        "peak_rss_kib": int(shard.get("peak_rss_kib") or 0),
                    },
                }
            )
    return outcomes


def shard_test_files(
    repo_root: Path,
    test_files: list[str],
    shard_count: int = DEFAULT_SHARDS,
    *,
    max_files_per_shard: int | None = None,
) -> list[list[str]]:

    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    if max_files_per_shard is not None and max_files_per_shard < 1:
        raise ValueError("max_files_per_shard must be positive")
    if not test_files:
        raise ValueError("no pytest files discovered")
    count = min(shard_count, len(test_files))
    memory_singletons = memory_singleton_test_files(repo_root, test_files)
    singleton_count = len(memory_singletons)
    if max_files_per_shard is not None and (
        count < singleton_count
        or (count - singleton_count) * max_files_per_shard
        < len(test_files) - singleton_count
    ):
        raise ValueError(
            "shard_count cannot satisfy max_files_per_shard plus memory-singleton "
            "isolation"
        )
    weighted: list[tuple[int, str]] = []
    for relative in test_files:
        if relative in memory_singletons:
            continue
        raw = (repo_root / relative).read_bytes()
        test_defs = raw.count(b"\ndef test_") + raw.count(b"\nasync def test_")
        weight = max(1, test_defs) * 4096 + len(raw)
        weighted.append((weight, relative))
    bins: list[list[str]] = [[] for _ in range(count)]
    totals = [0] * count
    for index, relative in enumerate(memory_singletons):
        bins[index].append(relative)
    ordinary_bins = range(singleton_count, count)
    for weight, relative in sorted(weighted, key=lambda item: (-item[0], item[1])):
        candidates = [
            index
            for index in ordinary_bins
            if max_files_per_shard is None or len(bins[index]) < max_files_per_shard
        ]
        if not candidates:
            raise ValueError("shard_count leaves no bin for ordinary test files")
        index = min(candidates, key=lambda item: (totals[item], item))
        bins[index].append(relative)
        totals[index] += weight
    return [sorted(group) for group in bins]


def shard_plan_sha256(groups: list[list[str]]) -> str:

    return hashlib.sha256(
        json.dumps(groups, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def git_source_commit(repo_root: Path) -> str:

    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    commit = result.stdout.strip().lower()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("pytest gate source commit is unavailable or not full SHA-1")
    return commit


def validate_shard_plan(
    repo_root: Path,
    test_files: list[str],
    groups: list[list[str]],
    *,
    requested_minimum_shards: int,
    max_files_per_shard: int = MAX_FILES_PER_SHARD,
) -> list[str]:

    findings: list[str] = []
    if not groups or any(not group for group in groups):
        findings.append("pytest shard plan contains no groups or an empty group")
        return findings
    flattened = [path for group in groups for path in group]
    if len(flattened) != len(set(flattened)):
        findings.append("pytest shard plan contains duplicate files")
    if sorted(flattened) != sorted(test_files):
        findings.append("pytest shard plan drops or adds discovered files")
    if any(group != sorted(group) for group in groups):
        findings.append("pytest shard plan member ordering is noncanonical")
    if any(len(group) > max_files_per_shard for group in groups):
        findings.append("pytest shard plan exceeds the hard files-per-shard cap")
    expected_count = recommended_shard_count(
        len(test_files),
        minimum=requested_minimum_shards,
        max_files_per_shard=max_files_per_shard,
        memory_singleton_count=len(memory_singleton_test_files(repo_root, test_files)),
    )
    if len(groups) != expected_count:
        findings.append(
            "pytest shard plan is under/overpartitioned "
            f"({len(groups)} != {expected_count})"
        )
        return findings
    expected = shard_test_files(
        repo_root,
        test_files,
        expected_count,
        max_files_per_shard=max_files_per_shard,
    )
    if groups != expected:
        findings.append("pytest shard plan differs from deterministic recomputation")
    return findings


def recommended_shard_count(
    test_file_count: int,
    *,
    minimum: int = DEFAULT_SHARDS,
    max_files_per_shard: int = MAX_FILES_PER_SHARD,
    memory_singleton_count: int = 0,
) -> int:

    if test_file_count < 1:
        raise ValueError("test_file_count must be positive")
    if minimum < 1:
        raise ValueError("minimum must be positive")
    if max_files_per_shard < 1:
        raise ValueError("max_files_per_shard must be positive")
    if not 0 <= memory_singleton_count <= test_file_count:
        raise ValueError("memory_singleton_count must be within the test-file count")
    capacity_count = memory_singleton_count + math.ceil(
        (test_file_count - memory_singleton_count) / max_files_per_shard
    )
    return min(
        test_file_count,
        max(minimum, capacity_count),
    )


def _parse_receipt_time(value: Any, label: str, findings: list[str]) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        findings.append(f"pytest gate {label} is not an ISO timestamp")
        return None


def _receipt_artifact_root(
    repo_root: Path,
    receipt: dict[str, Any],
    findings: list[str],
) -> tuple[str, Path] | None:
    run_id = str(receipt.get("run_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
        findings.append("pytest gate run_id is absent or unsafe")
        return None
    raw = str(receipt.get("artifact_root") or "")
    relative = Path(raw)
    if (
        not raw
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.name != run_id
    ):
        findings.append("pytest gate artifact_root is not bound to run_id")
        return None
    return run_id, repo_root / relative


def _junit_terminal_inventory(
    path: Path,
    *,
    files: list[str],
) -> tuple[dict[str, int], list[str], list[str]]:

    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall(".//testsuite"))
    counts = {
        key: sum(int(node.attrib.get(key, 0)) for node in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    module_paths = {
        relative[:-3].replace("/", "."): relative
        for relative in files
        if relative.endswith(".py")
    }
    canonical_ids: list[str] = []
    errors: list[str] = []
    observed = Counter()
    cases = root.findall(".//testcase")
    for case in cases:
        classname = str(case.attrib.get("classname") or "")
        name = str(case.attrib.get("name") or "")
        if classname.startswith("tests."):
            errors.append(f"noncanonical legacy classname {classname!r}")
        lookup = classname or name
        candidates = [
            (module, relative)
            for module, relative in module_paths.items()
            if lookup == module or lookup.startswith(module + ".")
        ]
        if len(candidates) != 1:
            errors.append(
                f"testcase {classname!r}::{name!r} resolves to "
                f"{len(candidates)} assigned files"
            )
            continue
        module, relative = candidates[0]
        class_tail = classname[len(module) :].lstrip(".") if classname else ""
        case_name = name if classname else "<collection>"
        canonical_ids.append(
            "::".join(part for part in (relative, class_tail, case_name) if part)
        )
        terminal_nodes = [
            kind
            for kind in ("failure", "error", "skipped")
            if case.find(kind) is not None
        ]
        if len(terminal_nodes) > 1:
            errors.append(f"testcase {classname!r}::{name!r} has multiple terminals")
            continue
        observed[terminal_nodes[0] if terminal_nodes else "passed"] += 1
    expected_observed = {
        "tests": len(cases),
        "failures": observed["failure"],
        "errors": observed["error"],
        "skipped": observed["skipped"],
    }
    if counts != expected_observed:
        errors.append(
            f"JUnit suite counts {counts} do not match testcase terminals "
            f"{expected_observed}"
        )
    return counts, canonical_ids, errors


def audit_receipt_integrity(
    repo_root: Path,
    receipt: dict[str, Any],
) -> list[str]:

    root = repo_root.resolve()
    findings: list[str] = []
    if receipt.get("schema") != SCHEMA:
        return ["unsupported pytest gate receipt schema"]
    if receipt.get("state") not in {"PASS", "FAIL"}:
        findings.append(
            f"pytest gate receipt is not terminal: {receipt.get('state')!r}"
        )
    started = _parse_receipt_time(receipt.get("started_at"), "started_at", findings)
    finished = _parse_receipt_time(receipt.get("finished_at"), "finished_at", findings)
    if started is not None and finished is not None and finished < started:
        findings.append("pytest gate finished_at precedes started_at")
    run_binding = _receipt_artifact_root(root, receipt, findings)
    if run_binding is None:
        return findings
    run_id, artifact_root = run_binding
    execution_root = Path(str(receipt.get("execution_root") or ""))
    if not execution_root.is_absolute() or ".." in execution_root.parts:
        findings.append("pytest gate execution_root is absent or unsafe")
        return findings
    if execution_root != root:
        findings.append(
            "pytest gate execution_root does not match the validation root "
            f"({execution_root} != {root})"
        )
        return findings
    source_commit = str(receipt.get("source_commit") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        findings.append("pytest gate source_commit is absent or not full lowercase SHA-1")
    else:
        try:
            current_commit = git_source_commit(root)
        except ValueError:
            current_commit = None
        if current_commit is not None and source_commit != current_commit:
            findings.append(
                "pytest gate source_commit does not match current execution root"
            )
    if receipt.get("source_worktree_clean") is not True:
        findings.append("pytest gate source worktree was not clean")

    current_manifest = input_manifest(root)
    manifest = receipt.get("input_manifest") or {}
    before_sha = str(receipt.get("input_manifest_before_sha256") or "")
    after_sha = str(manifest.get("sha256") or "")
    input_identity_stable = (
        before_sha == after_sha
        and after_sha == current_manifest["sha256"]
        and receipt.get("input_stable_during_run") is True
    )
    if not input_identity_stable:
        findings.append(
            "pytest gate input identity is not stable/current "
            f"(before={before_sha} after={after_sha} "
            f"current={current_manifest['sha256']})"
        )
    delta = receipt.get("input_manifest_delta")
    if delta != {"added": [], "removed": [], "changed": []}:
        findings.append(f"pytest gate input delta is nonempty or malformed: {delta!r}")

    expected_files = discover_test_files(root)
    expected_memory_singletons = memory_singleton_test_files(root, expected_files)
    if receipt.get("memory_singleton_files") != expected_memory_singletons:
        findings.append(
            "pytest gate memory_singleton_files do not match marked test inputs"
        )
    requested = int(receipt.get("requested_minimum_shards") or 0)
    plan = receipt.get("shard_plan")
    if not isinstance(plan, list) or not all(
        isinstance(group, list) for group in plan
    ):
        findings.append("pytest gate receipt has no complete shard plan")
        plan = []
    elif requested <= 0:
        findings.append("pytest gate requested shard count is invalid")
    else:
        findings.extend(
            validate_shard_plan(
                root,
                expected_files,
                plan,
                requested_minimum_shards=requested,
                max_files_per_shard=int(
                    receipt.get("max_files_per_shard") or MAX_FILES_PER_SHARD
                ),
            )
        )
    recorded_plan_sha = str(receipt.get("shard_plan_sha256") or "")
    if not plan or recorded_plan_sha != shard_plan_sha256(plan):
        findings.append("pytest gate shard_plan_sha256 does not bind the full plan")

    try:
        identity = json.loads(
            (artifact_root / "run_identity.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        identity = {}
        findings.append("pytest gate run_identity.json is absent or unreadable")
    identity_expected = {
        "schema": receipt.get("schema"),
        "run_id": run_id,
        "execution_root": receipt.get("execution_root"),
        "source_commit": receipt.get("source_commit"),
        "source_worktree_clean": receipt.get("source_worktree_clean"),
        "started_at": receipt.get("started_at"),
        "input_manifest_sha256": receipt.get("input_manifest_before_sha256"),
        "test_file_count": receipt.get("test_file_count"),
        "requested_minimum_shards": receipt.get("requested_minimum_shards"),
        "shard_count": receipt.get("shard_count"),
        "max_files_per_shard": receipt.get("max_files_per_shard"),
        "max_shard_seconds": receipt.get("max_shard_seconds"),
        "max_shard_rss_kib": receipt.get("max_shard_rss_kib"),
        "memory_singleton_files": receipt.get("memory_singleton_files"),
        "shard_plan": receipt.get("shard_plan"),
        "shard_plan_sha256": receipt.get("shard_plan_sha256"),
    }
    for field, expected in identity_expected.items():
        if identity.get(field) != expected:
            findings.append(f"pytest gate run identity disagrees on {field}")
    expected_retired_nodeids = sorted(retired_test_nodeids(root))
    if sorted(receipt.get("retired_test_nodeids") or []) != expected_retired_nodeids:
        findings.append(
            "pytest gate retired_test_nodeids do not match the current "
            "hash-bound retirement inventory"
        )
    if int(receipt.get("test_file_count") or -1) != len(expected_files):
        findings.append(
            "pytest gate test_file_count does not match discovery "
            f"({receipt.get('test_file_count')} != {len(expected_files)})"
        )
    expected_shards = (
        recommended_shard_count(
            len(expected_files),
            minimum=requested,
            memory_singleton_count=len(expected_memory_singletons),
        )
        if expected_files and requested > 0
        else 0
    )
    shards = receipt.get("shards")
    if not isinstance(shards, list) or not shards:
        findings.append("pytest gate receipt has no shards")
        return findings
    if (
        int(receipt.get("shard_count") or -1) != len(shards)
        or len(shards) != expected_shards
    ):
        findings.append(
            "pytest gate shard cardinality mismatch "
            f"(declared={receipt.get('shard_count')} observed={len(shards)} "
            f"expected={expected_shards})"
        )
    expected_cardinality = {
        "expected": expected_shards,
        "declared": int(receipt.get("shard_count") or -1),
        "observed": len(shards),
        "all_equal": (
            expected_shards
            == int(receipt.get("shard_count") or -1)
            == len(shards)
        ),
    }
    if receipt.get("shard_cardinality") != expected_cardinality:
        findings.append(
            "pytest gate shard_cardinality is absent or disagrees with "
            f"recomputation ({receipt.get('shard_cardinality')!r} != "
            f"{expected_cardinality!r})"
        )

    observed_files: list[str] = []
    all_node_ids: list[str] = []
    reparsed_totals = Counter()
    incomplete_shards: set[int] = set()
    for position, shard in enumerate(shards):
        if not isinstance(shard, dict):
            findings.append(f"pytest shard {position} is not an object")
            continue
        index = shard.get("index")
        if index != position:
            findings.append(
                f"pytest shard index/order mismatch at {position}: {index!r}"
            )
        files = shard.get("files")
        if not isinstance(files, list) or not files:
            findings.append(f"pytest shard {position} has no file list")
            continue
        files = [str(value) for value in files]
        observed_files.extend(files)
        if position >= len(plan) or files != plan[position]:
            findings.append(
                f"pytest shard {position} does not match the bound plan prefix"
            )
            incomplete_shards.add(position)
        if (
            int(shard.get("file_count") or -1) != len(files)
            or len(files) > MAX_FILES_PER_SHARD
        ):
            findings.append(
                f"pytest shard {position} file_count/cap mismatch "
                f"({shard.get('file_count')} recorded, {len(files)} actual)"
            )
        if shard.get("schema") != SCHEMA:
            findings.append(f"pytest shard {position} schema mismatch")
        if shard.get("run_id") != run_id:
            findings.append(f"pytest shard {position} run identity mismatch")
            incomplete_shards.add(position)
        if shard.get("execution_root") != str(execution_root):
            findings.append(f"pytest shard {position} execution root mismatch")
            incomplete_shards.add(position)
        if shard.get("source_commit") != source_commit:
            findings.append(f"pytest shard {position} source commit mismatch")
            incomplete_shards.add(position)
        if shard.get("shard_plan_sha256") != recorded_plan_sha:
            findings.append(f"pytest shard {position} plan identity mismatch")
            incomplete_shards.add(position)
        if shard.get("input_manifest_sha256") != before_sha:
            findings.append(f"pytest shard {position} input identity mismatch")
        if (
            shard.get("timed_out") is not False
            or shard.get("rss_exceeded") is not False
        ):
            findings.append(f"pytest shard {position} has an infrastructure stop")
            incomplete_shards.add(position)
        if not shard_exit_is_admissible(shard):
            findings.append(
                f"pytest shard {position} inadmissible exit_code="
                f"{shard.get('exit_code')}"
            )
            incomplete_shards.add(position)
        if (
            shard.get("max_shard_seconds") != MAX_SHARD_SECONDS
            or shard.get("max_shard_rss_kib") != MAX_SHARD_RSS_KIB
            or float(shard.get("duration_s") or 0.0) >= MAX_SHARD_SECONDS
        ):
            findings.append(f"pytest shard {position} execution budget mismatch")
            incomplete_shards.add(position)

        shard_started = _parse_receipt_time(
            shard.get("started_at"), f"shard {position} started_at", findings
        )
        shard_finished = _parse_receipt_time(
            shard.get("finished_at"), f"shard {position} finished_at", findings
        )
        if (
            shard_started is None
            or shard_finished is None
            or shard_finished < shard_started
            or (started is not None and shard_started < started)
            or (finished is not None and shard_finished > finished)
        ):
            findings.append(f"pytest shard {position} run time binding mismatch")
            incomplete_shards.add(position)

        expected_junit_path = artifact_root / "shards" / f"shard_{position:03d}.xml"
        expected_log_path = artifact_root / "shards" / f"shard_{position:03d}.log"
        expected_junit = expected_junit_path.relative_to(root).as_posix()
        expected_log = expected_log_path.relative_to(root).as_posix()
        if shard.get("junit") != expected_junit or shard.get("log") != expected_log:
            findings.append(f"pytest shard {position} artifact path mismatch")
            incomplete_shards.add(position)
            continue
        junit = expected_junit_path
        log = expected_log_path
        for label, path in (("junit", junit), ("log", log)):
            expected_sha = shard.get(f"{label}_sha256")
            if not path.is_file() or not expected_sha or _sha256(path) != expected_sha:
                findings.append(
                    f"pytest shard {position} {label} is absent or hash-mismatched"
                )
                incomplete_shards.add(position)
                continue
            mtime = path.stat().st_mtime
            if (
                shard_started is not None
                and shard_finished is not None
                and not (shard_started - 1.0 <= mtime <= shard_finished + 1.0)
            ):
                findings.append(
                    f"pytest shard {position} {label} is outside shard time bounds"
                )
                incomplete_shards.add(position)
        if not junit.is_file() or _sha256(junit) != shard.get("junit_sha256"):
            incomplete_shards.add(position)
            continue
        try:
            counts, node_ids, junit_errors = _junit_terminal_inventory(
                junit, files=files
            )
        except (ET.ParseError, OSError, ValueError) as exc:
            findings.append(f"pytest shard {position} JUnit parse failed: {exc}")
            incomplete_shards.add(position)
            continue
        for error in junit_errors:
            findings.append(f"pytest shard {position}: {error}")
        recorded_counts = {
            key: int((shard.get("counts") or {}).get(key) or 0)
            for key in ("tests", "failures", "errors", "skipped")
        }
        if recorded_counts != counts:
            findings.append(
                f"pytest shard {position} stored counts {recorded_counts} "
                f"do not match reparsed {counts}"
            )
        if counts["tests"] < sum(
            counts[key] for key in ("failures", "errors", "skipped")
        ):
            findings.append(f"pytest shard {position} has impossible count arithmetic")
        if shard.get("exit_code") == 0 and (counts["failures"] or counts["errors"]):
            findings.append(f"pytest shard {position} exit 0 conflicts with red JUnit")
        if shard.get("exit_code") == 1 and not (counts["failures"] or counts["errors"]):
            findings.append(
                f"pytest shard {position} exit 1 lacks a red JUnit terminal"
            )
        all_node_ids.extend(node_ids)
        reparsed_totals.update(counts)

        command = shard.get("command")
        expected_prefix = [
            str(execution_root / "coordharness/.venv/bin/python"),
            "-m",
            "pytest",
            "-q",
            "-c",
            str(execution_root / "pytest.ini"),
            "--rootdir",
            str(execution_root),
            "--continue-on-collection-errors",
        ]
        expected_command = [
            *expected_prefix,
            *files,
            f"--junitxml={execution_root / expected_junit}",
        ]
        if command != expected_command:
            findings.append(f"pytest shard {position} command/root binding mismatch")

    if sorted(observed_files) != expected_files:
        findings.append("pytest gate discovered-file coverage is not exact")
    duplicate_nodes = sorted(
        node for node, count in Counter(all_node_ids).items() if count > 1
    )
    if duplicate_nodes:
        findings.append(
            "pytest gate contains duplicate canonical testcase identities "
            f"(count={len(duplicate_nodes)} sample={duplicate_nodes[:10]})"
        )
    recorded_totals = {
        key: int((receipt.get("totals") or {}).get(key) or 0)
        for key in ("tests", "failures", "errors", "skipped")
    }
    exact_totals = {
        key: int(reparsed_totals[key])
        for key in ("tests", "failures", "errors", "skipped")
    }
    expected_complete = (
        not incomplete_shards
        and input_identity_stable
        and expected_cardinality["all_equal"]
    )
    expected_completion = (
        "COMPLETE_GREEN"
        if expected_complete
        and exact_totals["failures"] == 0
        and exact_totals["errors"] == 0
        else "COMPLETE_RED"
        if expected_complete
        else "INCOMPLETE_INFRA"
        if incomplete_shards
        else "INCOMPLETE_CARDINALITY"
        if not expected_cardinality["all_equal"]
        else "INCOMPLETE_INPUT"
    )
    if receipt.get("completion_state") != expected_completion:
        findings.append(
            "pytest gate completion_state drifted "
            f"({receipt.get('completion_state')!r} != {expected_completion!r})"
        )
    if receipt.get("totals_admissible") is not expected_complete:
        findings.append("pytest gate totals_admissible drifted")
    if receipt.get("infrastructure_stops") != sorted(incomplete_shards):
        findings.append("pytest gate infrastructure stop inventory drifted")
    if expected_complete:
        if recorded_totals != exact_totals:
            findings.append(
                f"pytest gate totals {recorded_totals} do not match reparsed "
                f"{exact_totals}"
            )
        if recorded_totals["tests"] < sum(
            recorded_totals[key] for key in ("failures", "errors", "skipped")
        ):
            findings.append("pytest gate totals have impossible count arithmetic")
        if receipt.get("partial_junit_totals") is not None:
            findings.append("complete pytest gate carries partial totals")
    else:
        if receipt.get("totals") is not None:
            findings.append("incomplete pytest gate publishes global totals")
        if receipt.get("partial_junit_totals") != exact_totals:
            findings.append("pytest gate partial JUnit totals drifted")
    classifications = receipt.get("outcome_classifications")
    recomputed_classifications = classify_receipt_outcomes(root, receipt)
    if classifications != recomputed_classifications:
        findings.append("pytest gate outcome classifications do not match JUnit replay")
    elif expected_complete and any(
        row.get("class") != "REAL_REGRESSION" for row in classifications or []
    ):
        findings.append("pytest gate contains non-regression outcome classifications")
    expected_scope = "COMPLETE" if expected_complete else "PARTIAL"
    if receipt.get("outcome_classification_scope") != expected_scope:
        findings.append("pytest gate outcome classification scope drifted")
    classification_counts = receipt.get("outcome_classification_counts") or {}
    expected_classification_counts = {
        label: sum(row.get("class") == label for row in recomputed_classifications)
        for label in ("REAL_REGRESSION", "MEMORY_KILL", "STALE_TELEMETRY")
    }
    if classification_counts != expected_classification_counts:
        findings.append("pytest gate outcome classification counts drifted")
    return findings


def validate_receipt(repo_root: Path, receipt: dict[str, Any]) -> list[str]:

    findings: list[str] = []
    if receipt.get("schema") != SCHEMA:
        findings.append("unsupported pytest gate receipt schema")
        return findings
    if receipt.get("state") != "PASS":
        findings.append(f"pytest gate state is {receipt.get('state')!r}, not PASS")
    integrity_findings = audit_receipt_integrity(repo_root, receipt)
    findings.extend(integrity_findings)
    current_manifest = input_manifest(repo_root)
    recorded_manifest = receipt.get("input_manifest") or {}
    if recorded_manifest.get("sha256") != current_manifest["sha256"]:
        findings.append(
            "pytest gate receipt is stale for the current source/test tree "
            f"(receipt={recorded_manifest.get('sha256')} current={current_manifest['sha256']})"
        )
    expected_files = discover_test_files(repo_root)
    shards = receipt.get("shards")
    if not isinstance(shards, list) or not shards:
        findings.append("pytest gate receipt has no shards")
        return findings
    observed: list[str] = []
    shard_outcomes: list[dict[str, Any]] = []
    for index, shard in enumerate(shards):
        if not isinstance(shard, dict):
            findings.append(f"pytest shard {index} is not an object")
            continue
        files = shard.get("files")
        if not isinstance(files, list):
            findings.append(f"pytest shard {index} has no file list")
            continue
        observed.extend(str(value) for value in files)
        counts = shard.get("counts") or {}
        failures = int(counts.get("failures") or 0)
        errors = int(counts.get("errors") or 0)
        exit_code = shard.get("exit_code")
        rss_exceeded = bool(shard.get("rss_exceeded"))
        duration_s = float(shard.get("duration_s") or 0.0)
        watchdog_exceeded = duration_s >= MAX_SHARD_SECONDS
        nonzero_exit = exit_code != 0 and not shard_exit_is_zero_selection(shard)
        if nonzero_exit or failures or errors or rss_exceeded or watchdog_exceeded:
            detail: dict[str, Any] = {
                "index": index,
                "exit_code": exit_code,
                "failures": failures,
                "errors": errors,
            }
            if rss_exceeded:
                detail["rss_exceeded"] = True
                detail["peak_rss_kib"] = int(shard.get("peak_rss_kib") or 0)
            if watchdog_exceeded:
                detail["watchdog_exceeded"] = True
                detail["duration_s"] = round(duration_s, 1)
            shard_outcomes.append(detail)
    if sorted(observed) != expected_files:
        missing = sorted(set(expected_files) - set(observed))
        extra = sorted(set(observed) - set(expected_files))
        duplicates = sorted(path for path in set(observed) if observed.count(path) > 1)
        collection_roots = tuple(path.as_posix() for path in configured_test_roots(repo_root))
        expected_by_root = {
            root: sum(path.startswith(f"{root}/") for path in expected_files)
            for root in collection_roots
        }
        observed_by_root = {
            root: sum(path.startswith(f"{root}/") for path in observed)
            for root in collection_roots
        }
        missing_by_root = {
            root: sum(path.startswith(f"{root}/") for path in missing)
            for root in collection_roots
        }
        findings.append(
            "pytest shard file coverage mismatch "
            f"(missing={len(missing)} extra={len(extra)} "
            f"duplicates={len(duplicates)}; "
            f"collection_roots={list(collection_roots)} "
            f"expected_by_root={expected_by_root} "
            f"observed_by_root={observed_by_root} "
            f"missing_by_root={missing_by_root}; "
            f"missing_sample={missing[:10]} extra_sample={extra[:10]} "
            f"duplicates_sample={duplicates[:10]})"
        )
    totals = receipt.get("totals")
    if totals is None:
        partial = receipt.get("partial_junit_totals") or {}
        findings.append(
            "pytest gate has no admissible global totals; "
            f"partial JUnit collected {int(partial.get('tests') or 0)} test cases"
        )
    elif int(totals.get("tests") or 0) <= 0:
        findings.append("pytest gate receipt collected zero test cases")
    if totals is not None and (
        int(totals.get("failures") or 0) or int(totals.get("errors") or 0)
    ):
        findings.append(
            f"pytest gate totals failures={totals.get('failures')} "
            f"errors={totals.get('errors')}"
        )
    if shard_outcomes:
        memory_shards = [
            row["index"] for row in shard_outcomes if row.get("rss_exceeded")
        ]
        watchdog_shards = [
            row["index"] for row in shard_outcomes if row.get("watchdog_exceeded")
        ]
        budget_detail = ""
        if memory_shards:
            budget_detail += f"; shards {memory_shards} exceeded memory budget"
        if watchdog_shards:
            budget_detail += f"; shards {watchdog_shards} exceeded watchdog budget"
        findings.append(
            "pytest gate shard outcome summary "
            f"({len(shard_outcomes)} non-green shard(s){budget_detail}; "
            f"details={json.dumps(shard_outcomes, sort_keys=True, separators=(',', ':'))})"
        )

    priority_needles = (
        "file coverage mismatch",
        "discovered-file coverage is not exact",
        "test_file_count does not match discovery",
        "shard cardinality mismatch",
        "shard_cardinality",
        "shard plan",
        "shard_plan",
    )

    def _finding_rank(finding: str) -> int:
        if any(needle in finding for needle in priority_needles):
            return 0
        if finding.startswith("pytest gate shard outcome summary"):
            return 2
        return 1

    return sorted(findings, key=_finding_rank)
