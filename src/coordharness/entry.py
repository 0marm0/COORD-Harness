"""The `coord` command.

This wraps the ported CLI with the two things a freshly installed package needs
and the original did not have to worry about, because it always ran inside a
repository that had already been set up by hand:

  * the database is created and migrated on first use, so `coord board` on a new
    machine prints an empty board instead of `no such table: v_work_owner`;
  * migrations are applied, not just the base schema. Bootstrapping the base
    schema alone leaves the optional authority and provenance tables missing,
    and nothing says so until a query fails.

Both are idempotent, so this runs on every invocation and costs a stat call once
the database exists.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

from . import config
from .bootstrap import bootstrap_database, database_current
from .coord import cli as coord_cli


def _db_path_from(argv: Sequence[str]) -> Path:
    """Honour an explicit --db before falling back to the configured location."""
    for index, token in enumerate(argv):
        if token == "--db" and index + 1 < len(argv):
            return Path(argv[index + 1]).expanduser()
        if token.startswith("--db="):
            return Path(token.split("=", 1)[1]).expanduser()
    return config.coord_db_path()


def _is_board_command(argv: Sequence[str]) -> bool:
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--db":
            index += 2
            continue
        if token.startswith("--db="):
            index += 1
            continue
        return token == "board"
    return False


def _is_doctor_command(argv: Sequence[str]) -> bool:
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--db":
            index += 2
            continue
        if token.startswith("--db="):
            index += 1
            continue
        return token == "doctor"
    return False


def _is_onboard_command(argv: Sequence[str]) -> bool:
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--db":
            index += 2
            continue
        if token.startswith("--db="):
            index += 1
            continue
        return token == "onboard"
    return False


def ensure_database(db_path: Path) -> None:
    bootstrap_database(db_path)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    db_path = _db_path_from(args)
    read_existing_board = _is_board_command(args) and database_current(db_path)
    read_only_doctor = _is_doctor_command(args)
    setup_or_verify_onboarding = _is_onboard_command(args)
    try:
        if not read_existing_board and not read_only_doctor and not setup_or_verify_onboarding:
            ensure_database(db_path)
    except Exception as exc:  # noqa: BLE001
        print(f"coord: could not prepare the database: {exc}", file=sys.stderr)
        return 2
    return coord_cli.main(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
