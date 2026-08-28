from __future__ import annotations

from pathlib import Path

import pytest

from coordharness.coord.runners import mlx_runner


def test_direct_runner_hardware_preflight_precedes_database_mutation(
    monkeypatch, tmp_path: Path
) -> None:
    db = tmp_path / "coord.db"
    monkeypatch.setattr(mlx_runner.sys, "platform", "linux")
    monkeypatch.setattr(mlx_runner.platform, "machine", lambda: "x86_64")
    runner = mlx_runner.MLXRunner(
        "operator/local-model",
        "MODEL-1",
        db_path=db,
        actor="local",
        session_id="local:preflight",
    )
    with pytest.raises(RuntimeError, match="Apple silicon"):
        runner.run("hello")
    assert not db.exists()
