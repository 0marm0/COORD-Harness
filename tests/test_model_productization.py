from __future__ import annotations

import json
from pathlib import Path
import tomllib

import pytest

from coordharness.coord import coord_db
from coordharness.coord.config import connect
from coordharness.coord.create_schema import apply_schema
from coordharness.coord.model_cli import main as model_cli_main
from coordharness.coord.modeld_lite import (
    LocalModelSpec,
    ModeldLiteRequest,
    backend_preflight,
    default_catalog,
    execute_mlx_request,
    load_catalog,
    select_model,
)
from coordharness.coord.runners.mlx_runner import MLXRunner
from coordharness.jobs.resource_lock import ResourceLock, ResourceLockError

REPO = Path(__file__).resolve().parents[1]


def _model(*, modes: list[str] | None = None, requires_gpu: bool = False, runner: str = "callable"):
    return LocalModelSpec(
        model_id="operator/local-model",
        runner=runner,
        modes=modes or ["draft"],
        requires_gpu=requires_gpu,
        context_tokens=4096,
        notes="synthetic test model",
    )


def _seed_work(db: Path, work_id: str = "MODEL-1") -> None:
    apply_schema(db)
    conn = connect(db)
    try:
        coord_db.upsert_work(conn, work_id, title="Synthetic model work", assignee="shared")
    finally:
        conn.close()


def test_catalog_is_operator_configured_and_has_no_vendor_default(tmp_path: Path) -> None:
    assert default_catalog(env={}) == []
    configured = default_catalog(env={"COORD_LOCAL_MODEL_ID": "operator/model"})
    assert configured[0].model_id == "operator/model"
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"models": [configured[0].to_dict()]}), encoding="utf-8")
    assert load_catalog(path) == configured


def test_prefer_gpu_false_never_falls_back_to_gpu_only_model() -> None:
    gpu = _model(requires_gpu=True)
    assert select_model("draft", prefer_gpu=False, catalog=[gpu]) is None
    cpu = _model(requires_gpu=False)
    assert select_model("draft", prefer_gpu=False, catalog=[gpu, cpu]) == cpu


