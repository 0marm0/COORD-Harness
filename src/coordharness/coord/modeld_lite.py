from __future__ import annotations

import importlib.util
import json
import os
import platform
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from coordharness import config as harness_config
from coordharness.coord import coord_db
from coordharness.coord.config import connect, harness_autonomy_config
from coordharness.coord.create_schema import apply_schema
from coordharness.coord.policy.pipeline import apply_output_budget, run_boundary_policy
from coordharness.coord.runners.mlx_runner import GenerateFn, MLXRunner
from coordharness.coord.run_events import record_measured_token_usage
from coordharness.jobs.resource_lock import ResourceLock, ResourceLockError


SUPPORTED_MODES = {"triage", "classify", "draft", "audit", "embed", "summarize"}
GENERATION_MODES = SUPPORTED_MODES - {"embed"}
FIRST_ACTIVATION_MAX_OUTPUT_TOKENS = 2048


def _truthy(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class LocalModelSpec:
    model_id: str
    runner: str
    modes: list[str]
    requires_gpu: bool
    context_tokens: int
    notes: str = ""

    def __post_init__(self) -> None:
        # MLX generation always uses the Metal accelerator; a catalog may not
        # downgrade that backend into an unlocked CPU entry.
        if self.runner == "mlx_lm" and not self.requires_gpu:
            object.__setattr__(self, "requires_gpu", True)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LocalModelSpec":
        model_id = str(raw.get("model_id") or "").strip()
        runner = str(raw.get("runner") or "").strip()
        modes = [str(mode).strip().lower() for mode in raw.get("modes") or []]
        if not model_id:
            raise ValueError("catalog model_id must not be empty")
        if not runner:
            raise ValueError(f"catalog runner must not be empty for {model_id!r}")
        if not modes or any(mode not in SUPPORTED_MODES for mode in modes):
            raise ValueError(f"catalog modes are invalid for {model_id!r}: {modes!r}")
        context_tokens = int(raw.get("context_tokens") or 0)
        if context_tokens <= 0:
            raise ValueError(f"catalog context_tokens must be positive for {model_id!r}")
        return cls(
            model_id=model_id,
            runner=runner,
            modes=modes,
            requires_gpu=bool(raw.get("requires_gpu")),
            context_tokens=context_tokens,
            notes=str(raw.get("notes") or ""),
        )


@dataclass(frozen=True)
class ModeldLiteRequest:
    work_id: str
    mode: str
    prompt: str
    max_output_tokens: int = 512
    prefer_gpu: bool = True


@dataclass(frozen=True)
class ModeldLiteMeasurement:
    output_text: str
    prompt_tokens_est: int
    output_tokens_est: int
    elapsed_ms: int
    runner: str
    runner_source: str
    model_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_catalog(*, env: Mapping[str, str] | None = None) -> list[LocalModelSpec]:
    """Build a neutral one-model catalog only when the operator configures it."""
    source = os.environ if env is None else env
    model_id = str(source.get("COORD_LOCAL_MODEL_ID") or "").strip()
    if not model_id:
        return []
    modes = [
        mode.strip().lower()
        for mode in str(
            source.get("COORD_LOCAL_MODEL_MODES")
            or "triage,classify,draft,audit,summarize"
        ).split(",")
        if mode.strip()
    ]
    return [
        LocalModelSpec.from_dict(
            {
                "model_id": model_id,
                "runner": source.get("COORD_LOCAL_MODEL_RUNNER") or "mlx_lm",
                "modes": modes,
                "requires_gpu": _truthy(source.get("COORD_LOCAL_MODEL_REQUIRES_GPU"), default=True),
                "context_tokens": int(source.get("COORD_LOCAL_MODEL_CONTEXT_TOKENS") or 32768),
                "notes": "operator-configured local model",
            }
        )
    ]


def load_catalog(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> list[LocalModelSpec]:
    source = os.environ if env is None else env
    configured = path or source.get("COORD_MODEL_CATALOG")
    if configured is None or not str(configured).strip():
        return default_catalog(env=source)
    catalog_path = Path(str(configured)).expanduser()
    try:
        parsed = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read model catalog {catalog_path}: {exc}") from exc
    rows = parsed.get("models") if isinstance(parsed, dict) else parsed
    if not isinstance(rows, list):
        raise ValueError("model catalog must be a list or an object containing a models list")
    return [LocalModelSpec.from_dict(row) for row in rows]


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def select_model(
    mode: str,
    *,
    prefer_gpu: bool = True,
    catalog: list[LocalModelSpec] | None = None,
) -> LocalModelSpec | None:
    mode = mode.strip().lower()
    candidates = [spec for spec in (catalog if catalog is not None else default_catalog()) if mode in spec.modes]
    if prefer_gpu:
        return candidates[0] if candidates else None
    return next((spec for spec in candidates if not spec.requires_gpu), None)


def backend_preflight(
    model: LocalModelSpec,
    *,
    injected_backend: bool = False,
) -> dict[str, Any]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    result: dict[str, Any] = {
        "model_id": model.model_id,
        "runner": model.runner,
        "requires_gpu": model.requires_gpu,
        "platform": {"system": system, "machine": machine},
        "dependency": None,
        "dependency_available": False,
        "hardware_supported": False,
        "execution_supported": False,
        "ok": False,
        "reason": None,
    }
    if model.runner == "callable":
        result["execution_supported"] = bool(injected_backend)
        result["dependency"] = "caller-injected"
        result["dependency_available"] = bool(injected_backend)
        result["hardware_supported"] = bool(injected_backend)
        result["ok"] = bool(injected_backend)
        result["reason"] = (
            "injected callable backend"
            if injected_backend
            else "callable runner is available only through an injected backend"
        )
        return result
    if model.runner == "mlx_embeddings":
        result["dependency"] = "mlx-embeddings"
        result["reason"] = "embedding execution is not implemented by modeld-lite"
        return result
    if model.runner != "mlx_lm":
        result["reason"] = f"unsupported model runner {model.runner!r}"
        return result
    result["execution_supported"] = True
    result["dependency"] = "mlx-lm"
    if injected_backend:
        result.update(
            {
                "dependency_available": True,
                "hardware_supported": True,
                "ok": True,
                "reason": "injected backend; hardware and dependency checks delegated to caller",
            }
        )
        return result
    result["dependency_available"] = importlib.util.find_spec("mlx_lm") is not None
    result["hardware_supported"] = sys.platform == "darwin" and machine in {"arm64", "aarch64"}
    if not result["hardware_supported"]:
        result["reason"] = "MLX execution requires Apple silicon on macOS"
    elif not result["dependency_available"]:
        result["reason"] = "optional dependency mlx-lm is not installed; install coordharness[mlx]"
    else:
        result["ok"] = True
        result["reason"] = "ready"
    return result


def catalog_status(catalog: list[LocalModelSpec] | None = None) -> dict[str, Any]:
    models = catalog if catalog is not None else default_catalog()
    checks = [backend_preflight(model) for model in models]
    return {
        "schema_version": 1,
        "configured": bool(models),
        "count": len(models),
        "ready_count": sum(1 for check in checks if check["ok"]),
        "hardware_available": any(check["hardware_supported"] for check in checks),
        "models": [
            {"spec": model.to_dict(), "preflight": check}
            for model, check in zip(models, checks, strict=True)
        ],
        "reason": None if models else "no local models configured",
    }


def _modeld_advisory_enabled(autonomy: dict[str, Any]) -> bool:
    tiers = autonomy.get("tiers") if isinstance(autonomy.get("tiers"), dict) else {}
    return bool(autonomy.get("enabled") and tiers.get("modeld_advisory"))


def _validate_request(
    request: ModeldLiteRequest,
    *,
    catalog: list[LocalModelSpec],
    injected_backend: bool,
) -> tuple[str, str, int, LocalModelSpec, dict[str, Any]]:
    mode = request.mode.strip().lower()
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"unsupported modeld-lite mode {request.mode!r}")
    prompt = str(request.prompt or "")
    if not prompt.strip():
        raise ValueError("prompt is required")
    max_output_tokens = int(request.max_output_tokens)
    if max_output_tokens <= 0:
        raise ValueError("max_output_tokens must be > 0")
    if max_output_tokens > FIRST_ACTIVATION_MAX_OUTPUT_TOKENS:
        raise ValueError(
            f"max_output_tokens must be <= {FIRST_ACTIVATION_MAX_OUTPUT_TOKENS} for first activation"
        )
    model = select_model(mode, prefer_gpu=request.prefer_gpu, catalog=catalog)
    if model is None:
        preference = "CPU-capable " if not request.prefer_gpu else ""
        raise ValueError(f"no configured {preference}model supports mode {mode!r}")
    preflight = backend_preflight(model, injected_backend=injected_backend)
    if not preflight["execution_supported"]:
        raise ValueError(str(preflight["reason"]))
    if not preflight["ok"]:
        raise RuntimeError(f"local model preflight failed: {preflight['reason']}")
    prompt_tokens = estimate_tokens(prompt)
    if prompt_tokens + max_output_tokens > model.context_tokens:
        raise ValueError("request exceeds selected model context_tokens")
    return mode, prompt, max_output_tokens, model, preflight


def plan_request(
    request: ModeldLiteRequest,
    *,
    autonomy: dict[str, Any] | None = None,
    catalog: list[LocalModelSpec] | None = None,
) -> dict[str, Any]:
    mode = request.mode.strip().lower()
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"unsupported modeld-lite mode {request.mode!r}")
    autonomy = autonomy or harness_autonomy_config(default_enabled=False)
    advisory_enabled = _modeld_advisory_enabled(autonomy)
    models = catalog if catalog is not None else default_catalog()
    model = select_model(mode, prefer_gpu=request.prefer_gpu, catalog=models)
    prompt_tokens = estimate_tokens(request.prompt)
    output_tokens = max(0, int(request.max_output_tokens))
    token_estimate = prompt_tokens + output_tokens
    command_hint = None
    if advisory_enabled and model:
        command_hint = (
            f"coord-models run --work-id {request.work_id} --mode {mode} "
            "--prompt-file <path>"
        )
    policy = run_boundary_policy(
        boundary="modeld",
        action="plan",
        work_id=request.work_id,
        payload={"task_mode": mode, "output_bytes": output_tokens * 4, "run_event_category": "token"},
    )
    disabled_reason = None
    if not advisory_enabled:
        disabled_reason = str(autonomy.get("reason") or "modeld_advisory_disabled")
    elif model is None:
        disabled_reason = "no configured model matches the request"
    return {
        "schema_version": 1,
        "mode": "modeld_lite_plan",
        "work_id": request.work_id,
        "task_mode": mode,
        "actions_enabled": False,
        "advisory_enabled": advisory_enabled,
        "daemon": False,
        "allow_complete": False,
        "auto_execute": False,
        "requires_explicit_operator_run": bool(advisory_enabled and model),
        "prompt_tokens_est": prompt_tokens,
        "max_output_tokens": output_tokens,
        "total_tokens_est": token_estimate,
        "recommended_model": model.to_dict() if model else None,
        "requires_gpu_lock": bool(model.requires_gpu) if model else False,
        "launch_command_hint": command_hint,
        "disabled_reason": disabled_reason,
        "autonomy": autonomy,
        "safety": {
            "no_lifecycle_status_mutation": True,
            "no_claim_completion": True,
            "no_daemon": True,
            "no_implicit_gpu_lock": True,
            "measurements_are_advisory": True,
        },
        "policy": policy,
    }


