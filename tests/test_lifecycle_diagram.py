from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
GENERATOR = REPO / "tools/generate_lifecycle_diagram.py"
SOURCE = REPO / "src/coordharness/coord/coord_db.py"
DIAGRAM = REPO / "docs/assets/lifecycle.svg"


def test_lifecycle_diagram_is_byte_identical_to_source(tmp_path: Path) -> None:
    generated = tmp_path / "lifecycle.svg"
    subprocess.run(
        [sys.executable, str(GENERATOR), "--source", str(SOURCE), "--output", str(generated)],
        cwd=REPO,
        check=True,
    )
    assert generated.read_bytes() == DIAGRAM.read_bytes()
    subprocess.run([sys.executable, str(GENERATOR), "--check"], cwd=REPO, check=True)


def test_lifecycle_generator_refuses_unknown_status(tmp_path: Path) -> None:
    mutated = tmp_path / "coord_db.py"
    source = SOURCE.read_text(encoding="utf-8")
    needle = '_HELD_CLAIM_STATUSES = ("running", "paused", "blocked")'
    assert needle in source
    mutated.write_text(
        source.replace(needle, '_HELD_CLAIM_STATUSES = ("running", "paused", "blocked", "mystery")'),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(GENERATOR), "--source", str(mutated), "--output", str(tmp_path / "out.svg")],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "no lifecycle diagram layout" in completed.stderr
    assert "mystery" in completed.stderr
