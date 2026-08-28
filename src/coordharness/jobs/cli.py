"""Public CLI for bounded tracked jobs and read-only status snapshots."""

from __future__ import annotations

import argparse
import json
import os
import sys

from coordharness import config
from coordharness.jobs import launch, sidecar_snapshot


def _public(value):
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    if isinstance(value, str) and os.path.isabs(value):
        return config.public_path_ref(value)
    return value


def _status() -> int:
    snapshot = sidecar_snapshot.load_snapshot()
    print(
        json.dumps(
            {
                "schema_version": 1,
                "state_root": "state://job_progress",
                "count": len(snapshot.items),
                "jobs": _public(list(snapshot.items)),
                "skipped_count": len(snapshot.skipped),
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="coord-jobs")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="print the current read-only job snapshot as JSON")
    launch_parser = sub.add_parser(
        "launch",
        help="run one command under tracked sidecar and RSS-cap supervision",
        add_help=False,
    )
    launch_parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(raw[:1] if raw[:1] == ["status"] else raw[:1])
    if args.command == "status":
        return _status()
    return launch.main(raw[1:])


if __name__ == "__main__":
    raise SystemExit(main())