def _connect_for_modeld_policy(db_path: str | Path | None):
    if db_path is not None:
        path = Path(db_path)
        apply_schema(path)
        return connect(path)
    apply_schema()
    return connect()


def _run_modeld_boundary_policy(
    *,
    request: ModeldLiteRequest,
    mode: str,
    model: LocalModelSpec,
    prompt_tokens: int,
    max_output_tokens: int,
    db_path: str | Path | None,
    actor: str | None,
    session_id: str | None,
) -> dict[str, Any]:
    run_id = f"modeld-pre:{session_id or actor or 'local'}:{request.work_id}"
    conn = _connect_for_modeld_policy(db_path)
    try:
        return run_boundary_policy(
            boundary="modeld",
            action="run",
            work_id=request.work_id,
            run_id=run_id,
            session_id=session_id,
            actor=actor,
            payload={
                "task_mode": mode,
                "model_id": model.model_id,
                "runner": model.runner,
                "requires_gpu": bool(model.requires_gpu),
                "prompt_tokens_est": int(prompt_tokens),
                "max_output_tokens": int(max_output_tokens),
                "output_bytes": int(max_output_tokens) * 4,
                "run_event_category": "token",
            },
            conn=conn,
        )
    finally:
        conn.close()


def execute_mlx_request(
    request: ModeldLiteRequest,
    *,
    db_path: str | Path | None = None,
    actor: str | None = None,
    session_id: str | None = None,
    env: dict[str, str] | None = None,
    generate_fn: GenerateFn | None = None,
    confidence_score: float | None = None,
    fallback_model: str | None = None,
    record_measurement: bool = True,
    record_token_shadow: bool = False,
    autonomy: dict[str, Any] | None = None,
    enforce_autonomy: bool = True,
    catalog: list[LocalModelSpec] | None = None,
    resource_lock: ResourceLock | None = None,
) -> dict[str, Any]:
    models = catalog if catalog is not None else default_catalog(env=env)
    mode, prompt, max_output_tokens, model, preflight = _validate_request(
        request,
        catalog=models,
        injected_backend=generate_fn is not None,
    )
    autonomy = autonomy or harness_autonomy_config(default_enabled=False)
    if enforce_autonomy and not _modeld_advisory_enabled(autonomy):
        raise RuntimeError(f"modeld-lite advisory disabled by autonomy kill-switch: {autonomy.get('reason')}")

    lock_receipt: dict[str, Any] | None = None
    if model.requires_gpu:
        if resource_lock is None:
            raise ResourceLockError("GPU model execution requires a process-held ResourceLock")
        lock_receipt = resource_lock.verify()

    # Everything above this line is read-only with respect to coord.db.
    prompt_tokens = estimate_tokens(prompt)
    pre_policy = _run_modeld_boundary_policy(
        request=request,
        mode=mode,
        model=model,
        prompt_tokens=prompt_tokens,
        max_output_tokens=max_output_tokens,
        db_path=db_path,
        actor=actor,
        session_id=session_id,
    )
    if pre_policy.get("blocked"):
        raise RuntimeError(f"policy blocked modeld run before execution: {pre_policy.get('block_reason')}")

    started = time.perf_counter()
    runner = MLXRunner(
        model_name=model.model_id,
        work_id=request.work_id,
        generate_fn=generate_fn,
        db_path=db_path,
        actor=actor,
        session_id=session_id,
        allow_complete_claim=False,
        env=env if env is not None else os.environ,
    )
    result = runner.run(
        prompt,
        step=f"modeld-lite:{mode}",
        confidence_score=confidence_score,
        fallback_model=fallback_model,
        max_output_tokens=max_output_tokens,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    measurement = ModeldLiteMeasurement(
        output_text=result.text,
        prompt_tokens_est=prompt_tokens,
        output_tokens_est=int(result.n_tokens),
        elapsed_ms=elapsed_ms,
        runner="modeld_lite",
        runner_source="mlx_runner",
        model_id=model.model_id,
    )
    event_id = None
    token_shadow: dict[str, Any] | None = None
    conn = connect(Path(db_path) if db_path else None)
    try:
        policy = run_boundary_policy(
            boundary="modeld",
            action="run",
            work_id=request.work_id,
            run_id=result.run_id,
            session_id=result.session_id,
            actor=actor,
            payload={
                "task_mode": mode,
                "output_text": result.text,
                "output_bytes": len(str(result.text or "").encode("utf-8")),
                "run_event_category": "token",
            },
            conn=conn,
        )
        if policy.get("blocked"):
            raise RuntimeError(f"policy blocked modeld run: {policy.get('block_reason')}")
        if record_measurement:
            event_id = record_measurement_event(
                conn,
                request=request,
                measurement=measurement,
                actor=actor,
                session_id=result.session_id,
            )
        if record_token_shadow:
            token_shadow = record_measured_token_shadow(
                conn,
                request=request,
                measurement=measurement,
                measured_tokens=result.n_tokens,
                logical_usage_id=f"{result.run_id}:output",
                enabled=True,
                actor=actor,
                session_id=result.session_id,
                run_id=result.run_id,
            )
    finally:
        conn.close()
    bounded_output = apply_output_budget(
        result.text,
        artifact_dir=harness_config.state_dir() / "model_output",
        artifact_prefix=f"modeld-{result.run_id}",
    )
    return {
        "schema_version": 1,
        "mode": "modeld_lite_mlx_run",
        "work_id": request.work_id,
        "task_mode": mode,
        "model_execution_enabled": True,
        "daemon": False,
        "allow_complete": False,
        "lifecycle_mutation_enabled": True,
        "lifecycle_effect": "temporary advisory claim released; run finalized; owned session ended",
        "requires_gpu_lock": bool(model.requires_gpu),
        "resource_lock": lock_receipt,
        "preflight": preflight,
        "run_id": result.run_id,
        "session_id": result.session_id,
        "model": model.to_dict(),
        "result": {
            "output_text": bounded_output["text"],
            "output_budget": {
                "truncated": bounded_output["truncated"],
                "bytes": bounded_output["bytes"],
                "inline_limit": bounded_output["inline_limit"],
                "artifact_ref": bounded_output["artifact_ref"],
                "enabled": bounded_output.get("enabled"),
            },
            "tokens": int(result.n_tokens),
            "escalated": bool(result.escalated),
            "escalation_event_id": result.escalation_event_id,
        },
        "measurement": measurement.to_dict(),
        "measurement_event_id": event_id,
        "token_shadow": token_shadow,
        "pre_policy": pre_policy,
        "policy": policy,
    }


def run_stub_measurement(
    request: ModeldLiteRequest,
    *,
    generate_fn: Callable[[str], str],
    model_id: str = "stub",
) -> ModeldLiteMeasurement:
    started = time.perf_counter()
    output = generate_fn(request.prompt)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return ModeldLiteMeasurement(
        output_text=output,
        prompt_tokens_est=estimate_tokens(request.prompt),
        output_tokens_est=estimate_tokens(output),
        elapsed_ms=elapsed_ms,
        runner="modeld_lite",
        runner_source="stub_measurement",
        model_id=model_id,
    )


def record_measurement_event(
    conn,
    *,
    request: ModeldLiteRequest,
    measurement: ModeldLiteMeasurement,
    actor: str | None = None,
    session_id: str | None = None,
) -> int | None:
    payload = {
        "schema_version": 1,
        "request": {
            "work_id": request.work_id,
            "mode": request.mode,
            "prompt_tokens_est": estimate_tokens(request.prompt),
            "max_output_tokens": request.max_output_tokens,
        },
        "measurement": measurement.to_dict(),
        "safety": {"allow_complete": False, "lifecycle_mutation": False, "advisory_only": True},
    }
    return coord_db.post_event(
        conn,
        kind="modeld_lite_measurement",
        actor=actor,
        session_id=session_id,
        work_id=request.work_id,
        severity="info",
        title=f"modeld-lite measurement: {request.mode}",
        payload_json=json.dumps(payload, sort_keys=True),
    )


def record_measured_token_shadow(
    conn,
    *,
    request: ModeldLiteRequest,
    measurement: ModeldLiteMeasurement,
    measured_tokens: int | None,
    logical_usage_id: str,
    enabled: bool = False,
    actor: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    if measured_tokens is None:
        raise ValueError("explicit measured_tokens is required for modeld-lite token shadow")
    measured_tokens = int(measured_tokens)
    if measured_tokens < 0:
        raise ValueError(f"measured_tokens must be >= 0, got {measured_tokens}")
    logical_usage_id = str(logical_usage_id or "").strip()
    if not logical_usage_id:
        raise ValueError("logical_usage_id is required")
    event_run_id = str(run_id or f"modeld_lite:{request.work_id}:{logical_usage_id}").strip()
    metadata = {
        "actor": actor,
        "modeld_lite_mode": request.mode,
        "output_tokens_est": measurement.output_tokens_est,
        "prompt_tokens_est": measurement.prompt_tokens_est,
        "runner": measurement.runner,
        "runner_source": measurement.runner_source,
    }
    return record_measured_token_usage(
        conn,
        work_id=request.work_id,
        run_id=event_run_id,
        thread_id=None,
        session_id=session_id,
        caller="modeld_lite",
        model=measurement.model_id,
        tokens=measured_tokens,
        source="modeld_lite",
        logical_usage_id=logical_usage_id,
        metadata=metadata,
        source_reliability="exact",
        enabled=enabled,
    )
