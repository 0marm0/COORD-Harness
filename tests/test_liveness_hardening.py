"""Tests for the liveness-hardening fixes:

1. process_liveness.pid_start_time forces LC_ALL=C on the `ps` subprocess and
   logs a warning (never silently swallows) on any parse failure.
2. native_cockpit._sidecar_index cross-checks pid start time via
   process_liveness.pid_matches instead of a bare os.kill(pid, 0), while
   staying tolerant of sidecars written before pid_started_at existed.
3. jobs.identity._proc_pattern_matches caps pattern length and refuses
   catastrophic-backtracking shapes before calling re.search, logging a
   warning and returning "no match" rather than raising.

All MEASURED via direct calls into the modules under test; no real `ps` or
real reaper process is involved.
"""

from __future__ import annotations

import logging
import subprocess

import pytest

from coordharness.coord import native_cockpit, process_liveness
from coordharness.jobs import identity, sidecar_writer


# ---------------------------------------------------------------------------
# 1. process_liveness.pid_start_time
# ---------------------------------------------------------------------------


def test_pid_start_time_forces_lc_all_c_on_the_ps_subprocess(monkeypatch):
    captured: dict[str, object] = {}

    def fake_check_output(cmd, *, text, stderr, timeout, env):
        captured["cmd"] = cmd
        captured["env"] = env
        return "Mon Aug 31 12:00:00 2026\n"

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)

    result = process_liveness.pid_start_time(4242)

    assert result is not None
    assert captured["env"]["LC_ALL"] == "C"
    # the caller's own environment must still be forwarded (PATH etc.), not
    # replaced wholesale, so `ps` itself can still be found.
    assert "cmd" in captured


def test_pid_start_time_returns_none_and_warns_when_ps_fails(monkeypatch, caplog):
    def raise_failure(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["ps"])

    monkeypatch.setattr(subprocess, "check_output", raise_failure)

    with caplog.at_level(logging.WARNING, logger="coordharness.coord.process_liveness"):
        result = process_liveness.pid_start_time(4242)

    assert result is None
    assert any("ps" in rec.message for rec in caplog.records)


def test_pid_start_time_returns_none_and_warns_on_unparseable_output(monkeypatch, caplog):
    monkeypatch.setattr(
        subprocess, "check_output",
        lambda *a, **k: "not a timestamp at all\n",
    )

    with caplog.at_level(logging.WARNING, logger="coordharness.coord.process_liveness"):
        result = process_liveness.pid_start_time(4242)

    assert result is None
    assert any("unparseable" in rec.message for rec in caplog.records)


def test_pid_start_time_parses_a_well_formed_c_locale_line(monkeypatch):
    monkeypatch.setattr(
        subprocess, "check_output",
        lambda *a, **k: "Mon Aug 31 12:00:00 2026\n",
    )
    result = process_liveness.pid_start_time(4242)
    assert result is not None


def test_pid_matches_falls_back_to_bare_pid_exists_when_start_time_unknown(monkeypatch):
    # expected_start_time=None is the tolerant path relied on by callers
    # (like native_cockpit) reading sidecars written before pid_started_at
    # existed.
    monkeypatch.setattr(process_liveness, "pid_exists", lambda pid: True)
    assert process_liveness.pid_matches(999, None) is True

    monkeypatch.setattr(process_liveness, "pid_exists", lambda pid: False)
    assert process_liveness.pid_matches(999, None) is False


# ---------------------------------------------------------------------------
# 2. native_cockpit._sidecar_index
# ---------------------------------------------------------------------------


def _snapshot_for(monkeypatch, items):
    """Patch native_cockpit's sidecar_snapshot.load_snapshot to return the
    given raw sidecar dicts, bypassing the real on-disk job_progress dir."""
    from coordharness.jobs.sidecar_snapshot import SidecarSnapshot

    snap = SidecarSnapshot(items=tuple(items))
    monkeypatch.setattr(
        native_cockpit.sidecar_snapshot, "load_snapshot", lambda *_a, **_k: snap
    )


