"""Regression coverage for the policy pipeline's resource-modes path.

``_configured_modes()`` used to re-derive the location of ``resource_modes.json``
by hand as ``Path(HARNESS_ROOT) / "data_local" / "resource_modes.json"``, while
``coordharness.config.resource_modes_path()`` -- the resolver the rest of the
harness (the governor, the job launcher, ``coord/config.py``'s
``harness_autonomy_config``) actually reads and writes -- points at
``state_dir() / "resource_modes.json"``. Those are two different paths, so any
mode file the rest of the system wrote was invisible to the policy pipeline;
the bare ``except Exception`` swallowed the resulting ``FileNotFoundError`` and
silently fell back to an empty config.

The fix makes the pipeline call the shared resolver instead of re-deriving the
path. These tests pin that: one asserts the pipeline is structurally wired to
the shared resolver (so a future edit cannot quietly repoint it at a literal
path again), and one is a behavioral proof that a mode file written at the
resolver's path is now actually seen.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from coordharness import config as harness_config
from coordharness.coord.policy import pipeline


def test_configured_modes_calls_the_shared_resource_modes_path_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Path] = []
    original = harness_config.resource_modes_path

    def spy() -> Path:
        path = original()
        calls.append(path)
        return path

    monkeypatch.setattr(harness_config, "resource_modes_path", spy)

    pipeline._configured_modes()

    assert calls, (
        "_configured_modes() must resolve resource_modes.json via "
        "coordharness.config.resource_modes_path() rather than re-deriving the path"
    )


def test_configured_modes_source_no_longer_hardcodes_a_data_local_path() -> None:
    source = inspect.getsource(pipeline._configured_modes)
    assert "data_local" not in source
    assert "HARNESS_ROOT" not in source


def test_configured_modes_sees_a_mode_file_written_at_the_shared_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("COORD_HOME", raising=False)

    # This is the path every other reader/writer in the harness uses --
    # written directly through the shared resolver, not guessed at.
    modes_path = harness_config.resource_modes_path()
    modes_path.parent.mkdir(parents=True, exist_ok=True)
    modes_path.write_text(
        json.dumps({"harness_policy_modes": {"creation_lint": "enforce"}}),
        encoding="utf-8",
    )

    assert pipeline._configured_modes() == {"creation_lint": "enforce"}


def test_configured_modes_ignores_a_stale_data_local_file_at_the_old_wrong_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("COORD_HOME", raising=False)

    # A file at the old, wrong location must not leak through: proves the two
    # paths have genuinely diverged rather than the fix happening to coincide.
    old_wrong_path = tmp_path / "data_local" / "resource_modes.json"
    old_wrong_path.parent.mkdir(parents=True, exist_ok=True)
    old_wrong_path.write_text(
        json.dumps({"harness_policy_modes": {"creation_lint": "enforce"}}),
        encoding="utf-8",
    )

    assert pipeline._configured_modes() == {}
