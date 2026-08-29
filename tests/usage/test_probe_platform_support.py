"""A probe that cannot run must say so, not answer as if it had asked.

Both provider probes run the vendor CLI with ``cwd="/private/tmp"``. That
directory is a macOS spelling, and where it does not exist ``subprocess`` raises
``FileNotFoundError`` for the *working directory* -- which the broad handlers in
``local_service`` read as "the CLI did not answer" and report as ``unavailable``.
A signed-in, fully authenticated CLI then produced byte-identical output to a
machine with no CLI installed at all.

These tests force the missing-directory condition rather than waiting for a
non-Darwin host, so they assert the same thing on every platform.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from coordharness.usage import local_service


class _CompletedRun:
    """The shape ``subprocess.run`` returns for the one field the probe reads."""

    def __init__(self, stdout: bytes) -> None:
        self.stdout = stdout
        self.returncode = 0


@pytest.fixture
def missing_sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    absent = str(tmp_path / "no-such-directory")
    monkeypatch.setattr(local_service, "_PROBE_CWD", absent)
    return absent


@pytest.fixture
def cli_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a CLI in front of the probe, so the platform is what it trips on.

    Without this the probe short-circuits on ``claude_cli_unavailable`` wherever
    the vendor CLI is not installed, and the assertions below would pass for a
    reason that has nothing to do with the fix.
    """
    monkeypatch.setattr(local_service.shutil, "which", lambda *_a, **_k: "/usr/bin/true")


def test_claude_probe_names_the_platform_instead_of_reporting_unavailable(
    missing_sandbox: str,
    cli_on_path: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the probe launched the CLI from a directory that does not exist")

    monkeypatch.setattr(local_service.subprocess, "run", refuse)

    probe = local_service.probe_claude_account(tmp_path)

    assert probe.account == {"status": "unsupported", "plan": "unknown", "authenticated": None}
    assert probe.errors[0] == "claude_probe_platform_unsupported"


def test_codex_probe_names_the_platform_instead_of_reporting_unavailable(
    missing_sandbox: str,
    cli_on_path: None,
    tmp_path: Path,
) -> None:
    probe = local_service.probe_codex_account(tmp_path)

    assert probe.account == {"status": "unsupported", "plan": "unknown", "authenticated": None}
    assert probe.errors[0] == "codex_probe_platform_unsupported"


def test_the_jsonl_runner_refuses_with_the_reason_not_a_bare_missing_file(
    missing_sandbox: str,
) -> None:
    with pytest.raises(OSError) as raised:
        local_service._default_jsonl_runner(["/usr/bin/true"], [], 0.2)

    assert "macOS only" in str(raised.value)
    assert missing_sandbox in str(raised.value)


def test_an_authenticated_cli_still_reads_as_authenticated_where_the_sandbox_exists(
    monkeypatch: pytest.MonkeyPatch,
    cli_on_path: None,
    tmp_path: Path,
) -> None:
    """The guard must gate on the directory, not merely mention it.

    This is the differential half: the same probe, the same stub CLI, and the
    only thing that changed is whether the working directory exists.
    """
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setattr(local_service, "_PROBE_CWD", str(sandbox))
    seen: dict[str, Any] = {}

    def fake_run(*args: Any, **kwargs: Any) -> _CompletedRun:
        seen.update(kwargs)
        return _CompletedRun(json.dumps({"loggedIn": True, "subscriptionType": "max"}).encode())

    monkeypatch.setattr(local_service.subprocess, "run", fake_run)

    probe = local_service.probe_claude_account(tmp_path)

    assert probe.account == {"status": "active", "plan": "max", "authenticated": True}
    assert seen["cwd"] == str(sandbox)
