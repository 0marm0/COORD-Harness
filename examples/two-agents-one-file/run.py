#!/usr/bin/env python3
"""GUARD: two agents declare overlapping write sets on the same board.

Claude claims a job and declares it will write ``src/billing/`` (a whole
directory). Codex claims a *different* job and declares a narrower scope
inside that same directory, ``src/billing/retries.py``. Nobody edits a file
yet -- this is the moment before either agent has touched anything, which is
the only moment a warning is still cheap to act on.

Nothing here is "checked out" from a git repo you already have. This script
creates its own throwaway git repo and its own throwaway coord.db under a
fresh ``tempfile.TemporaryDirectory()`` and deletes both when it exits. It
never opens, reads, or writes any database that existed before it ran.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COORD_BIN = Path(sys.executable).parent / "coord"


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


def coord(project: Path, db: Path, *args: str, session: str, quiet: bool = False) -> dict:
    """Run one `coord` command as a named actor:session identity.

    `--db` is passed explicitly (never relying on a default), and every
    ambient identity variable the *real* CLI would otherwise pick up from a
    running Claude/Codex process is cleared first, so this recipe's identity
    is exactly the one it names below -- not whatever launched this script.
    """
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
    print(f"    $ coord {' '.join(args)}   [session={session}]")
    if result.returncode != 0:
        print(f"    -> refused: {result.stderr.strip()}")
        raise SystemExit(f"unexpected refusal from a step meant to succeed: {result.stderr.strip()}")
    if not quiet:
        print(f"    -> {result.stdout.strip()}")
    return json.loads(result.stdout)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="coord-example-two-agents-") as tmp:
        project = Path(tmp) / "project"
        project.mkdir()
        _git(project, "init", "-q")
        (project / ".gitignore").write_text(".coordharness/\n", encoding="utf-8")
        _git(project, "add", "-A")
        _git(project, "commit", "-qm", "initial")
        db = project / ".coordharness" / "coord.db"

        print("[1/4] Claude creates and claims its own row, declaring src/billing/")
        coord(
            project, db, "create", "DEMO-CLA-BILLING-RATES",
            "--title", "Rework rate lookup",
            "--module", "billing", "--tier", "T1",
            "--done-signal", "docs/reports/billing-rates.md",
            "--acceptance", "rate lookup reads the new table",
            "--note", "two-agents-one-file example",
            session="claude:frontend",
        )
        claude_claim = coord(
            project, db, "claim", "DEMO-CLA-BILLING-RATES",
            "--write-scope", "src/billing/",
            session="claude:frontend", quiet=True,
        )
        print(f"    -> claim {claude_claim['claim_id']}, write_set={claude_claim['write_set']}, "
              f"write_set_conflicts.count={claude_claim['write_set_conflicts']['count']}")
        assert claude_claim["write_set_conflicts"]["count"] == 0, "first claimant should see nothing yet"

        print("\n[2/4] Codex creates and claims a DIFFERENT row, declaring a file INSIDE that directory")
        coord(
            project, db, "create", "DEMO-CDX-BILLING-RETRY",
            "--title", "Fix retry backoff",
            "--module", "billing", "--tier", "T1",
            "--done-signal", "docs/reports/billing-retry.md",
            "--acceptance", "retries back off exponentially",
            "--note", "two-agents-one-file example",
            session="codex:backend",
        )
        codex_claim = coord(
            project, db, "claim", "DEMO-CDX-BILLING-RETRY",
            "--write-scope", "path=src/billing/retries.py",
            session="codex:backend", quiet=True,
        )
        print(f"    -> claim {codex_claim['claim_id']}, write_set={codex_claim['write_set']}")

        print("\n[3/4] The SECOND claim's own response names the collision immediately:")
        conflicts_at_claim = codex_claim["write_set_conflicts"]
        print(f"    write_set_conflicts = {json.dumps(conflicts_at_claim, indent=2)}")
        assert conflicts_at_claim["count"] == 1, "narrower scope inside the wider one must collide"

        print("\n[4/4] `coord conflicts` names both sides of the collision by row, claim, session, and scope:")
        report = coord(project, db, "conflicts", session="claude:frontend", quiet=True)
        assert report["count"] == 1
        finding = report["findings"][0]
        print(f"    -> {finding['describe']}")
        assert {finding["work_a"], finding["work_b"]} == {
            "DEMO-CLA-BILLING-RATES", "DEMO-CDX-BILLING-RETRY",
        }
        assert {finding["session_a"], finding["session_b"]} == {
            "claude:frontend", "codex:backend",
        }

        print(
            "\nNote: this is a REPORT, not a lock. Both claims still stand, and "
            "either agent can still edit -- `conflicts` is what an orchestrator "
            "checks BEFORE assigning the second job, or what a human checks when "
            "something looks wrong. It never silently blocks a write."
        )
        print("\nOK: the collision was surfaced by name, not discovered by a broken build.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
