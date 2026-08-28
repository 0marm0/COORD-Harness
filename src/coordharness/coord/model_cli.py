"""Public CLI for configured local-model catalog inspection and execution."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from coordharness.coord.modeld_lite import (
    ModeldLiteRequest,
    backend_preflight,
    catalog_status,
    execute_mlx_request,
    load_catalog,
    select_model,
)
from coordharness.jobs.resource_lock import ResourceLock


def _emit(payload: dict, *, stream=None) -> None:
    print(json.dumps(payload, sort_keys=True), file=stream or sys.stdout)


def _prompt(args: argparse.Namespace) -> str:
    if args.prompt is not None:
        return args.prompt
    if args.prompt_file is not None:
        return Path(args.prompt_file).read_text(encoding="utf-8")
    raise ValueError("run requires exactly one of --prompt or --prompt-file")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coord-models")
    parser.add_argument(
        "--catalog",
        default=None,
        help="JSON catalog path (or set COORD_MODEL_CATALOG)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list configured models without probing hardware")
    sub.add_parser("check", help="probe backend dependency and hardware readiness")

    run = sub.add_parser("run", help="run one bounded generation request")
    run.add_argument("--work-id", required=True)
    run.add_argument("--mode", required=True)
    prompt_group = run.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt")
    prompt_group.add_argument("--prompt-file")
    run.add_argument("--max-output-tokens", type=int, default=512)
    run.add_argument(
        "--prefer-cpu",
        action="store_true",
        help="select only a catalog entry that does not require the GPU lock",
    )
    run.add_argument("--fallback-actor", default=None)
    run.add_argument("--actor", default=None)
    run.add_argument("--session-id", default=None)
    run.add_argument("--db", default=None)

    args = parser.parse_args(argv)
    try:
        catalog = load_catalog(args.catalog)
        if args.command == "list":
            _emit(
                {
                    "schema_version": 1,
                    "configured": bool(catalog),
                    "count": len(catalog),
                    "models": [model.to_dict() for model in catalog],
                    "reason": None if catalog else "no local models configured",
                }
            )
            return 0
        if args.command == "check":
            status = catalog_status(catalog)
            _emit(status)
            return 0 if status["configured"] and status["ready_count"] else 1

        request = ModeldLiteRequest(
            work_id=args.work_id,
            mode=args.mode,
            prompt=_prompt(args),
            max_output_tokens=args.max_output_tokens,
            prefer_gpu=not args.prefer_cpu,
        )
        model = select_model(request.mode, prefer_gpu=request.prefer_gpu, catalog=catalog)
        if model is None:
            kind = "CPU-capable " if args.prefer_cpu else ""
            raise ValueError(f"no configured {kind}model supports mode {request.mode!r}")
        preflight = backend_preflight(model)
        if not preflight["ok"]:
            _emit(
                {"ok": False, "stage": "preflight", "preflight": preflight},
                stream=sys.stderr,
            )
            return 3

        if model.requires_gpu:
            with ResourceLock("local-model-gpu") as lock:
                result = execute_mlx_request(
                    request,
                    db_path=args.db,
                    actor=args.actor,
                    session_id=args.session_id,
                    fallback_model=args.fallback_actor,
                    enforce_autonomy=False,
                    catalog=catalog,
                    resource_lock=lock,
                )
        else:
            result = execute_mlx_request(
                request,
                db_path=args.db,
                actor=args.actor,
                session_id=args.session_id,
                fallback_model=args.fallback_actor,
                enforce_autonomy=False,
                catalog=catalog,
            )
        result["operator_initiated"] = True
        _emit(result)
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        _emit(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            stream=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