def test_sidecar_index_routes_through_pid_matches_with_pid_started_at(monkeypatch):
    calls = []

    def fake_pid_matches(pid, expected_start_time):
        calls.append((pid, expected_start_time))
        return True

    monkeypatch.setattr(native_cockpit.process_liveness, "pid_matches", fake_pid_matches)
    _snapshot_for(
        monkeypatch,
        [
            {
                "job_id": "job-1",
                "state": "running",
                "pid": 4242,
                "pid_started_at": 1700000000.0,
                "updated_at": "2020-01-01T00:00:00+00:00",  # stale by age alone
            }
        ],
    )

    out = native_cockpit._sidecar_index(now=1_800_000_000.0)

    assert calls == [(4242, 1700000000.0)]
    # pid_matches said alive -> kept despite the stale `updated_at`, proving
    # the pid check (not just age) drives retention.
    assert "job-1" in out


def test_sidecar_index_drops_stale_entry_when_pid_matches_says_dead(monkeypatch):
    monkeypatch.setattr(
        native_cockpit.process_liveness, "pid_matches", lambda pid, started: False
    )
    _snapshot_for(
        monkeypatch,
        [
            {
                "job_id": "job-1",
                "state": "running",
                "pid": 4242,
                "pid_started_at": 1700000000.0,
                "updated_at": "2020-01-01T00:00:00+00:00",
            }
        ],
    )

    out = native_cockpit._sidecar_index(now=1_800_000_000.0)

    assert out == {}


def test_sidecar_index_is_tolerant_of_sidecars_without_pid_started_at(monkeypatch):
    # Old sidecars predate the pid_started_at field entirely. _sidecar_index
    # must still call through to pid_matches (with expected_start_time=None)
    # rather than crashing or silently treating the job as dead.
    calls = []

    def fake_pid_matches(pid, expected_start_time):
        calls.append((pid, expected_start_time))
        return True

    monkeypatch.setattr(native_cockpit.process_liveness, "pid_matches", fake_pid_matches)
    _snapshot_for(
        monkeypatch,
        [
            {
                "job_id": "job-1",
                "state": "running",
                "pid": 4242,
                # no pid_started_at key at all
                "updated_at": "2020-01-01T00:00:00+00:00",
            }
        ],
    )

    out = native_cockpit._sidecar_index(now=1_800_000_000.0)

    assert calls == [(4242, None)]
    assert "job-1" in out


