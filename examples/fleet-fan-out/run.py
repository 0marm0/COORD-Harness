#!/usr/bin/env python3
"""GUARD: a fleet's subagent history can only be recorded under a HELD claim.

`record_child_attempt` is the Python-level surface a fan-out orchestrator
uses to log each subagent it spawns against the ONE board row it holds --
so a dozen subagents show up as one row's history, not a dozen invisible
rows. There is no CLI verb and no MCP tool for it yet (grep the repo: it is
reachable only as `coordharness.coord.work_contracts.record_child_attempt`).
That is the point of this recipe -- it exercises a real, shipped guard that
no other example in this repo touches.

The guard: recording a child attempt against a claim_id that is not
currently held -- because it was never claimed, released, or already
completed -- raises `UnclaimedFleetError` rather than silently writing an
orphaned row. "No claim -> no tracking" is enforced in code, not just
asserted in a doc.

This script creates its own throwaway git repo and its own throwaway
coord.db under a fresh `tempfile.TemporaryDirectory()` and deletes both
when it exits. It never opens, reads, or writes any database that existed
before it ran.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from coordharness.coord.config import connect
from coordharness.coord.work_contracts import (
    UnclaimedFleetError,
    child_attempts,
    record_child_attempt,
    record_child_outcome,
)

COORD_BIN = Path(sys.executable).parent / "coord"
WORK_ID = "DEMO-CLA-MARKET-SCAN"
ARTIFACT_REL = "docs/reports/market-scan-summary.md"
SESSION = "claude:fleet-lead"


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


def coord(project: Path, db: Path, *args: str) -> dict:
    env = {**os.environ, "COORD_PROJECT_ROOT": str(project), "COORD_HOME": str(project / ".coordharness")}
    for leaked in (
        "CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID",
        "CODEX_WORKTREE_ID", "CODEX_CONVERSATION_ID", "STARSHIP_SESSION_KEY",
        "COORD_ACTOR", "COORD_SESSION_ID", "COORD_PARENT_SESSION_ID",
    ):
        env.pop(leaked, None)
    env["COORD_ACTOR"] = SESSION.split(":", 1)[0]
    env["COORD_SESSION_ID"] = SESSION

    result = subprocess.run(
        [str(COORD_BIN), "--db", str(db), *args],
        cwd=project, capture_output=True, text=True, env=env,
    )
    print(f"    $ coord {' '.join(args)}")
    if result.returncode != 0:
        print(f"    -> refused: {result.stderr.strip()}")
        raise SystemExit(f"unexpected refusal from a step meant to succeed: {result.stderr.strip()}")
    print(f"    -> {result.stdout.strip()}")
    return json.loads(result.stdout)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="coord-example-fleet-fan-out-") as tmp:
        project = Path(tmp) / "project"
        project.mkdir()
        _git(project, "init", "-q")
        (project / ".gitignore").write_text(".coordharness/\n", encoding="utf-8")
        _git(project, "add", "-A")
        _git(project, "commit", "-qm", "initial")
        db = project / ".coordharness" / "coord.db"

        print(f"[1/5] Create and claim {WORK_ID} -- ONE row for the whole fan-out")
        coord(
            project, db, "create", WORK_ID,
            "--title", "Scan competitor pricing pages",
            "--module", "research", "--tier", "T1",
            "--done-signal", ARTIFACT_REL,
            "--acceptance", "summary cites every subagent's finding",
            "--note", "fleet-fan-out example",
        )
        claimed = coord(project, db, "claim", WORK_ID, "--step", "spawning subagents")
        held_claim_id = claimed["claim_id"]

        print("\n[2/5] GUARD: recording a child attempt against a claim nobody holds fails")
        conn = connect(db)
        try:
            bogus_claim_id = "clm-never-claimed-0000"
            try:
                record_child_attempt(
                    conn,
                    claim_id=bogus_claim_id,
                    child_label="explorer-pricing",
                    executed_by=SESSION,
                    model="sonnet-4.5",
                )
                raise SystemExit("expected UnclaimedFleetError, got no exception")
            except UnclaimedFleetError as exc:
                print(f"    record_child_attempt(claim_id={bogus_claim_id!r}, ...)")
                print(f"    -> UnclaimedFleetError: {exc}")

            print(f"\n[3/5] Record three subagent attempts under the HELD claim {held_claim_id}")
            attempts = []
            for label, actor, model in (
                ("explorer-pricing", "claude:fleet-lead/sub-1", "sonnet-4.5"),
                ("explorer-competitors", "claude:fleet-lead/sub-2", "sonnet-4.5"),
                ("synthesizer", "claude:fleet-lead/sub-3", "haiku-4.5"),
            ):
                attempt = record_child_attempt(
                    conn, claim_id=held_claim_id, child_label=label,
                    executed_by=actor, model=model,
                )
                attempts.append(attempt)
                print(f"    recorded {attempt.attempt_id}  label={label!r}  model={model!r}")

            print("\n[4/5] Record outcomes (one failure -- a fleet's history keeps the failure, not just the wins)")
            record_child_outcome(conn, attempt_id=attempts[0].attempt_id, outcome="completed",
                                  outcome_ref="docs/reports/pricing-notes.md")
            record_child_outcome(conn, attempt_id=attempts[1].attempt_id, outcome="failed",
                                  outcome_ref="timed out after 40 turns, no citations found")
            record_child_outcome(conn, attempt_id=attempts[2].attempt_id, outcome="completed",
                                  outcome_ref=ARTIFACT_REL)

            history = child_attempts(conn, work_id=WORK_ID)
            print(f"\n    child_attempts(work_id={WORK_ID!r}) -> {len(history)} rows, ONE parent claim:")
            for h in history:
                print(f"      {h.child_label:<22} model={h.model:<10} outcome={h.outcome:<10} ref={h.outcome_ref}")
        finally:
            conn.close()

        print("\n[5/5] Write the summary artifact citing the fleet history, stage it, and close the ONE row")
        artifact = project / ARTIFACT_REL
        artifact.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# Market scan summary\n", "\nSubagent history for this claim:\n"]
        for h in history:
            lines.append(f"- {h.child_label} ({h.model}): {h.outcome} -- {h.outcome_ref}\n")
        artifact.write_text("".join(lines), encoding="utf-8")
        _git(project, "add", ARTIFACT_REL)
        payload = coord(project, db, "done", WORK_ID, "--artifact", ARTIFACT_REL)
        assert payload["ok"] is True

        board = coord(project, db, "board")
        research_rows = [r for r in board["rows"] if r["work_id"] == WORK_ID]
        assert len(research_rows) == 1, "the fan-out must still be exactly one board row"
        assert research_rows[0]["status"] == "done"
        print(f"\n    board row count for {WORK_ID}: {len(research_rows)} (status={research_rows[0]['status']})")

        print("\nOK: no held claim -> UnclaimedFleetError; three subagent attempts (one failed)")
        print("    rolled up under the single claim they were spawned under; one board row closed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
