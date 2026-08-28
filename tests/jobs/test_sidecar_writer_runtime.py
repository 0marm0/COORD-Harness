from __future__ import annotations

import hashlib
from pathlib import Path

from coordharness.jobs import sidecar_writer, status


def test_tracked_control_paths_use_configured_control_directory(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    control = project / "runtime" / "job-control"
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(project))
    monkeypatch.setenv("COORD_JOB_CONTROL_DIR", str(control))

    paths = sidecar_writer.tracked_control_paths("job-1")
    job_key = hashlib.sha256(b"job-1").hexdigest()

    assert paths == {
        "canonical_control_record_path": str(
            (control / "records" / f"{job_key}.json").resolve()
        ),
        "canonical_control_sentinel_path": str(
            (control / "managed" / f"{job_key}.managed").resolve()
        ),
        "canonical_control_lock_path": str(
            (control / "locks" / f"{job_key}.lock").resolve()
        ),
    }


def test_finalize_uses_configured_project_root_for_relative_done_signal(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(project))
    observed: dict[str, object] = {}

    def artifact_settled(
        signal: str | None,
        root: str | Path,
        *,
        settle_s: float,
        now: float | None,
    ) -> bool:
        observed.update(signal=signal, root=Path(root), settle_s=settle_s, now=now)
        return True

    def update(job_id: str, roadmap_id: str, **kwargs) -> dict:
        return {"job_id": job_id, "roadmap_id": roadmap_id, **kwargs}

    monkeypatch.setattr(status, "artifact_settled", artifact_settled)
    monkeypatch.setattr(sidecar_writer, "update", update)

    payload = sidecar_writer.finalize(
        "job-1",
        "WORK-1",
        state="done",
        done_signal="artifacts/result.json",
        settle_s=0,
    )

    assert payload["state"] == "done"
    assert payload["done"] is True
    assert observed == {
        "signal": "artifacts/result.json",
        "root": project.resolve(),
        "settle_s": 0,
        "now": None,
    }
