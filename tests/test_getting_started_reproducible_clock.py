"""The one override the getting-started page suggests turns the board red.

`docs/getting-started.md` offers `SOURCE_DATE_EPOCH` for a byte-reproducible
capture, and a few paragraphs later promises `coord doctor` prints
`"status": "PASS"`. Both cannot be true at once: the seeder honours the frozen
clock and backdates every lease, doctor reads the real clock, and every lease
therefore looks expired.

The behaviour is pinned here rather than the wording alone, so that if doctor is
ever taught to honour the same clock, this fails and the page gets rewritten
instead of quietly becoming wrong in the other direction.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
FROZEN_EPOCH = "1735689600"  # 2025-01-01T00:00:00Z, well before any lease window


def _env(project: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    for leaked in (
        "CODEX_SESSION_ID",
        "COORD_ACTOR",
        "COORD_SESSION_ID",
        "COORD_DB",
        "SOURCE_DATE_EPOCH",
    ):
        env.pop(leaked, None)
    env.update({
        "PYTHONPATH": str(SRC),
        "COORD_PROJECT_ROOT": str(project),
        "COORD_HOME": str(project / ".coordharness"),
        "CLAUDE_CODE_SESSION_ID": "claude:frontend",
    })
    env.update(extra)
    return env


def _seed_then_doctor(project: Path, **extra: str) -> dict:
    project.mkdir(parents=True, exist_ok=True)
    seeded = subprocess.run(
        [sys.executable, "-m", "coordharness.demo", "--quiet"],
        cwd=project,
        capture_output=True,
        text=True,
        env=_env(project, **extra),
        timeout=300,
    )
    assert seeded.returncode == 0, seeded.stderr

    checked = subprocess.run(
        [sys.executable, "-m", "coordharness.coord.cli", "doctor"],
        cwd=project,
        capture_output=True,
        text=True,
        env=_env(project),
        timeout=300,
    )
    assert checked.stdout, checked.stderr
    report = json.loads(checked.stdout)
    return next(item for item in report["findings"] if item["id"] == "doctor.leases_reviews")


def test_the_default_clock_leaves_the_demo_leases_live(tmp_path: Path) -> None:
    finding = _seed_then_doctor(tmp_path / "default-clock")

    assert finding["status"] == "PASS"
    assert finding["details"]["expired_claim_count"] == 0


def test_the_suggested_override_expires_every_demo_lease(tmp_path: Path) -> None:
    finding = _seed_then_doctor(tmp_path / "frozen-clock", SOURCE_DATE_EPOCH=FROZEN_EPOCH)

    assert finding["status"] == "BLOCKED"
    assert finding["details"]["expired_claim_count"] > 0
    assert finding["details"]["expired_session_count"] > 0


@pytest.mark.parametrize(
    "phrase",
    [
        "expect a red board when you do",
        'Seeded without `SOURCE_DATE_EPOCH`, it prints `"status": "PASS"`',
    ],
)
def test_the_page_says_so_where_it_makes_the_suggestion(phrase: str) -> None:
    assert phrase in (REPO / "docs" / "getting-started.md").read_text(encoding="utf-8")
