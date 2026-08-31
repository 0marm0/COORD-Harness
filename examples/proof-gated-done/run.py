#!/usr/bin/env python3
"""GUARD: completion is refused until the declared artifact is in git's index.

A claim's `done_signal` is declared when the work item is created -- a
repo-relative path the row will not be considered complete without. This
recipe claims a row, then tries `coord done` three times:

  1. before the artifact file exists at all            -> refused: "does not exist"
  2. after the file exists but before `git add`          -> refused: "not carried by git's index"
  3. after `git add` (staged, not even committed)         -> succeeds

Step 2 is the one worth reading closely: an artifact that exists on disk is
NOT proof. A completion claim your reviewer cannot `git diff` against is not
a completion claim COORD will accept.

This script creates its own throwaway git repo and its own throwaway
coord.db under a fresh `tempfile.TemporaryDirectory()` and deletes both when
it exits. It never opens, reads, or writes any database that existed before
it ran.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

COORD_BIN = Path(sys.executable).parent / "coord"
WORK_ID = "DEMO-CLA-INVOICE-EXPORT"
ARTIFACT_REL = "docs/reports/invoice-export.md"


def _git(project: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=project,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "example",
            "GIT_AUTHOR_EMAIL": "example@invalid",
            "GIT_COMMITTER_NAME": "example",
            "GIT_COMMITTER_EMAIL": "example@invalid",
        },
    )


def coord(project: Path, db: Path, *args: str, session: str = "claude:invoices",
          expect_ok: bool = True) -> tuple[int, dict | None, str]:
    """Run one `coord` command. Returns (returncode, parsed-stdout-or-None, stderr)."""
    env = {**os.environ, "COORD_PROJECT_ROOT": str(project), "COORD_HOME": str(project / ".coordharness")}
    for leaked in (
        "CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID",
        "CODEX_WORKTREE_ID", "CODEX_CONVERSATION_ID", "STARSHIP_SESSION_KEY",
        "COORD_ACTOR", "COORD_SESSION_ID", "COORD_PARENT_SESSION_ID",
    ):
        env.pop(leaked, None)
    env["COORD_ACTOR"] = session.split(":", 1)[0]
    env["COORD_SESSION_ID"] = session

    result = subprocess.run(
        [str(COORD_BIN), "--db", str(db), *args],
        cwd=project, capture_output=True, text=True, env=env,
    )
    print(f"    $ coord {' '.join(args)}")
    if result.returncode == 0:
        print(f"    -> {result.stdout.strip()}")
        if not expect_ok:
            raise SystemExit("expected this step to be REFUSED, but it succeeded")
        return result.returncode, json.loads(result.stdout), result.stderr
    print(f"    -> refused: {result.stderr.strip()}")
    if expect_ok:
        raise SystemExit(f"unexpected refusal from a step meant to succeed: {result.stderr.strip()}")
    return result.returncode, None, result.stderr


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="coord-example-proof-gated-") as tmp:
        project = Path(tmp) / "project"
        project.mkdir()
        _git(project, "init", "-q")
        (project / ".gitignore").write_text(".coordharness/\n", encoding="utf-8")
        _git(project, "add", "-A")
        _git(project, "commit", "-qm", "initial")
        db = project / ".coordharness" / "coord.db"
        artifact = project / ARTIFACT_REL

        print(f"[1/5] Create {WORK_ID}, declaring done_signal={ARTIFACT_REL}")
        coord(
            project, db, "create", WORK_ID,
            "--title", "Export invoices as CSV",
            "--module", "billing", "--tier", "T1",
            "--done-signal", ARTIFACT_REL,
            "--acceptance", "exported CSV matches the invoice table",
            "--note", "proof-gated-done example",
        )

        print(f"\n[2/5] Claim {WORK_ID}")
        coord(project, db, "claim", WORK_ID, "--step", "writing the export")

        print(f"\n[3/5] Attempt `coord done` before the artifact file exists at all")
        rc, _, stderr = coord(
            project, db, "done", WORK_ID, "--artifact", ARTIFACT_REL, expect_ok=False,
        )
        assert "does not exist" in stderr, stderr
        assert "git add" not in stderr, "a missing file should not suggest `git add`"

        print(f"\n[4/5] Write the artifact to disk, but do NOT `git add` it, then attempt `coord done` again")
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            "# Invoice export\n\nCSV generated at docs/reports/invoice-export.csv.\n",
            encoding="utf-8",
        )
        rc, _, stderr = coord(
            project, db, "done", WORK_ID, "--artifact", ARTIFACT_REL, expect_ok=False,
        )
        assert "not carried by git's index" in stderr, stderr
        assert f"git add {ARTIFACT_REL}" in stderr, stderr
        print("\n    The file is sitting right there on disk. COORD refuses it anyway --")
        print("    a done_signal must be reachable by `git diff`, not just `ls`.")

        print(f"\n[5/5] `git add` the artifact (staged, not committed) and retry `coord done`")
        _git(project, "add", ARTIFACT_REL)
        _, payload, _ = coord(project, db, "done", WORK_ID, "--artifact", ARTIFACT_REL)
        assert payload["ok"] is True
        assert payload["canonical_event_id"] > 0

        print("\nOK: completion was refused twice for the exact reasons stated, and")
        print("    accepted only once the declared proof was staged in git's index.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
