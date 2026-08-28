from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import coordharness.usage.fingerprint as fingerprint_module
from coordharness.usage.fingerprint import (
    SourceRoot,
    fingerprint_source,
    fingerprint_sources,
)


def _config(path: Path, *, strong: bool = True, account: str = "acct-a") -> SourceRoot:
    return SourceRoot(
        path=path,
        provider="claude",
        account_key=account,
        timezone="America/New_York",
        include_sha256=strong,
    )


def test_discovers_nested_account_caches_and_classifies_sources(tmp_path: Path) -> None:
    root = tmp_path / "usage"
    (root / "Caches" / "acct-a").mkdir(parents=True)
    (root / "Caches" / "acct-b" / "nested").mkdir(parents=True)
    (root / "sessions" / "2026").mkdir(parents=True)
    (root / "ledger").mkdir(parents=True)
    (root / "Caches" / "acct-a" / "claude-v6.json").write_text("{}")
    (root / "Caches" / "acct-b" / "nested" / "codex-v3.json").write_text("{}")
    (root / "sessions" / "2026" / "session.jsonl").write_text('{"type":"event"}\n')
    (root / "ledger" / "usage-v2.sqlite").write_bytes(b"sqlite")
    (root / "README.txt").write_text("source")

    result = fingerprint_source(_config(root))

    assert result.status == "available"
    assert result.fingerprint_strength == "strong_sha256"
    by_path = {entry.relative_path: entry for entry in result.entries}
    assert list(by_path) == sorted(by_path)
    assert by_path["Caches/acct-a/claude-v6.json"].source_type == "cache"
    assert by_path["Caches/acct-b/nested/codex-v3.json"].source_type == "cache"
    assert by_path["sessions/2026/session.jsonl"].source_type == "session"
    assert by_path["ledger/usage-v2.sqlite"].source_type == "ledger"
    assert by_path["README.txt"].source_type == "source"
    assert all(entry.entry_type == "file" for entry in result.entries)


def test_missing_root_is_in_band_and_json_ready(tmp_path: Path) -> None:
    config = _config(tmp_path / "not-created")

    first = fingerprint_source(config)
    second = fingerprint_source(config)

    assert first.status == "missing"
    assert first.entries == ()
    assert [issue.code for issue in first.issues] == ["root_missing"]
    assert first.root_digest == second.root_digest
    assert json.loads(json.dumps(first.to_dict(), sort_keys=True))["status"] == "missing"


def test_unavailable_root_is_in_band_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "unreadable"
    root.mkdir()

    def deny_scan(_path: object) -> object:
        raise PermissionError("denied for test")

    monkeypatch.setattr(fingerprint_module.os, "scandir", deny_scan)
    result = fingerprint_source(_config(root))

    assert result.status == "unavailable"
    assert result.entries == ()
    assert [issue.code for issue in result.issues] == ["directory_unavailable"]


def test_order_and_digests_are_deterministic_and_input_order_independent(tmp_path: Path) -> None:
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    (alpha / "z").mkdir(parents=True)
    beta.mkdir()
    (alpha / "z" / "last.json").write_text("last")
    (alpha / "a.json").write_text("first")
    (beta / "b.json").write_text("beta")
    alpha_config = _config(alpha, account="acct-a")
    beta_config = _config(beta, account="acct-b")

    first = fingerprint_source(alpha_config)
    second = fingerprint_source(alpha_config)
    forward = fingerprint_sources([alpha_config, beta_config])
    reverse = fingerprint_sources([beta_config, alpha_config])

    assert first.to_dict() == second.to_dict()
    assert [entry.relative_path for entry in first.entries] == ["a.json", "z/last.json"]
    assert forward == reverse
    assert forward["aggregate_digest"] == reverse["aggregate_digest"]


def test_strong_hash_detects_same_size_content_change_with_restored_mtime(tmp_path: Path) -> None:
    root = tmp_path / "usage"
    root.mkdir()
    source = root / "claude-v6.json"
    source.write_bytes(b"ABCD")
    original = source.stat()
    first = fingerprint_source(_config(root, strong=True))

    source.write_bytes(b"WXYZ")
    os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
    second = fingerprint_source(_config(root, strong=True))

    assert first.entries[0].size == second.entries[0].size == 4
    assert first.entries[0].mtime_ns == second.entries[0].mtime_ns
    assert first.entries[0].inode == second.entries[0].inode
    assert first.entries[0].sha256 != second.entries[0].sha256
    assert first.root_digest != second.root_digest


def test_rejects_symlink_escape_without_reading_target(tmp_path: Path) -> None:
    root = tmp_path / "usage"
    root.mkdir()
    outside = tmp_path / "outside-secret.json"
    outside.write_text("do not include")
    link = root / "escaped.json"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not supported on this test filesystem")

    result = fingerprint_source(_config(root))

    assert result.status == "partial"
    assert result.entries == ()
    assert [issue.code for issue in result.issues] == ["rejected_symlink_escape"]
    assert result.issues[0].relative_path == "escaped.json"
    assert "do not include" not in json.dumps(result.to_dict())


def test_fingerprinting_does_not_change_source_content_or_mtime(tmp_path: Path) -> None:
    root = tmp_path / "usage"
    nested = root / "account" / "sessions"
    nested.mkdir(parents=True)
    files = [
        root / "account" / "claude-v6.json",
        nested / "session.jsonl",
    ]
    files[0].write_text('{"total":123}')
    files[1].write_text('{"tokens":456}\n')
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
        for path in files
    }

    result = fingerprint_source(_config(root, strong=True))

    after = {
        path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
        for path in files
    }
    assert result.status == "available"
    assert before == after
    assert all(entry.sha256 for entry in result.entries)


def test_source_root_requires_explicit_identity_fields(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="account_key"):
        SourceRoot(
            path=tmp_path,
            provider="claude",
            account_key="",
            timezone="America/New_York",
        )
    with pytest.raises(ValueError, match="unsafe"):
        SourceRoot(
            path=tmp_path,
            provider="claude",
            account_key="unknown",
            timezone="America/New_York",
        )
