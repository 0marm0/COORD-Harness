"""The `coord` command.

This wraps the ported CLI with the two things a freshly installed package needs
and the original did not have to worry about, because it always ran inside a
repository that had already been set up by hand:

  * the database is created and migrated on first use, so `coord board` on a new
    machine prints an empty board instead of `no such table: v_work_owner`. On
    first use, not on every invocation: `coord --help` reaches no handler, so it
    creates nothing and works in a directory this process cannot write to;
  * migrations are applied, not just the base schema. Bootstrapping the base
    schema alone leaves the optional authority and provenance tables missing,
    and nothing says so until a query fails.

Both are idempotent, so this runs on every invocation and costs a stat call once
the database exists.

It is also where the CLI's error boundary lives, because this is the one place
every ``coord`` invocation passes through. A refusal -- "that work is assigned to
another lane", "that artifact is not committed" -- is the harness doing its job,
and it was reaching the terminal as an eleven-line traceback with the sentence
that mattered on the last line. Delivering it as one line does not soften the
refusal; it stops the refusal from reading like a crash.
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


def _leading_command(argv: Sequence[str]) -> str | None:
    """The subcommand token, stepping over the global ``--db`` option.

    ``None`` means this invocation names no subcommand and so reaches no
    handler: a bare ``coord``, or one carrying only a global flag such as
    ``-h``/``--help``. argparse answers those itself, or rejects them, without
    any handler ever asking for a database.
    """
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--db":
            index += 2
            continue
        if token.startswith("--db="):
            index += 1
            continue
        if token.startswith("-"):
            return None
        return token
    return None


# Verbs that must never bootstrap. `doctor` is declared read-only -- it reports
# on the state tree it finds and creating one would make its own finding false.
# `onboard` owns the setup path and decides for itself what to write.
_COMMANDS_WITHOUT_BOOTSTRAP = frozenset({"doctor", "onboard"})


def _needs_database(argv: Sequence[str], db_path: Path) -> bool:
    """Whether this invocation should create and migrate the database first.

    The default is yes: a verb that reaches a handler needs somewhere to write.
    The exemptions are the invocations that reach no handler at all, and the
    verbs that have declared they do their own thing:

      * no subcommand -- ``coord``, ``coord --help``, ``coord -h``. Creating a
        state directory in order to print usage makes the first command anyone
        types fail in a read-only or not-yours working directory, and leaves a
        ``.coordharness/`` behind in a writable one.
      * ``doctor`` and ``onboard``, as above.
      * ``board``, but only once the database it reads is already current. On a
        new machine it still bootstraps, so an empty board prints as an empty
        board rather than ``no such table: v_work_owner``.
    """
    command = _leading_command(argv)
    if command is None:
        return False
    if command in _COMMANDS_WITHOUT_BOOTSTRAP:
        return False
    if command == "board":
        return not database_current(db_path)
    return True


_TRACEBACK_FLAG = "--traceback"


def _take_traceback_flag(argv: list[str]) -> bool:
    """Remove --traceback from argv, wherever the caller put it, and report it.

    Handled here rather than by argparse because argparse would only accept it
    before the subcommand, and by the time anyone wants this flag they are
    re-running a command they have already typed and appending it to the end.
    Only the bare token is taken: an argument that legitimately carries the
    string has to be written ``--body=--traceback`` to survive argparse at all,
    and that token is not this one.
    """
    if _TRACEBACK_FLAG not in argv:
        return False
    argv[:] = [token for token in argv if token != _TRACEBACK_FLAG]
    return True


def _refusal_types() -> tuple[type[BaseException], ...]:
    """Exception types this CLI declares as a refusal rather than a crash.

    ``ValueError`` is the base every coordination contract in this package
    raises through -- CreationLintError, ParkContractError,
    ResumeTriggerContractError, ReviewTierPolicyError, PanelContractError and
    the WorkRelationInvariantError family all derive from it -- so naming it
    covers the declared set without importing a dozen modules. ``LedgerError``
    is the one declared domain error reachable from a verb here (``coord
    route``) that descends from RuntimeError instead, so it has to be named.

    Everything else keeps its traceback on purpose. A bare RuntimeError in this
    package marks a broken invariant ("new claim is missing its exact custody
    fence"), not a user mistake, and a locked or corrupt database is not a thing
    the caller can be told to fix in one line.

    An except clause's expression is evaluated only while an exception is being
    matched, so the import below costs nothing on the success path.
    """
    from .usage.ledger import LedgerError

    return (ValueError, LedgerError)


def ensure_database(db_path: Path) -> None:
    bootstrap_database(db_path)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # Taken before anything else reads argv: the flag is not a command, and the
    # argv scanners below would otherwise have to know about it.
    show_traceback = _take_traceback_flag(args)
    db_path = _db_path_from(args)
    try:
        if _needs_database(args, db_path):
            ensure_database(db_path)
    except Exception as exc:  # noqa: BLE001
        if show_traceback:
            raise
        print(f"coord: could not prepare the database: {exc}", file=sys.stderr)
        return 2
    if show_traceback:
        return coord_cli.main(args) or 0
    try:
        return coord_cli.main(args) or 0
    except _refusal_types() as exc:
        # One line on stderr, so stdout stays parseable for the verbs that emit
        # JSON, and exit 1 -- the code an uncaught exception already produced,
        # so a script branching on a refusal keeps reading it the same way.
        # SystemExit and KeyboardInterrupt are not caught here: argparse's own
        # usage errors keep their exit 2, and an interrupt stays an interrupt.
        print(f"coord: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
