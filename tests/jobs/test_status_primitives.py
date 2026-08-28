from __future__ import annotations

import pytest

from coordharness.jobs import status


class TestParseUpdatedAt:
    def test_epoch_float(self):
        assert status.parse_updated_at(1_700_000_000.0) == 1_700_000_000.0

    def test_epoch_int(self):
        assert status.parse_updated_at(1_700_000_000) == 1_700_000_000.0

    def test_numeric_string(self):
        assert status.parse_updated_at("1700000000") == 1_700_000_000.0

    def test_iso_with_z(self):
        from datetime import datetime, timezone
        expected = datetime(2026, 6, 20, tzinfo=timezone.utc).timestamp()
        assert status.parse_updated_at("2026-06-20T00:00:00Z") == pytest.approx(expected)

    def test_iso_with_offset(self):
        a = status.parse_updated_at("2026-06-20T00:00:00+00:00")
        b = status.parse_updated_at("2026-06-20T00:00:00Z")
        assert a == b

    def test_iso_naive_assumed_utc(self):
        naive = status.parse_updated_at("2026-06-20T00:00:00")
        z = status.parse_updated_at("2026-06-20T00:00:00Z")
        assert naive == z

    def test_empty_and_garbage_return_none(self):
        assert status.parse_updated_at("") is None
        assert status.parse_updated_at(None) is None
        assert status.parse_updated_at("not-a-date") is None


class TestStale:
    def test_fresh_is_not_stale(self):
        now = 1_000_000.0
        assert status.is_stale(now - 60.0, now, window_s=900.0) is False

    def test_old_is_stale(self):
        now = 1_000_000.0
        assert status.is_stale(now - 1000.0, now, window_s=900.0) is True

    def test_missing_updated_at_is_stale(self):
        assert status.is_stale(None, 1_000_000.0) is True
        assert status.is_stale("garbage", 1_000_000.0) is True

    def test_iso_value_works_in_stale(self):
        ts = status.parse_updated_at("2026-06-20T00:00:00Z")
        assert status.is_stale("2026-06-20T00:00:00Z", ts + 10.0, window_s=900.0) is False

    def test_age_seconds_never_negative(self):
        now = 1_000_000.0
        assert status.age_seconds(now + 50.0, now) == 0.0


class TestCanonicalId:
    def test_roadmap_id_wins(self):
        row = {"roadmap_id": "JOB-0620", "id": "abc", "name": "Something"}
        assert status.canonical_id(row) == "JOB-0620"

    def test_falls_to_id_then_job_id(self):
        assert status.canonical_id({"id": "abc", "name": "n"}) == "abc"
        assert status.canonical_id({"job_id": "j1", "name": "n"}) == "j1"

    def test_name_slug_last_resort(self):
        assert status.canonical_id({"name": "CAP Re-tag v2"}) == "cap-re-tag-v2"

    def test_empty_when_nothing(self):
        assert status.canonical_id({}) == ""

    def test_blank_fields_skipped(self):
        row = {"roadmap_id": "  ", "id": "", "job_id": "real"}
        assert status.canonical_id(row) == "real"

    def test_dedup_key_is_casefolded_canonical(self):
        a = {"id": "THETA-ISSUE-1"}
        b = {"roadmap_id": "theta-issue-1"}
        assert status.dedup_key(a) == status.dedup_key(b)


class TestFormatEta:
    @pytest.mark.parametrize("secs,expected", [
        (None, "—"),
        (-5, "—"),
        (0, "0s"),
        (45, "45s"),
        (65, "1m 5s"),
        (3700, "1h 1m"),
        (90000, "1d 1h"),
    ])
    def test_format(self, secs, expected):
        assert status.format_eta(secs) == expected


class TestPctDisplay:
    def test_explicit_pct_preferred(self):
        assert status.pct_display(pct=36.1) == "36.1%"

    def test_from_done_total(self):
        assert status.pct_display(done=50, total=200) == "25.0%"

    def test_clamped(self):
        assert status.pct_display(pct=150) == "100.0%"
        assert status.pct_display(pct=-10) == "0.0%"

    def test_unknown_is_dash(self):
        assert status.pct_display() == "—"
        assert status.pct_display(done=5, total=0) == "—"

    def test_nonfinite_pct_is_dash_not_false_100(self):
        for bad in (float("nan"), float("inf"), float("-inf"), "nan", "inf"):
            assert status.pct_display(pct=bad) == "—"
        assert status.pct_display(done=float("nan"), total=10) == "—"


class TestDoneSignalNonString:
    def test_list_done_signal_any_member_counts(self, tmp_path):
        (tmp_path / "b.json").write_text('{"x": 1}', encoding="utf-8")
        assert status.done_signal_exists(["data_local/missing.json", "b.json"], tmp_path)
        assert not status.done_signal_exists(["data_local/missing.json"], tmp_path)

    def test_dict_done_signal_does_not_crash(self, tmp_path):
        assert status.done_signal_exists({"path": "x"}, tmp_path) is False
        ev = status.derive_status(
            {"id": "X", "status": "done", "done_signal": {"weird": "shape"}}, tmp_path)
        assert ev.status in {"done", "queued", "planned"}


