"""Regression coverage for COMPACT_VERIFY_COMMAND.

The constant used to point at ``coordharness/scripts/verify_jobs.py``, a file
that has never shipped in this repository. Every loop contract emitted by
this module told an agent to run that missing script as its verification
step, so the agent following the contract would either error or silently
skip verification. The fix repoints the constant at ``coord doctor``, the
read-only verifier this package actually installs (see
``[project.scripts]`` in ``pyproject.toml`` and ``tests/safety/test_safety_doctor.py``).

These tests pin two things so the drift cannot recur silently:
  * the constant never again names a script that does not exist in ``src/``;
  * the constant names a subcommand the installed CLI actually recognizes,
    and that subcommand exits 0 (PASS) against a freshly seeded harness --
    exactly the "run this before closing work" promise the loop contract
    makes to the agent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordharness.demo import seed as demo_seed
from coordharness.coord.cli import main as coord_main
from coordharness.coord.loop_contracts import (
    COMPACT_VERIFY_COMMAND,
    _proposal_for,
    scaffold_contract,
)

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"


def test_compact_verify_command_does_not_name_a_missing_script() -> None:
    assert "verify_jobs" not in COMPACT_VERIFY_COMMAND
    # Belt and suspenders: nothing named verify_jobs.py ships anywhere in the
    # source tree, so no future edit can silently repoint the constant at it.
    assert not list(_SRC_ROOT.rglob("verify_jobs.py"))


def test_compact_verify_command_names_a_subcommand_the_cli_recognizes() -> None:
    tokens = COMPACT_VERIFY_COMMAND.split()
    # The program must be the installed console script, resolved from PATH. An
    # earlier version of this assertion required a ".venv/bin/coord" prefix,
    # which pinned a checkout-relative path no clone has -- so the emitted
    # instruction stayed unrunnable while the test that existed to catch that
    # agreed with it.
    assert tokens[0] == "coord", COMPACT_VERIFY_COMMAND
    assert "/" not in COMPACT_VERIFY_COMMAND, COMPACT_VERIFY_COMMAND
    subcommand = tokens[1]

    # argparse exits 0 for a recognized subcommand's --help and 2 for an
    # unrecognized one -- this is precisely the check that would have caught
    # a command naming a script the package never installed.
    with pytest.raises(SystemExit) as excinfo:
        coord_main([subcommand, "--help"])
    assert excinfo.value.code == 0


def test_compact_verify_command_passes_clean_on_a_freshly_seeded_harness(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    project = tmp_path / "project"
    state = project / ".coordharness"
    project.mkdir()
    state.mkdir()
    db = state / "coord.db"
    demo_seed(db, quiet=True)

    subcommand = COMPACT_VERIFY_COMMAND.split()[1]
    exit_code = coord_main(
        [
            "--db",
            str(db),
            subcommand,
            "--project-root",
            str(project),
            "--state-root",
            str(state),
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert report["status"] == "PASS"
    assert report["findings"]
    assert all(finding["status"] == "PASS" for finding in report["findings"])


def test_scaffold_contract_verify_step_and_evidence_share_the_constant() -> None:
    contract = scaffold_contract("coord", title="example loop")
    assert contract["verify"] == [f"run {COMPACT_VERIFY_COMMAND}"]
    assert contract["evidence"]["required_commands"] == [COMPACT_VERIFY_COMMAND]


def test_supervisor_proposal_verify_step_and_evidence_share_the_constant() -> None:
    row = {"id": "DEMO-TEST", "title": "example row"}
    suggestion = {"kind": "repeated_tool_pattern"}

    proposal = _proposal_for(row, suggestion)
    contract = proposal["proposed_loop_contract"]

    assert f"Run {COMPACT_VERIFY_COMMAND} before closing any boarded work." in contract["verify"]
    assert COMPACT_VERIFY_COMMAND in contract["evidence"]["required_commands"]
