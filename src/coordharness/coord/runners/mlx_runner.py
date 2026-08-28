from __future__ import annotations

import importlib.util
import json
import logging
import platform
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from coordharness.coord import coord_db
from coordharness.coord.config import connect
from coordharness.coord.create_schema import apply_schema
from coordharness.coord.ingest import resolve_identity

GenerateFn = Callable[[str], tuple[str, int]]
_logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    text: str
    n_tokens: int
    run_id: str
    session_id: str
    escalated: bool = False
    escalation_event_id: int | None = None


def _make_real_generate_fn(model_name: str, *, max_tokens: int | None = None) -> GenerateFn:
    machine = platform.machine().lower()
    if sys.platform != "darwin" or machine not in {"arm64", "aarch64"}:
        raise RuntimeError("MLX execution requires Apple silicon on macOS")
    if importlib.util.find_spec("mlx_lm") is None:
        raise RuntimeError("optional dependency mlx-lm is not installed; install coordharness[mlx]")
    import mlx_lm

    model, tokenizer = mlx_lm.load(model_name)

    def _generate(prompt: str) -> tuple[str, int]:
        kwargs = {"verbose": False}
        if max_tokens is not None:
            kwargs["max_tokens"] = int(max_tokens)
        text: str = mlx_lm.generate(model, tokenizer, prompt, **kwargs)
        n_tokens: int = len(tokenizer.encode(text))
        return text, n_tokens

    return _generate