def test_unsupported_embed_fails_before_database_mutation(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    request = ModeldLiteRequest("MODEL-1", "embed", "hello")
    with pytest.raises(ValueError, match="embedding execution is not implemented"):
        execute_mlx_request(
            request,
            db_path=db,
            catalog=[_model(modes=["embed"], requires_gpu=True, runner="mlx_embeddings")],
            enforce_autonomy=False,
        )
    assert not db.exists()


def test_self_asserted_environment_flag_is_not_lock_authority(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    request = ModeldLiteRequest("MODEL-1", "draft", "hello")
    with pytest.raises(ResourceLockError, match="process-held ResourceLock"):
        execute_mlx_request(
            request,
            db_path=db,
            env={"COORD_GPU_LOCK_HELD": "1", "COORD_ACTOR": "local"},
            generate_fn=lambda prompt: (prompt, 1),
            catalog=[_model(requires_gpu=True)],
            enforce_autonomy=False,
        )
    assert not db.exists()


def test_resource_lock_is_os_exclusive_and_verifiable(tmp_path: Path) -> None:
    path = tmp_path / "gpu.lock"
    first = ResourceLock("gpu", path=path).acquire()
    try:
        assert first.verify()["authority"] == "os_file_lock"
        with pytest.raises(ResourceLockError, match="already locked"):
            ResourceLock("gpu", path=path).acquire()
    finally:
        first.release()
    with ResourceLock("gpu", path=path) as reacquired:
        assert reacquired.verify()["verified"] is True


def test_runner_failure_finally_releases_claim_finalizes_run_and_ends_owned_session(
    tmp_path: Path,
) -> None:
    db = tmp_path / "coord.db"
    _seed_work(db)

    def fail(_prompt: str):
        raise RuntimeError("synthetic generation failure")

    with pytest.raises(RuntimeError, match="synthetic generation failure"):
        execute_mlx_request(
            ModeldLiteRequest("MODEL-1", "draft", "hello"),
            db_path=db,
            actor="local",
            session_id="local:model-failure",
            generate_fn=fail,
            catalog=[_model()],
            enforce_autonomy=False,
        )

    conn = connect(db)
    try:
        assert conn.execute("SELECT state FROM runs").fetchone()["state"] == "failed"
        assert conn.execute("SELECT status FROM claims").fetchone()["status"] == "released"
        assert conn.execute(
            "SELECT state FROM agent_sessions WHERE session_id='local:model-failure'"
        ).fetchone()["state"] == "ended"
    finally:
        conn.close()


def test_success_is_advisory_and_releases_authority(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    _seed_work(db)
    result = execute_mlx_request(
        ModeldLiteRequest("MODEL-1", "draft", "hello"),
        db_path=db,
        actor="local",
        session_id="local:model-success",
        generate_fn=lambda _prompt: ("bounded output", 3),
        catalog=[_model()],
        enforce_autonomy=False,
        record_measurement=False,
    )
    assert result["result"]["output_text"] == "bounded output"
    assert result["lifecycle_effect"].startswith("temporary advisory claim released")
    conn = connect(db)
    try:
        assert conn.execute("SELECT state FROM runs").fetchone()["state"] == "done"
        assert conn.execute("SELECT status FROM claims").fetchone()["status"] == "released"
        assert conn.execute("SELECT intent_state FROM work_items").fetchone()["intent_state"] == "queued"
    finally:
        conn.close()


def test_runner_does_not_synthesize_unknown_work(tmp_path: Path) -> None:
    db = tmp_path / "coord.db"
    runner = MLXRunner(
        "operator/local-model",
        "TYPO-WORK",
        db_path=db,
        actor="local",
        session_id="local:unknown-work",
        generate_fn=lambda _prompt: ("never", 1),
    )
    with pytest.raises(ValueError, match="never create lifecycle work"):
        runner.run("hello")
    conn = connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM work_items").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM agent_sessions").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    finally:
        conn.close()


def test_cli_list_and_truthful_unavailable_check(monkeypatch, capsys) -> None:
    monkeypatch.delenv("COORD_MODEL_CATALOG", raising=False)
    monkeypatch.delenv("COORD_LOCAL_MODEL_ID", raising=False)
    assert model_cli_main(["list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed == {
        "configured": False,
        "count": 0,
        "models": [],
        "reason": "no local models configured",
        "schema_version": 1,
    }
    assert model_cli_main(["check"]) == 1
    checked = json.loads(capsys.readouterr().out)
    assert checked["configured"] is False
    assert checked["hardware_available"] is False


def test_mlx_extra_is_platform_marked_and_console_script_is_public() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    requirement = pyproject["project"]["optional-dependencies"]["mlx"][0]
    assert "sys_platform == 'darwin'" in requirement
    assert "platform_machine == 'arm64'" in requirement
    assert pyproject["project"]["scripts"]["coord-models"] == "coordharness.coord.model_cli:main"


def test_owned_sources_have_no_private_launcher_or_vendor_defaults() -> None:
    roots = [
        REPO / "src/coordharness/coord/modeld_lite.py",
        REPO / "src/coordharness/coord/runners/mlx_runner.py",
        REPO / "src/coordharness/coord/model_cli.py",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in roots)
    for forbidden in ("gpu_job.sh", "data_local", "mlx-community", '"sonnet"'):
        assert forbidden not in text


def test_backend_status_is_truthful_on_non_mlx_machine(monkeypatch) -> None:
    from coordharness.coord import modeld_lite

    monkeypatch.setattr(modeld_lite.sys, "platform", "linux")
    monkeypatch.setattr(modeld_lite.platform, "machine", lambda: "x86_64")
    status = backend_preflight(_model(requires_gpu=True, runner="mlx_lm"))
    assert status["ok"] is False
    assert status["hardware_supported"] is False
    assert "Apple silicon" in status["reason"]