class TestDirectoryDoneAndUnverified:
    def test_dir_with_part_file_counts_as_done(self, tmp_path):
        d = tmp_path / "out.parquet"
        d.mkdir()
        (d / "part-0.parquet").write_bytes(b"x" * 5000)
        assert status.done_signal_exists("out.parquet", tmp_path)

    def test_empty_dir_not_done(self, tmp_path):
        (tmp_path / "out.parquet").mkdir()
        assert not status.done_signal_exists("out.parquet", tmp_path)

    def test_dir_with_only_tiny_files_not_done(self, tmp_path):
        d = tmp_path / "out.parquet"
        d.mkdir()
        (d / "part-0.parquet").write_bytes(b"x" * 10)
        assert not status.done_signal_exists("out.parquet", tmp_path)

    def test_success_marker_counts_as_done(self, tmp_path):
        d = tmp_path / "out"
        d.mkdir()
        (d / "_SUCCESS").write_text("", encoding="utf-8")
        assert status.done_signal_exists("out", tmp_path)

    def test_done_claimed_but_artifact_missing_is_done_unverified_not_queued(self, tmp_path):
        ev = status.derive_status(
            {"id": "X", "status": "done", "done_signal": "data_local/missing.parquet"}, tmp_path)
        assert ev.status == "done"
        assert ev.unverified is True

    def test_done_with_verified_artifact_is_clean_done(self, tmp_path):
        (tmp_path / "data_local").mkdir()
        (tmp_path / "data_local" / "real.parquet").write_bytes(b"x" * 5000)
        ev = status.derive_status(
            {"id": "X", "status": "done", "done_signal": "data_local/real.parquet"}, tmp_path)
        assert ev.status == "done"
        assert ev.unverified is False


class TestReadModeSot:
    def _setup(self, tmp_path, *, gov=None, gov_ts=None, txt=None, modejson=None, now=1000.0):
        import json
        if gov is not None:
            payload = {"mode": gov}
            if gov_ts is not None:
                payload["timestamp"] = gov_ts
            (tmp_path / "governor_status.json").write_text(json.dumps(payload))
        if txt is not None:
            (tmp_path / "resource_mode.txt").write_text(txt)
        if modejson is not None:
            (tmp_path / "mode.json").write_text(json.dumps({"mode": modejson}))
        return status.read_mode_sot(now=now, data_dir=tmp_path)

    def test_fresh_governor_wins(self, tmp_path):
        r = self._setup(tmp_path, gov="medium", gov_ts=990.0, txt="medium", now=1000.0)
        assert r == {"mode": "medium", "pending": False, "source": "governor_status"}

    def test_stale_governor_falls_to_txt(self, tmp_path):
        r = self._setup(tmp_path, gov="full", gov_ts=500.0, txt="medium", now=1000.0)
        assert r["mode"] == "medium" and r["source"] == "resource_mode_txt"

    def test_disagreement_sets_pending(self, tmp_path):
        r = self._setup(tmp_path, gov="medium", gov_ts=990.0, txt="full", now=1000.0)
        assert r["mode"] == "medium" and r["pending"] is True

    def test_never_reads_mode_json(self, tmp_path):
        r = self._setup(tmp_path, gov="medium", gov_ts=990.0, modejson="full", now=1000.0)
        assert r["mode"] == "medium" and "mode_json" not in r["source"]

    def test_offline_when_nothing(self, tmp_path):
        r = status.read_mode_sot(now=1000.0, data_dir=tmp_path)
        assert r == {"mode": "unknown", "pending": False, "source": "offline"}


class TestArtifactSettled:
    def test_settled_when_old(self, tmp_path):
        f = tmp_path / "out.json"
        f.write_text('{"x": 1}')
        mt = f.stat().st_mtime
        assert status.artifact_settled(str(f), tmp_path, settle_s=10, now=mt + 100) is True

    def test_not_settled_when_fresh(self, tmp_path):
        f = tmp_path / "out.json"
        f.write_text('{"x": 1}')
        mt = f.stat().st_mtime
        assert status.artifact_settled(str(f), tmp_path, settle_s=10, now=mt + 1) is False

    def test_missing_artifact_not_settled(self, tmp_path):
        assert status.artifact_settled(str(tmp_path / "nope.json"), tmp_path) is False

    def test_below_min_bytes_parquet_not_settled(self, tmp_path):
        f = tmp_path / "out.parquet"
        f.write_text("tiny")
        mt = f.stat().st_mtime
        assert status.artifact_settled(str(f), tmp_path, settle_s=10, now=mt + 100) is False

    def test_any_artifact_settled_over_keys(self, tmp_path):
        f = tmp_path / "out.json"
        f.write_text('{"x": 1}')
        mt = f.stat().st_mtime
        assert status.any_artifact_settled({"done_signal": str(f)}, tmp_path,
                                           settle_s=10, now=mt + 100) is True


class TestHardening:
    def test_format_eta_nonfinite_and_negative(self):
        for bad in (float("nan"), float("inf"), float("-inf"), -5):
            assert status.format_eta(bad) == "—"

    def test_format_eta_rejects_epoch_magnitude(self):
        assert status.format_eta(1_750_000_000) == "—"
        assert status.format_eta(1e12) == "—"

    def test_parse_updated_at_rejects_nonfinite_and_bool(self):
        for bad in (float("inf"), float("-inf"), float("nan"), "inf", "nan", "1e400", True, False):
            assert status.parse_updated_at(bad) is None

    def test_is_stale_true_for_inf(self):
        assert status.is_stale(float("inf"), 1000.0) is True

    def test_read_mode_sot_future_governor_falls_to_txt(self, tmp_path):
        import json
        (tmp_path / "governor_status.json").write_text(
            json.dumps({"mode": "full", "timestamp": 999_999_999_999}))
        (tmp_path / "resource_mode.txt").write_text("medium")
        r = status.read_mode_sot(now=1000.0, data_dir=tmp_path)
        assert r["mode"] == "medium" and r["source"] == "resource_mode_txt"

    def test_done_signal_rejects_zero_byte_json_and_dir(self, tmp_path):
        empty = tmp_path / "out.json"
        empty.write_text("")
        assert status.done_signal_exists(str(empty), tmp_path) is False
        d = tmp_path / "outdir"
        d.mkdir()
        assert status.done_signal_exists(str(d), tmp_path) is False
