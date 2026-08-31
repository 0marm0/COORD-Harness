
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from coordharness.testing import pytest_gate as gate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _run_pytest_and_get_junit(
    execution_root: Path, files: list[str], junit_path: Path, log_path: Path
) -> dict[str, int]:
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--rootdir",
            str(execution_root),
            "--continue-on-collection-errors",
            *files,
            f"--junitxml={junit_path}",
        ],
        cwd=execution_root,
        capture_output=True,
        text=True,
    )
    log_path.write_text(result.stdout + result.stderr)
    suite = ET.parse(junit_path).getroot().find(".//testsuite")
    assert suite is not None
    return {key: int(suite.attrib.get(key, 0)) for key in ("tests", "failures", "errors", "skipped")}


def _build_consistent_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, requested_minimum_shards: int = 2
) -> tuple[Path, dict[str, Any]]:
    """Builds a real repo + real pytest-produced JUnit + a receipt that
    `audit_receipt_integrity`/`validate_receipt` accept with zero findings.

    Uses a non-default test-root name ("suite" via COORD_PYTEST_ROOTS_JSON) so
    the JUnit classnames ("suite.test_alpha") do not collide with the module's
    hardcoded "tests."-prefix legacy-classname guard -- see
    test_audit_receipt_integrity_flags_tests_dot_prefixed_classnames below,
    which exercises that guard on purpose using the default "tests" root.
    """
    monkeypatch.setenv("COORD_PYTEST_ROOTS_JSON", json.dumps(["suite"]))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "coord-harness-test@example.invalid")
    _git(root, "config", "user.name", "coord-harness-test")
    (root / "suite").mkdir()
    (root / "suite" / "test_alpha.py").write_text(
        "def test_one():\n    assert True\n\n\ndef test_two():\n    assert True\n"
    )
    (root / "suite" / "test_beta.py").write_text("def test_three():\n    assert True\n")
    (root / "pytest.ini").write_text("[pytest]\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    execution_root = root.resolve()

    manifest = gate.input_manifest(execution_root)
    test_files = gate.discover_test_files(execution_root)
    shard_count = gate.recommended_shard_count(len(test_files), minimum=requested_minimum_shards)
    groups = gate.shard_test_files(execution_root, test_files, shard_count)
    plan_sha = gate.shard_plan_sha256(groups)
    source_commit = gate.git_source_commit(execution_root)

    run_id = "run1"
    artifact_root_rel = Path(".coordharness/pytest_gate/artifacts") / run_id
    artifact_root = execution_root / artifact_root_rel
    shards_dir = artifact_root / "shards"
    shards_dir.mkdir(parents=True)

    started = datetime.now(timezone.utc)
    shards: list[dict[str, Any]] = []
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
    for index, files in enumerate(groups):
        junit_path = shards_dir / f"shard_{index:03d}.xml"
        log_path = shards_dir / f"shard_{index:03d}.log"
        counts = _run_pytest_and_get_junit(execution_root, files, junit_path, log_path)
        finished = datetime.now(timezone.utc)
        command = [
            *expected_prefix,
            *files,
            f"--junitxml={execution_root / junit_path.relative_to(execution_root)}",
        ]
        shards.append(
            {
                "index": index,
                "files": files,
                "file_count": len(files),
                "schema": gate.SCHEMA,
                "run_id": run_id,
                "execution_root": str(execution_root),
                "source_commit": source_commit,
                "shard_plan_sha256": plan_sha,
                "input_manifest_sha256": manifest["sha256"],
                "timed_out": False,
                "rss_exceeded": False,
                "exit_code": 0,
                "max_shard_seconds": gate.MAX_SHARD_SECONDS,
                "max_shard_rss_kib": gate.MAX_SHARD_RSS_KIB,
                "duration_s": 0.5,
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
                "junit": str(junit_path.relative_to(execution_root)),
                "log": str(log_path.relative_to(execution_root)),
                "junit_sha256": _sha256(junit_path),
                "log_sha256": _sha256(log_path),
                "counts": counts,
                "command": command,
                "peak_rss_kib": 1000,
            }
        )

    finished_all = datetime.now(timezone.utc)
    totals = {
        key: sum(shard["counts"][key] for shard in shards)
        for key in ("tests", "failures", "errors", "skipped")
    }
    memory_singletons = gate.memory_singleton_test_files(execution_root, test_files)
    expected_cardinality = {
        "expected": shard_count,
        "declared": shard_count,
        "observed": len(shards),
        "all_equal": True,
    }

    run_identity = {
        "schema": gate.SCHEMA,
        "run_id": run_id,
        "execution_root": str(execution_root),
        "source_commit": source_commit,
        "source_worktree_clean": True,
        "started_at": started.isoformat(),
        "input_manifest_sha256": manifest["sha256"],
        "test_file_count": len(test_files),
        "requested_minimum_shards": requested_minimum_shards,
        "shard_count": shard_count,
        "max_files_per_shard": gate.MAX_FILES_PER_SHARD,
        "max_shard_seconds": gate.MAX_SHARD_SECONDS,
        "max_shard_rss_kib": gate.MAX_SHARD_RSS_KIB,
        "memory_singleton_files": memory_singletons,
        "shard_plan": groups,
        "shard_plan_sha256": plan_sha,
    }
    (artifact_root / "run_identity.json").write_text(json.dumps(run_identity))

    receipt = {
        "schema": gate.SCHEMA,
        "run_id": run_id,
        "artifact_root": str(artifact_root_rel),
        "execution_root": str(execution_root),
        "source_commit": source_commit,
        "source_worktree_clean": True,
        "started_at": started.isoformat(),
        "state": "PASS",
        "finished_at": finished_all.isoformat(),
        "shard_count": shard_count,
        "shard_cardinality": expected_cardinality,
        "shard_plan_sha256": plan_sha,
        "shard_plan": groups,
        "stopped_early": False,
        "totals": totals,
        "partial_junit_totals": None,
        "totals_admissible": True,
        "completion_state": "COMPLETE_GREEN",
        "infrastructure_stops": [],
        "outcome_classification_counts": {
            "REAL_REGRESSION": 0,
            "MEMORY_KILL": 0,
            "STALE_TELEMETRY": 0,
        },
        "outcome_classifications": [],
        "outcome_classification_scope": "COMPLETE",
        "requested_minimum_shards": requested_minimum_shards,
        "max_files_per_shard": gate.MAX_FILES_PER_SHARD,
        "max_shard_seconds": gate.MAX_SHARD_SECONDS,
        "max_shard_rss_kib": gate.MAX_SHARD_RSS_KIB,
        "test_file_count": len(test_files),
        "memory_singleton_files": memory_singletons,
        "retired_test_nodeids": [],
        "input_manifest": manifest,
        "input_manifest_before_sha256": manifest["sha256"],
        "input_stable_during_run": True,
        "input_manifest_delta": {"added": [], "removed": [], "changed": []},
        "shards": shards,
    }
    return execution_root, receipt


# --- consistent multi-shard receipt: the validator accepts it --------------


def test_validate_receipt_accepts_a_consistent_multi_shard_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution_root, receipt = _build_consistent_receipt(tmp_path, monkeypatch)
    assert len(receipt["shards"]) == 2  # MEASURED: two real pytest-executed shards
    assert gate.audit_receipt_integrity(execution_root, receipt) == []
    assert gate.validate_receipt(execution_root, receipt) == []


# --- REJECTS: missing shard --------------------------------------------


def test_validate_receipt_rejects_a_missing_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution_root, receipt = _build_consistent_receipt(tmp_path, monkeypatch)
    tampered = json.loads(json.dumps(receipt))
    tampered["shards"].pop()  # drop the second shard's files/coverage entirely
    findings = gate.validate_receipt(execution_root, tampered)
    assert findings, "dropping a shard must not validate"
    assert any("file coverage mismatch" in finding for finding in findings)
    audit_findings = gate.audit_receipt_integrity(execution_root, tampered)
    assert any("shard cardinality mismatch" in finding for finding in audit_findings)


# --- REJECTS: count mismatch (recorded counts vs. reparsed JUnit) ----------


def test_audit_receipt_integrity_rejects_a_tampered_shard_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution_root, receipt = _build_consistent_receipt(tmp_path, monkeypatch)
    tampered = json.loads(json.dumps(receipt))
    # The JUnit file and its hash are untouched; only the recorded summary count
    # is lied about, so this specifically exercises the counts-vs-reparse check
    # rather than the hash check below.
    tampered["shards"][0]["counts"]["tests"] = 99
    findings = gate.audit_receipt_integrity(execution_root, tampered)
    assert any(
        "stored counts" in finding and "do not match reparsed" in finding
        for finding in findings
    )


# --- REJECTS: tampered hash (recorded hash vs. real file on disk) ----------


def test_audit_receipt_integrity_rejects_a_tampered_junit_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution_root, receipt = _build_consistent_receipt(tmp_path, monkeypatch)
    tampered = json.loads(json.dumps(receipt))
    # The JUnit file on disk is untouched; only the recorded hash is corrupted,
    # so a hash-mismatch is the only thing this should trip.
    tampered["shards"][0]["junit_sha256"] = "0" * 64
    findings = gate.audit_receipt_integrity(execution_root, tampered)
    assert any(
        "junit is absent or hash-mismatched" in finding for finding in findings
    )


def test_audit_receipt_integrity_rejects_a_junit_file_edited_after_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution_root, receipt = _build_consistent_receipt(tmp_path, monkeypatch)
    tampered = json.loads(json.dumps(receipt))
    junit_path = execution_root / tampered["shards"][0]["junit"]
    # Edit the actual bytes on disk without touching the recorded hash: the
    # opposite direction of the previous test, same defect class.
    junit_path.write_text(junit_path.read_text() + "<!-- tampered -->")
    findings = gate.audit_receipt_integrity(execution_root, tampered)
    assert any(
        "junit is absent or hash-mismatched" in finding for finding in findings
    )


# --- the "tests."-prefix legacy-classname guard, on purpose -----------------


def test_audit_receipt_integrity_flags_tests_dot_prefixed_classnames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Uses the module's *default* collection root ("tests"), which is exactly
    # what makes real pytest emit classnames like "tests.test_alpha" -- and the
    # module hardcodes a rejection of any classname starting with "tests.".
    # This is why _build_consistent_receipt above deliberately uses a "suite"
    # root instead: under the default root, no receipt from a real pytest run
    # can pass this check. Documented here as observed behavior, not asserted
    # to be correct or incorrect.
    monkeypatch.delenv("COORD_PYTEST_ROOTS_JSON", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "coord-harness-test@example.invalid")
    _git(root, "config", "user.name", "coord-harness-test")
    (root / "tests").mkdir()
    (root / "tests" / "test_alpha.py").write_text("def test_one():\n    assert True\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    execution_root = root.resolve()
    junit_path = tmp_path / "shard_000.xml"
    _run_pytest_and_get_junit(execution_root, ["tests/test_alpha.py"], junit_path, tmp_path / "shard_000.log")
    counts, node_ids, errors = gate._junit_terminal_inventory(
        junit_path, files=["tests/test_alpha.py"]
    )
    assert any("noncanonical legacy classname" in error for error in errors)


# --- shard admissibility / zero-selection classifiers -----------------------


def test_shard_exit_is_zero_selection_true_only_for_a_clean_empty_shard() -> None:
    empty_shard = {
        "exit_code": 5,
        "timed_out": False,
        "rss_exceeded": False,
        "junit_sha256": "abc",
        "counts": {"tests": 0, "failures": 0, "errors": 0, "skipped": 0},
    }
    assert gate.shard_exit_is_zero_selection(empty_shard) is True
    assert gate.shard_exit_is_admissible(empty_shard) is True
    non_empty = dict(empty_shard, counts={"tests": 1, "failures": 0, "errors": 0, "skipped": 0})
    assert gate.shard_exit_is_zero_selection(non_empty) is False


def test_shard_exit_is_admissible_accepts_only_zero_one_or_zero_selection() -> None:
    assert gate.shard_exit_is_admissible({"exit_code": 0}) is True
    assert gate.shard_exit_is_admissible({"exit_code": 1}) is True
    assert gate.shard_exit_is_admissible({"exit_code": 2}) is False


# --- input_manifest / discover_test_files / memory_singleton_test_files -----


def test_input_manifest_excludes_configured_parts_and_hashes_the_rest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    (root / "keep").mkdir(parents=True)
    (root / "keep" / "a.py").write_text("x = 1\n")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "a.pyc").write_bytes(b"\x00")
    manifest = gate.input_manifest(root)
    paths = {row["path"] for row in manifest["files"]}
    assert "keep/a.py" in paths
    assert not any(path.startswith("__pycache__/") for path in paths)
    assert manifest["file_count"] == len(manifest["files"])


def test_discover_test_files_finds_only_test_prefixed_or_suffixed_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COORD_PYTEST_ROOTS_JSON", raising=False)
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_a.py").write_text("")
    (root / "tests" / "b_test.py").write_text("")
    (root / "tests" / "helpers.py").write_text("")
    found = gate.discover_test_files(root)
    assert found == ["tests/b_test.py", "tests/test_a.py"]


def test_memory_singleton_test_files_detects_the_marker_line(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "singleton.py").write_text(gate.MEMORY_SINGLETON_MARKER + "\n")
    (root / "plain.py").write_text("def test_x():\n    pass\n")
    found = gate.memory_singleton_test_files(root, ["singleton.py", "plain.py"])
    assert found == ["singleton.py"]


def test_retired_test_helpers_are_currently_empty_placeholders(tmp_path: Path) -> None:
    # Both retirement lookups are hardcoded to the empty set in this version of
    # the module; asserted explicitly so a future change to real retirement
    # data is caught by this test rather than silently changing gate behavior.
    assert gate.retired_test_basenames(tmp_path) == frozenset()
    assert gate.retired_test_nodeids(tmp_path) == frozenset()


# --- shard_test_files / recommended_shard_count / shard_plan_sha256 --------


def test_shard_test_files_isolates_memory_singletons_into_their_own_shard(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "test_singleton.py").write_text(
        gate.MEMORY_SINGLETON_MARKER + "\ndef test_a():\n    pass\n"
    )
    (root / "test_ordinary.py").write_text("def test_b():\n    pass\n")
    groups = gate.shard_test_files(
        root, ["test_singleton.py", "test_ordinary.py"], shard_count=2
    )
    assert groups[0] == ["test_singleton.py"]
    assert groups[1] == ["test_ordinary.py"]


def test_shard_test_files_rejects_invalid_shard_counts(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        gate.shard_test_files(tmp_path, ["a.py"], shard_count=0)
    with pytest.raises(ValueError):
        gate.shard_test_files(tmp_path, [], shard_count=1)


def test_recommended_shard_count_respects_minimum_and_capacity() -> None:
    assert gate.recommended_shard_count(10, minimum=2, max_files_per_shard=8) == 2
    assert gate.recommended_shard_count(20, minimum=1, max_files_per_shard=8) == 3
    with pytest.raises(ValueError):
        gate.recommended_shard_count(0)


def test_shard_plan_sha256_is_order_sensitive_and_deterministic() -> None:
    plan_a = [["x.py"], ["y.py"]]
    plan_b = [["y.py"], ["x.py"]]
    assert gate.shard_plan_sha256(plan_a) == gate.shard_plan_sha256(plan_a)
    assert gate.shard_plan_sha256(plan_a) != gate.shard_plan_sha256(plan_b)


# --- git_source_commit -------------------------------------------------


def test_git_source_commit_returns_the_full_lowercase_sha(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "coord-harness-test@example.invalid")
    _git(repo, "config", "user.name", "coord-harness-test")
    (repo / "f.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    commit = gate.git_source_commit(repo)
    assert len(commit) == 40
    assert commit == commit.lower()


def test_git_source_commit_raises_outside_a_git_repository(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    with pytest.raises(ValueError):
        gate.git_source_commit(not_a_repo)


# --- pytest_gate_paths / configured_test_roots ------------------------------


def test_pytest_gate_paths_defaults_under_dot_coordharness(tmp_path: Path) -> None:
    receipt_path, running_path = gate.pytest_gate_paths(tmp_path, env={})
    assert receipt_path == tmp_path / ".coordharness/pytest_gate/receipt.json"
    assert running_path == tmp_path / ".coordharness/pytest_gate/running.json"


def test_pytest_gate_paths_refuses_escaping_configured_dir(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        gate.pytest_gate_paths(tmp_path, env={"COORD_PYTEST_GATE_DIR": "../outside"})


def test_configured_test_roots_default_is_tests(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    assert gate.configured_test_roots(tmp_path, env={}) == (Path("tests"),)


def test_configured_test_roots_reads_inline_json(tmp_path: Path) -> None:
    (tmp_path / "suite").mkdir()
    roots = gate.configured_test_roots(
        tmp_path, env={"COORD_PYTEST_ROOTS_JSON": json.dumps(["suite"])}
    )
    assert roots == (Path("suite"),)


def test_configured_test_roots_rejects_absolute_or_traversal_entries(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        gate.configured_test_roots(
            tmp_path, env={"COORD_PYTEST_ROOTS_JSON": json.dumps(["/etc"])}
        )
    with pytest.raises(ValueError):
        gate.configured_test_roots(
            tmp_path, env={"COORD_PYTEST_ROOTS_JSON": json.dumps(["../escape"])}
        )


def test_configured_test_roots_rejects_both_inline_and_file_set_together(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        gate.configured_test_roots(
            tmp_path,
            env={
                "COORD_PYTEST_ROOTS_JSON": json.dumps(["a"]),
                "COORD_PYTEST_ROOTS_FILE": "roots.json",
            },
        )


# --- runner_is_live / canonical_gate_pair_findings / reconcile_running_sidecar ---


def test_runner_is_live_is_false_for_a_dead_pid() -> None:
    # A pid that (almost certainly) does not exist on this machine.
    assert gate.runner_is_live({"runner_pid": 2**30}) is False


def test_runner_is_live_is_false_for_a_non_positive_pid() -> None:
    assert gate.runner_is_live({"runner_pid": 0}) is False
    assert gate.runner_is_live({"runner_pid": "not-a-pid"}) is False


def test_runner_is_live_rejects_a_live_pid_running_the_wrong_command() -> None:
    # Our own test process is alive, but it is not "run_pytest_gate_shards.py",
    # so the command-line cross-check must say False rather than True.
    assert gate.runner_is_live({"runner_pid": os.getpid()}) is False


def test_canonical_gate_pair_findings_flags_disagreeing_fields() -> None:
    running = {"schema": "s1", "run_id": "r1", "completed_shards": 3}
    receipt = {"schema": "s1", "run_id": "r2", "completed_shard_count": 3}
    findings = gate.canonical_gate_pair_findings(running, receipt)
    assert any("run_id" in finding for finding in findings)
    assert not any("schema" in finding for finding in findings)


def test_reconcile_running_sidecar_returns_none_without_a_sidecar(tmp_path: Path) -> None:
    assert gate.reconcile_running_sidecar(tmp_path) is None


def test_classify_receipt_outcomes_extracts_a_real_junit_failure(tmp_path: Path) -> None:
    junit_path = tmp_path / "shard_000.xml"
    junit_path.write_text(
        '<testsuite><testcase classname="pkg.mod" name="test_thing">'
        '<failure message="boom">trace</failure></testcase></testsuite>'
    )
    log_path = tmp_path / "shard_000.log"
    log_path.write_text("log body")
    receipt = {
        "shards": [
            {
                "index": 0,
                "counts": {"tests": 1, "failures": 1, "errors": 0, "skipped": 0},
                "junit": "shard_000.xml",
                "log": "shard_000.log",
                "junit_sha256": _sha256(junit_path),
                "log_sha256": _sha256(log_path),
                "rss_exceeded": False,
                "exit_code": 1,
            }
        ]
    }
    outcomes = gate.classify_receipt_outcomes(tmp_path, receipt)
    assert len(outcomes) == 1
    assert outcomes[0]["class"] == "REAL_REGRESSION"
    assert outcomes[0]["test"] == "pkg.mod::test_thing"
    assert outcomes[0]["kind"] == "failure"


def test_validate_shard_plan_flags_a_plan_that_drops_a_discovered_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("def test_a():\n    pass\n")
    (tmp_path / "b.py").write_text("def test_b():\n    pass\n")
    findings = gate.validate_shard_plan(
        tmp_path,
        ["a.py", "b.py"],
        [["a.py"]],  # b.py silently dropped from the plan
        requested_minimum_shards=1,
    )
    assert any("drops or adds discovered files" in finding for finding in findings)


def test_validate_shard_plan_flags_duplicate_files_across_groups(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def test_a():\n    pass\n")
    findings = gate.validate_shard_plan(
        tmp_path,
        ["a.py"],
        [["a.py"], ["a.py"]],
        requested_minimum_shards=1,
    )
    assert any("duplicate files" in finding for finding in findings)


def test_reconcile_running_sidecar_marks_a_dead_runner_interrupted(
    tmp_path: Path,
) -> None:
    receipt_path, running_path = gate.pytest_gate_paths(tmp_path, env={})
    running_path.parent.mkdir(parents=True)
    running_path.write_text(
        json.dumps({"schema": gate.SCHEMA, "state": "RUNNING", "runner_pid": 2**30})
    )
    result = gate.reconcile_running_sidecar(tmp_path, rewrite=True)
    assert result is not None
    assert result["state"] == "INTERRUPTED"
    assert result["reconciled_reason"] == "runner_process_not_live"
    # rewrite=True must persist the reconciled state back to disk
    assert json.loads(running_path.read_text())["state"] == "INTERRUPTED"