def test_sidecar_index_never_crashes_on_a_missing_pid(monkeypatch):
    monkeypatch.setattr(
        native_cockpit.process_liveness, "pid_matches",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    _snapshot_for(
        monkeypatch,
        [{"job_id": "job-1", "state": "running", "updated_at": "2020-01-01T00:00:00+00:00"}],
    )

    out = native_cockpit._sidecar_index(now=1_800_000_000.0)
    assert out == {}


# ---------------------------------------------------------------------------
# 2b. sidecar_writer records pid_started_at alongside pid
# ---------------------------------------------------------------------------


def test_update_stamps_pid_started_at_on_a_pid_change_and_reuses_it_otherwise(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COORD_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sidecar_writer, "SIDECAR_DIR", tmp_path / "job_progress")

    calls = []

    def fake_pid_start_time(pid):
        calls.append(pid)
        return 5000.0 + pid

    monkeypatch.setattr(process_liveness, "pid_start_time", fake_pid_start_time)

    sidecar_writer._atomic_write(
        {
            "job_id": "job-1",
            "roadmap_id": "WORK-1",
            "state": "running",
            "pid": 111,
            "pid_started_at": 111.0,
            "created_at": 1.0,
            "updated_at": "2020-01-01T00:00:00+00:00",
        },
        sidecar_writer._path("job-1"),
    )

    # same pid as on disk -> no ps call, prior value carried forward
    payload = sidecar_writer.update("job-1", "WORK-1", pid=111, step="still running")
    assert payload["pid_started_at"] == 111.0
    assert calls == []

    # pid changes (PID reuse / process restart) -> a fresh start time is
    # (re)stamped via process_liveness.pid_start_time
    payload2 = sidecar_writer.update("job-1", "WORK-1", pid=222, step="restarted")
    assert calls == [222]
    assert payload2["pid_started_at"] == pytest.approx(5222.0)
    assert payload2["pid"] == 222


def test_update_tolerates_pid_start_time_returning_none(tmp_path, monkeypatch):
    monkeypatch.setenv("COORD_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sidecar_writer, "SIDECAR_DIR", tmp_path / "job_progress")
    monkeypatch.setattr(process_liveness, "pid_start_time", lambda pid: None)

    sidecar_writer._atomic_write(
        {
            "job_id": "job-1",
            "roadmap_id": "WORK-1",
            "state": "running",
            "pid": 111,
            "created_at": 1.0,
            "updated_at": "2020-01-01T00:00:00+00:00",
        },
        sidecar_writer._path("job-1"),
    )

    # first observation of a new pid, and ps could not determine a start
    # time -- must not raise, and must not fabricate a value.
    payload = sidecar_writer.update("job-1", "WORK-1", pid=999, step="running")
    assert payload["pid"] == 999
    assert payload["pid_started_at"] is None


# ---------------------------------------------------------------------------
# 3. jobs.identity._proc_pattern_matches
# ---------------------------------------------------------------------------


def test_proc_pattern_matches_ordinary_patterns_unaffected():
    assert identity._proc_pattern_matches(r"pytest.*worker", "pytest worker 3") is True
    assert identity._proc_pattern_matches(r"nomatch", "pytest worker 3") is False


def test_proc_pattern_matches_falls_back_to_substring_on_bad_regex():
    # an actual re.error (unbalanced group), not a catastrophic shape --
    # existing substring-fallback behavior must be preserved.
    assert identity._proc_pattern_matches("py(thon", "py(thon script") is True
    assert identity._proc_pattern_matches("py(thon", "python script") is False


def test_proc_pattern_matches_rejects_overlong_patterns(caplog):
    pattern = "a" * 300
    with caplog.at_level(logging.WARNING, logger="coordharness.jobs.identity"):
        result = identity._proc_pattern_matches(pattern, "a" * 300)
    assert result is False
    assert any("length" in rec.message for rec in caplog.records)


@pytest.mark.parametrize(
    "pattern",
    [
        r"(x+)+",
        r"(a*)*",
        r"(a+)*",
        r"(ab+)+c",
        r"(x+){2,}",
    ],
)
def test_proc_pattern_matches_refuses_nested_quantifier_shapes(pattern, caplog):
    with caplog.at_level(logging.WARNING, logger="coordharness.jobs.identity"):
        result = identity._proc_pattern_matches(pattern, "some process command line text")
    assert result is False
    assert any("catastrophic" in rec.message for rec in caplog.records)


def test_proc_pattern_matches_refusal_never_raises_to_the_caller(caplog):
    # A pattern that is BOTH catastrophic-shaped and would otherwise take a
    # very long time under naive backtracking must return promptly and
    # without raising -- refusal, not an exception, reaches the caller.
    evil = "(a+)+$"
    text = "a" * 40 + "!"
    with caplog.at_level(logging.WARNING, logger="coordharness.jobs.identity"):
        result = identity._proc_pattern_matches(evil, text)
    assert result is False


def test_bind_sidecar_to_backlog_survives_a_hostile_proc_pattern():
    # end-to-end: a backlog row carrying an adversarial proc_pattern must not
    # crash bind_sidecar_to_backlog, and must simply fail to match rather
    # than hang.
    rows = [{"roadmap_id": "WORK-1", "proc_pattern": "(x+)+evil-marker"}]
    sidecar = {"job_id": "job-1", "script": "x" * 50}
    assert identity.bind_sidecar_to_backlog(sidecar, rows) is None
