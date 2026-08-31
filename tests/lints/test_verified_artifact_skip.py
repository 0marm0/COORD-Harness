
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from coordharness.testing import verified_artifact_skip as vas

_TRIPWIRE_RE = re.compile(
    r"VERIFIED_ARTIFACT_SKIP_TRIPWIRE "
    r"collected=(\d+) skipped=(\d+) rate=([\d.]+) "
    r"typed_unmeasured=(\d+) typed_unmeasured_failures=(\d+) "
    r"untyped_unmeasured=(\d+) expected_missing_guards=(\d+)"
)


def _run_real_pytest(target: Path, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "coordharness.testing.verified_artifact_skip",
            "--no-header",
            str(target),
        ],
        cwd=target,
        capture_output=True,
        text=True,
        env=env,
    )


# --- skip_unmeasured / parse_unmeasured_skip (pure round trip) -------------


def test_skip_unmeasured_raises_skipped_with_a_typed_marker(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.json"
    with pytest.raises(pytest.skip.Exception) as excinfo:
        vas.skip_unmeasured(
            guard_id="guard-a", reason="not materialized", artifact=artifact, expected=False
        )
    parsed = vas.parse_unmeasured_skip(str(excinfo.value))
    assert parsed is not None
    assert parsed.guard_id == "guard-a"
    assert parsed.expected is False
    assert parsed.reason == "not materialized"
    assert parsed.artifact == str(artifact)


def test_skip_unmeasured_raises_failed_when_expected_is_true() -> None:
    with pytest.raises(pytest.fail.Exception) as excinfo:
        vas.skip_unmeasured(guard_id="guard-b", reason="should have existed", expected=True)
    parsed = vas.parse_unmeasured_skip(str(excinfo.value))
    assert parsed is not None
    assert parsed.expected is True
    assert parsed.guard_id == "guard-b"


def test_skip_unmeasured_defaults_expected_from_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_EXPECT_VERIFIED_ARTIFACTS", "1")
    with pytest.raises(pytest.fail.Exception):
        vas.skip_unmeasured(guard_id="guard-c", reason="env-driven expected=True")
    monkeypatch.delenv("COORD_EXPECT_VERIFIED_ARTIFACTS", raising=False)
    with pytest.raises(pytest.skip.Exception):
        vas.skip_unmeasured(guard_id="guard-c", reason="env-driven expected=False")


def test_require_verified_artifact_returns_the_path_when_it_exists(tmp_path: Path) -> None:
    artifact = tmp_path / "present.json"
    artifact.write_text("{}")
    result = vas.require_verified_artifact(artifact, guard_id="guard-d")
    assert result == artifact


def test_require_verified_artifact_skips_when_the_artifact_is_absent(tmp_path: Path) -> None:
    artifact = tmp_path / "absent.json"
    with pytest.raises(pytest.skip.Exception) as excinfo:
        vas.require_verified_artifact(artifact, guard_id="guard-e")
    parsed = vas.parse_unmeasured_skip(str(excinfo.value))
    assert parsed is not None
    assert parsed.guard_id == "guard-e"
    assert parsed.reason == "required containment artifact is not materialized"
    assert parsed.artifact == str(artifact)


def test_parse_unmeasured_skip_returns_none_for_an_unmarked_reason() -> None:
    assert vas.parse_unmeasured_skip("some ordinary skip reason") is None


def test_parse_unmeasured_skip_returns_none_for_malformed_json_after_the_marker() -> None:
    assert vas.parse_unmeasured_skip(vas._MARKER + "{not valid json") is None


# --- the four pytest hooks, exercised through a real pytest run ------------


def _write_scenario(root: Path) -> None:
    (root / "verified_artifacts").mkdir(parents=True)
    (root / "verified_artifacts" / "test_guarded.py").write_text(
        "from coordharness.testing.verified_artifact_skip import (\n"
        "    require_verified_artifact,\n"
        "    skip_unmeasured,\n"
        ")\n"
        "import pytest\n"
        "\n"
        "\n"
        "def test_missing_artifact_skips():\n"
        "    require_verified_artifact('/no/such/path/artifact.json', guard_id='guard-a')\n"
        "\n"
        "\n"
        "def test_expected_missing_artifact_fails():\n"
        "    skip_unmeasured(guard_id='guard-b', reason='required artifact absent', expected=True)\n"
        "\n"
        "\n"
        "def test_untyped_skip():\n"
        "    pytest.skip('unrelated reason, no marker')\n"
        "\n"
        "\n"
        "def test_passes_normally():\n"
        "    assert True\n"
        "\n"
        "\n"
        "def test_xfail_is_ignored():\n"
        "    pytest.xfail('known broken, should not be tallied')\n"
    )
    (root / "regular").mkdir(parents=True)
    (root / "regular" / "test_plain.py").write_text(
        "import pytest\n\n\ndef test_regular_skip():\n    pytest.skip('plain skip outside verified_artifacts')\n"
    )


def test_terminal_summary_tallies_are_scoped_to_verified_artifacts_nodeids(
    tmp_path: Path,
) -> None:
    _write_scenario(tmp_path)
    result = _run_real_pytest(tmp_path)
    match = _TRIPWIRE_RE.search(result.stdout)
    assert match is not None, result.stdout
    collected, skipped, rate, typed, typed_failures, untyped, expected_guards = match.groups()
    # 5 items live under verified_artifacts/: missing-artifact skip, expected-fail,
    # untyped skip, a pass, and an xfail. The plain skip under regular/ is outside
    # the tripwire's scope (pytest_collection_finish only counts nodeids containing
    # "/verified_artifacts/") and must not move any of these numbers.
    assert collected == "5"
    assert skipped == "2"  # 1 typed skip (guard-a) + 1 untyped skip
    assert float(rate) == pytest.approx(2 / 5)
    assert typed == "1"
    assert typed_failures == "1"  # guard-b: expected=True -> pytest.fail, not skip
    assert untyped == "1"
    assert expected_guards == "1"
    assert "guard=guard-a" in result.stdout
    assert "nodeid=verified_artifacts/test_guarded.py::test_missing_artifact_skips" in result.stdout
    assert "UNMEASURED_FAILURE" in result.stdout and "guard=guard-b" in result.stdout
    assert "guard=unclassified" in result.stdout and "test_untyped_skip" in result.stdout
    # the xfailed test contributes to none of the typed/untyped/failure tallies
    assert "test_xfail_is_ignored" not in result.stdout
    # the out-of-scope plain skip never appears in the tripwire's own output
    assert "test_regular_skip" not in result.stdout
    assert result.returncode == 1  # the expected=True guard fails the run


def test_terminal_summary_is_silent_when_nothing_verified_artifact_shaped_ran(
    tmp_path: Path,
) -> None:
    only_dir = tmp_path / "plain"
    only_dir.mkdir()
    (only_dir / "test_ordinary.py").write_text("def test_x():\n    assert True\n")
    result = _run_real_pytest(tmp_path)
    assert "VERIFIED_ARTIFACT_SKIP_TRIPWIRE" not in result.stdout
    assert result.returncode == 0


def test_terminal_summary_counts_typed_skips_even_outside_the_verified_artifacts_dir(
    tmp_path: Path,
) -> None:
    # collected/rate are scoped to "/verified_artifacts/" nodeids (see above), but
    # a *typed* skip_unmeasured()/require_verified_artifact() call is tallied into
    # typed_unmeasured regardless of where the calling test file lives -- only the
    # untyped-skip bucket is nodeid-scoped. Documented here as observed behavior.
    stray = tmp_path / "elsewhere"
    stray.mkdir()
    (stray / "test_stray_guard.py").write_text(
        "from coordharness.testing.verified_artifact_skip import skip_unmeasured\n\n\n"
        "def test_stray():\n"
        "    skip_unmeasured(guard_id='guard-stray', reason='not under verified_artifacts', expected=False)\n"
    )
    result = _run_real_pytest(tmp_path)
    match = _TRIPWIRE_RE.search(result.stdout)
    assert match is not None, result.stdout
    collected, skipped, _rate, typed, _typed_failures, _untyped, _expected_guards = match.groups()
    assert collected == "0"  # nothing collected lives under a verified_artifacts/ path
    assert typed == "1"  # but the typed skip is still tallied
    assert skipped == "1"
    assert "guard=guard-stray" in result.stdout


def test_pytest_configure_resets_accumulated_state_between_runs(tmp_path: Path) -> None:
    # Runs pytest twice in this same process so the module-level accumulators
    # (populated by pytest_runtest_logreport during run 1) are directly
    # observable across the pytest_configure boundary of run 2 -- this is the
    # one place a subprocess-per-run can't demonstrate the reset, since a fresh
    # subprocess would start clean regardless of whether pytest_configure works.
    first = tmp_path / "first"
    first.mkdir()
    (first / "test_a.py").write_text(
        "from coordharness.testing.verified_artifact_skip import skip_unmeasured\n\n\n"
        "def test_x():\n    skip_unmeasured(guard_id='g1', reason='r', expected=False)\n"
    )
    second = tmp_path / "second"
    second.mkdir()
    (second / "test_b.py").write_text("def test_y():\n    assert True\n")

    rc1 = pytest.main(["-q", "-p", "coordharness.testing.verified_artifact_skip", str(first)])
    assert rc1 == 0
    assert len(vas._records) == 1
    assert vas._records[0][1].guard_id == "g1"

    rc2 = pytest.main(["-q", "-p", "coordharness.testing.verified_artifact_skip", str(second)])
    assert rc2 == 0
    # If pytest_configure had not cleared _records, run 1's guard-g1 entry would
    # still be sitting here even though run 2 never touched skip_unmeasured.
    assert vas._records == []
    assert vas._collected == 0