class MLXRunner:
    def __init__(
        self,
        model_name: str,
        work_id: str,
        *,
        generate_fn: GenerateFn | None = None,
        confidence_threshold: float = 0.5,
        db_path: str | Path | None = None,
        actor: str | None = None,
        session_id: str | None = None,
        allow_complete_claim: bool = False,
        env: dict | None = None,
    ) -> None:
        self.model_name = model_name
        self.work_id = work_id
        self.confidence_threshold = confidence_threshold
        self._generate_fn: GenerateFn | None = generate_fn
        self._db_path = Path(db_path) if db_path is not None else None
        identity = resolve_identity(env)
        self.actor = actor or identity["actor"]
        self.session_id = session_id or identity["session_id"] or f"mlx-runner-{uuid.uuid4().hex[:8]}"
        self.allow_complete_claim = allow_complete_claim
        self.run_id: str | None = None

    def _open_conn(self):
        if self._db_path is not None:
            apply_schema(self._db_path)
            return connect(self._db_path)
        apply_schema()
        return connect()

    def _get_generate_fn(self, *, max_output_tokens: int | None = None) -> GenerateFn:
        if self._generate_fn is not None:
            return self._generate_fn
        return _make_real_generate_fn(self.model_name, max_tokens=max_output_tokens)

    def run(
        self,
        prompt: str,
        *,
        step: str | None = None,
        confidence_score: float | None = None,
        fallback_model: str | None = None,
        max_output_tokens: int | None = None,
    ) -> RunResult:
        # Import and load the backend before opening coord.db. Missing backend
        # dependencies therefore cannot leave lifecycle rows behind.
        generate = self._get_generate_fn(max_output_tokens=max_output_tokens)
        conn = self._open_conn()
        try:
            return self._run_with_conn(
                conn,
                prompt,
                step=step,
                confidence_score=confidence_score,
                fallback_actor=fallback_model,
                generate=generate,
            )
        finally:
            conn.close()

    def _run_with_conn(
        self,
        conn,
        prompt: str,
        *,
        step: str | None,
        confidence_score: float | None,
        fallback_actor: str | None,
        generate: GenerateFn,
    ) -> RunResult:
        work = conn.execute(
            "SELECT intent_state FROM work_items WHERE work_id=?", (self.work_id,)
        ).fetchone()
        if work is None:
            raise ValueError(
                f"unknown work_id {self.work_id!r}; local model runs never create lifecycle work"
            )
        state = str(work["intent_state"] or "").strip().lower()
        if state in coord_db.TERMINAL_WORK_STATES:
            raise ValueError(
                f"work_id {self.work_id!r} is terminal ({state}); refusing local model run"
            )

        session_preexisted = conn.execute(
            "SELECT 1 FROM agent_sessions WHERE session_id=?", (self.session_id,)
        ).fetchone() is not None
        session_registered = False
        run_id: str | None = None
        claim_id: str | None = None
        succeeded = False
        try:
            coord_db.register_session(conn, self.session_id, self.actor, runner_type="local_mlx")
            session_registered = True
            run_id = coord_db.appear_run(
                conn,
                work_id=self.work_id,
                session_id=self.session_id,
                runner_kind="local_mlx",
                model=self.model_name,
                resource_class="local_mlx",
            )
            self.run_id = run_id
            claim_id = coord_db.claim_work(conn, self.session_id, self.work_id, step=step)
            coord_db.heartbeat_claim(conn, claim_id, step=step or f"mlx:{self.model_name}")
            text, n_tokens = generate(prompt)

            coord_db.post_event(
                conn,
                kind="token_usage",
                actor=self.actor,
                session_id=self.session_id,
                work_id=self.work_id,
                run_id=run_id,
                payload_json=json.dumps(
                    {
                        "tokens": n_tokens,
                        "runner": "local_mlx",
                        "runner_source": "mlx_lm",
                        "model": self.model_name,
                    }
                ),
            )

            escalated = False
            escalation_event_id: int | None = None
            if confidence_score is not None and confidence_score < self.confidence_threshold:
                payload = {
                    "confidence_score": confidence_score,
                    "threshold": self.confidence_threshold,
                    "source_model": self.model_name,
                    "fallback_actor": fallback_actor,
                    "runner": "local_mlx",
                }
                if fallback_actor:
                    escalation_event_id = coord_db.post_event(
                        conn,
                        kind="handoff",
                        actor=self.actor,
                        session_id=self.session_id,
                        work_id=self.work_id,
                        run_id=run_id,
                        title=f"Local model low-confidence escalation → {fallback_actor}",
                        body=(
                            f"confidence_score={confidence_score:.3f} < "
                            f"threshold={self.confidence_threshold:.3f}; "
                            f"escalating to configured actor {fallback_actor}"
                        ),
                        payload_json=json.dumps(payload),
                        to_selector=f"actor:{fallback_actor}",
                    )
                    escalated = True
                else:
                    escalation_event_id = coord_db.post_event(
                        conn,
                        kind="model_low_confidence",
                        actor=self.actor,
                        session_id=self.session_id,
                        work_id=self.work_id,
                        run_id=run_id,
                        title="Local model output below confidence threshold",
                        payload_json=json.dumps(payload),
                    )

            succeeded = True
            return RunResult(
                text=text,
                n_tokens=n_tokens,
                run_id=run_id,
                session_id=self.session_id,
                escalated=escalated,
                escalation_event_id=escalation_event_id,
            )
        finally:
            cleanup_errors: list[Exception] = []
            if run_id is not None:
                try:
                    coord_db.finalize_run(conn, run_id, state="done" if succeeded else "failed")
                except Exception as exc:  # noqa: BLE001
                    cleanup_errors.append(exc)
            if claim_id is not None:
                try:
                    if succeeded and self.allow_complete_claim:
                        try:
                            coord_db.complete_claim(conn, claim_id)
                        except ValueError:
                            coord_db.release_claim(
                                conn,
                                claim_id,
                                status="released",
                                reason="run completed without artifact proof",
                            )
                    else:
                        coord_db.release_claim(
                            conn,
                            claim_id,
                            status="released",
                            reason=(
                                "local_mlx advisory run completed; completion disabled"
                                if succeeded
                                else "local_mlx run failed; claim released by finally"
                            ),
                        )
                except Exception as exc:  # noqa: BLE001
                    cleanup_errors.append(exc)
            if session_registered and not session_preexisted:
                try:
                    coord_db.end_session(conn, self.session_id)
                except Exception as exc:  # noqa: BLE001
                    cleanup_errors.append(exc)
            if cleanup_errors:
                if sys.exc_info()[0] is None:
                    raise RuntimeError(
                        "local model lifecycle cleanup failed: "
                        + "; ".join(f"{type(exc).__name__}: {exc}" for exc in cleanup_errors)
                    )
                for exc in cleanup_errors:
                    _logger.error("local model cleanup failed while handling another error: %s", exc)
