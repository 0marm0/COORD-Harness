"""The drivers that took three lint modules out of the dark, and their guards.

Each module here was `referenced only by its own module and its tests` before
this change: fully written, fully tested, called by nothing. A test is not a
caller, so these tests assert the DRIVER -- the caller that makes the guard
capable of failing in front of somebody -- and each one is written so that
removing the driver turns it red.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = "coordharness.testing.verified_artifact_skip"


def _coord(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "coordharness.coord.cli", *args],
        cwd=cwd or REPO, capture_output=True, text=True, timeout=180,
    )


# --------------------------------------------------------------- fail-loud ---
def test_lint_fail_loud_reports_the_package_and_never_refuses():
    proc = _coord("lint-fail-loud")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["scan_root"] == "src/coordharness"
    # The module's own SRC_ROOT is the repository ROOT, which walks .venv and
    # build/. Scanning the package is what makes the number actionable.
    assert payload["modules_scanned"] < 200, payload["modules_scanned"]
    assert payload["mode"].startswith("REPORT-ONLY")


def test_lint_fail_loud_names_a_finding_the_frozen_baseline_does_not_excuse(tmp_path):
    """The ratchet: a count above the frozen one is named, and only that."""
    baseline = json.loads((REPO / "tools" / "fail_loud_baseline.json").read_text())
    assert baseline["counts"], "an empty baseline would make this vacuous"
    victim = sorted(baseline["counts"])[0]
    lowered = dict(baseline)
    lowered["counts"] = {k: (0 if k == victim else v) for k, v in baseline["counts"].items()}
    path = tmp_path / "lowered.json"
    path.write_text(json.dumps(lowered))

    proc = _coord("lint-fail-loud", "--baseline", str(path))
    payload = json.loads(proc.stdout)
    assert proc.returncode == 0, "report-only: a regression is still a successful read"
    assert [row["key"] for row in payload["regressions"]] == [victim]

    unchanged = _coord("lint-fail-loud")
    assert json.loads(unchanged.stdout)["regressed_keys"] == 0, (
        "the tree is at its frozen baseline; a regression here means either the "
        "baseline is stale or somebody added a silent failure"
    )


# ------------------------------------------------------------ stall-scan ---
def _stall_db(path: Path, *, final_message: str) -> Path:
    from coordharness.bootstrap import bootstrap_database

    bootstrap_database(str(path))
    conn = sqlite3.connect(path)
    now = time.time()
    cols = ("run_id,work_id,session_id,parent_session_id,started_at,finished_at,"
            "state,runner_kind")
    conn.execute(f"INSERT INTO runs({cols}) VALUES(?,?,?,?,?,?,?,?)",
                 ("run-parent", "PROBE", "claude:p", "", now - 120, now - 100, "success", "claude"))
    conn.execute(f"INSERT INTO runs({cols}) VALUES(?,?,?,?,?,?,?,?)",
                 ("run-child", "PROBE", "claude:c", "claude:p", now - 110, None, "live", "claude"))
    conn.execute("CREATE TABLE IF NOT EXISTS run_events(run_id TEXT, seq INTEGER,"
                 " category TEXT, event_type TEXT, content_json TEXT, metadata_json TEXT)")
    conn.execute("INSERT INTO run_events(run_id,seq,category,event_type,content_json,metadata_json)"
                 " VALUES(?,?,?,?,?,?)",
                 ("run-parent", 1, "message", "assistant",
                  json.dumps({"text": final_message}), "{}"))
    conn.commit()
    conn.close()
    return path


def test_stall_scan_names_a_finished_parent_that_left_a_live_child(tmp_path):
    db = _stall_db(tmp_path / "stall.db",
                   final_message="Spawned them; waiting for the subagent fleet to report back.")
    proc = _coord("stall-scan", "--db", str(db))
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["candidates"] == 1, payload
    assert payload["findings"][0]["run_id"] == "run-parent"
    assert "waiting_language_without_managed_job" in payload["findings"][0]["reasons"]
    assert payload["auto_nudge"] is False, "this surface reports; it never acts"


def test_stall_scan_stays_quiet_when_the_child_work_is_tracked(tmp_path):
    """Differential: the ONLY change is the final message naming a managed job."""
    db = _stall_db(tmp_path / "managed.db",
                   final_message="Started it as a managed background job; "
                                 "job_progress/PROBE.json is the sidecar.")
    payload = json.loads(_coord("stall-scan", "--db", str(db)).stdout)
    assert payload["candidates"] == 0, payload


# --------------------------------------------------- verified_artifact_skip ---
def test_the_unmeasured_skip_plugin_is_registered():
    options = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    assert PLUGIN in str(options.get("addopts", "")), (
        "without this registration an unmeasured guard is counted in the skip "
        "total and named nowhere"
    )


@pytest.mark.slow
def test_an_unmeasured_guard_is_named_only_while_the_plugin_is_loaded(tmp_path):
    probe = tmp_path / "test_probe_unmeasured.py"
    probe.write_text(
        "from coordharness.testing.verified_artifact_skip import skip_unmeasured\n"
        "\n\ndef test_probe():\n"
        "    skip_unmeasured(guard_id='n0902-probe', reason='synthesized poison',\n"
        "                    artifact='/nonexistent/probe.json')\n"
    )
    common = [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", str(probe)]
    with_plugin = subprocess.run(common + ["-p", PLUGIN], cwd=REPO,
                                 capture_output=True, text=True, timeout=180)
    without = subprocess.run(common + ["-p", f"no:{PLUGIN}"], cwd=REPO,
                             capture_output=True, text=True, timeout=180)
    assert "UNMEASURED guard=n0902-probe" in with_plugin.stdout
    assert "UNMEASURED guard=n0902-probe" not in without.stdout
    assert "1 skipped" in without.stdout, (
        "the ablated run still passes -- a skipped guard reads as green, which "
        "is the whole failure mode"
    )


# ------------------------------------------------------ the checker itself ---
def test_a_comment_naming_a_module_does_not_make_it_look_wired(tmp_path):
    """The entry-point heuristic reads declared values, not the whole file.

    It matched the raw pyproject text first, so the sentence explaining a
    plugin registration kept the module green after the registration itself
    was removed -- an ablation that could not turn its own guard red.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "dcc_probe", REPO / "tools" / "dark_capability_check.py")
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)

    repo = tmp_path / "repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "__init__.py").write_text("")
    (repo / "src" / "pkg" / "lonely.py").write_text("VALUE = 1\n")
    (repo / "pyproject.toml").write_text(
        "# lonely is explained here but declared nowhere\n"
        "[project]\nname = 'probe'\n[project.scripts]\nother = 'pkg.other:main'\n")
    assert "lonely" in {name for name, _ in check.dark_modules(repo)}

    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'probe'\n[project.scripts]\nlonely = 'pkg.lonely:main'\n")
    assert "lonely" not in {name for name, _ in check.dark_modules(repo)}
