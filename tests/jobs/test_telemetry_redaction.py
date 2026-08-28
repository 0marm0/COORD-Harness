"""Telemetry must not carry argv, host paths, or generated output bodies.

Job sidecars and coord `runs` rows are the two places the harness writes machine
state to disk on a reader's behalf. Three things must never reach them:

* an **absolute host path**, which names the operator's machine and account;
* an **argv**, which carries whatever secrets a command line was invoked with;
* a **generated output body**, which is the silent variant -- nobody notices a
  model's answer smuggled into a `step` or `reason` field, because a status
  field with 40kB in it still renders as a status field.

The sweep below is a pure function over decoded JSON so it can be pointed at a
fixture as easily as at a real board. `test_sweep_is_live` points it at a record
that MUST fail on all three rules; without that, a sweep that silently stopped
matching would read as a clean estate.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from coordharness import demo
from coordharness.bootstrap import bootstrap_database
from coordharness.coord import coord_db
from coordharness.coord.config import connect

# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

#: Prefixes that identify an absolute path on a real host. Tests add the
#: temporary root they seed under, because a tmpdir lives under `/var` on macOS
#: and a sweep that only knew these two would pass on every fixture by accident.
HOST_PATH_PREFIXES: tuple[str, ...] = ("/Users/", "/home/")

#: A status/step/reason field longer than this is carrying a body, not a status.
MAX_FIELD_CHARS = 2000

#: A key holding an argv. `original_argv_sha256` and friends are digests of one,
#: which is the redacted form and is deliberately allowed; `argv` and
#: `child_argv` are the command line itself and are not.
_ARGV_KEY = re.compile(r"^(?:.*_)?argv$", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    """One rule violation, located precisely enough to fix without a search."""

    source: str
    location: str
    kind: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - only ever read in a failure
        return f"{self.source}:{self.location}: {self.kind}: {self.detail}"


def _host_path_tokens(text: str, prefixes: Sequence[str]) -> list[str]:
    """Return whitespace/punctuation-delimited tokens that begin a host path."""
    tokens = re.split(r"""[\s"'`,;=()\[\]{}<>|]+""", text)
    return [tok for tok in tokens if any(tok.startswith(p) for p in prefixes)]


def sweep_record(
    source: str,
    record: Any,
    *,
    host_prefixes: Sequence[str] = HOST_PATH_PREFIXES,
    max_chars: int = MAX_FIELD_CHARS,
) -> list[Finding]:
    """Walk one decoded-JSON record and report every redaction violation.

    Pure: no filesystem, no database, no clock. `record` is whatever
    `json.loads` produced, or an equivalent dict of a database row.
    """
    findings: list[Finding] = []

    def walk(node: Any, location: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{location}.{key}" if location else str(key)
                if isinstance(key, str) and _ARGV_KEY.match(key.strip()):
                    findings.append(
                        Finding(source, child, "argv_key",
                                f"key {key!r} carries a command line")
                    )
                walk(value, child)
            return
        if isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                walk(value, f"{location}[{index}]")
            return
        if isinstance(node, str):
            for token in _host_path_tokens(node, host_prefixes):
                findings.append(
                    Finding(source, location, "host_path",
                            f"absolute host path {token!r}")
                )
            if len(node) > max_chars:
                findings.append(
                    Finding(source, location, "oversize_field",
                            f"{len(node)} chars exceeds the {max_chars}-char field ceiling")
                )

    walk(record, "")
    return findings


def sweep(
    records: Iterable[tuple[str, Any]],
    *,
    host_prefixes: Sequence[str] = HOST_PATH_PREFIXES,
    max_chars: int = MAX_FIELD_CHARS,
) -> list[Finding]:
    """Sweep an iterable of (source, decoded-record) pairs."""
    out: list[Finding] = []
    for source, record in records:
        out.extend(
            sweep_record(source, record, host_prefixes=host_prefixes, max_chars=max_chars)
        )
    return out


def string_leaves(record: Any) -> Iterator[str]:
    """Every string leaf in a decoded record, in document order."""
    if isinstance(record, dict):
        for value in record.values():
            yield from string_leaves(value)
    elif isinstance(record, (list, tuple)):
        for value in record:
            yield from string_leaves(value)
    elif isinstance(record, str):
        yield record


def absolute_path_leaves(records: Iterable[tuple[str, Any]]) -> list[str]:
    """Leaves that look like an absolute path on any host, prefix rules aside.

    Used to prove a prefix list is not blind: a sweep configured with a prefix
    that matches nothing reports a clean estate for the wrong reason.
    """
    return [leaf for _, record in records for leaf in string_leaves(record)
            if leaf.startswith("/")]


def count_string_leaves(record: Any) -> int:
    """How many string leaves a record actually offered the sweep.

    A sweep over nothing returns no findings, which is indistinguishable from a
    clean estate. Every real-data test below asserts this is non-zero.
    """
    if isinstance(record, dict):
        return sum(count_string_leaves(v) for v in record.values())
    if isinstance(record, (list, tuple)):
        return sum(count_string_leaves(v) for v in record)
    return 1 if isinstance(record, str) else 0


# ---------------------------------------------------------------------------
# Proof that the sweep is live
# ---------------------------------------------------------------------------

#: A record that violates all three rules, each at a different nesting depth.
DIRTY_FIXTURE: dict[str, Any] = {
    "job_id": "leaky-job",
    "step": "x" * (MAX_FIELD_CHARS + 1),
    "sidecar_path": "/" + "Users/someone/Developer/proj/.coordharness/job_progress/leaky-job.json",
    "launch": {
        "attempts": [
            {"argv": ["python", "-m", "train", "--token", "sk-" + "not-a-real-secret"]},
        ],
        "cwd": "/home/runner/work/proj",
    },
    "original_argv_sha256": "0" * 64,
}


def test_sweep_is_live() -> None:
    findings = sweep_record("fixture", DIRTY_FIXTURE)
    by_kind: dict[str, list[Finding]] = {}
    for finding in findings:
        by_kind.setdefault(finding.kind, []).append(finding)

    assert sorted(by_kind) == ["argv_key", "host_path", "oversize_field"], findings

    assert [f.location for f in by_kind["argv_key"]] == ["launch.attempts[0].argv"]
    assert sorted(f.location for f in by_kind["host_path"]) == [
        "launch.cwd",
        "sidecar_path",
    ]
    assert [f.location for f in by_kind["oversize_field"]] == ["step"]


def test_argv_digest_is_not_an_argv() -> None:
    """The redacted form must not be reported, or the rule is unusable."""
    assert sweep_record("fixture", {"original_argv_sha256": "0" * 64}) == []
    assert sweep_record("fixture", {"child_argv": ["python"]})[0].kind == "argv_key"


def test_field_ceiling_is_exact() -> None:
    assert sweep_record("fixture", {"step": "x" * MAX_FIELD_CHARS}) == []
    assert sweep_record("fixture", {"step": "x" * (MAX_FIELD_CHARS + 1)})[0].kind == (
        "oversize_field"
    )


# ---------------------------------------------------------------------------
# Real telemetry
# ---------------------------------------------------------------------------


def _read_sidecars(directory: Path) -> list[tuple[str, Any]]:
    import json

    return [
        (f"sidecar:{path.name}", json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(directory.glob("*.json"))
    ]


def _text_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        if str(row[2]).upper().startswith("TEXT")
    ]


def _read_runs(db: Path) -> tuple[list[tuple[str, Any]], list[str]]:
    """Every row of `runs`, restricted to its text columns."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        columns = _text_columns(conn, "runs")
        rows = conn.execute(f"SELECT {', '.join(columns)} FROM runs").fetchall()
        records = [
            (
                f"runs[{index}]",
                {col: row[col] for col in columns if isinstance(row[col], str)},
            )
            for index, row in enumerate(rows)
        ]
    finally:
        conn.close()
    return records, columns


def test_demo_sidecars_carry_no_argv_paths_or_bodies(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("COORD_HOME", str(state))
    monkeypatch.setenv("COORD_PROJECT_ROOT", str(tmp_path / "project"))
    (tmp_path / "project").mkdir()
    demo.seed(state / "coord.db", quiet=True)

    records = _read_sidecars(state / "job_progress")
    assert records, "demo seeded no sidecars; the sweep would be vacuous"
    assert sum(count_string_leaves(r) for _, r in records) > 0

    # The tmpdir prefix is added so the rule can actually fire here: a real host
    # writes under /Users or /home, a test writes under /var.
    # Prefix-independent: a redacted sidecar carries `state://...` refs, so any
    # leading-slash leaf is a host path regardless of which host wrote it.
    assert absolute_path_leaves(records) == []

    findings = sweep(
        records, host_prefixes=(*HOST_PATH_PREFIXES, f"{tmp_path.resolve()}/", f"{tmp_path}/")
    )
    assert findings == [], "\n".join(str(f) for f in findings)


def _seed_binding(root: Path) -> dict[str, str]:
    db = root / "state" / "coord.db"
    bootstrap_database(db)
    conn = connect(db)
    try:
        session_id = "local:telemetry-redaction"
        coord_db.register_session(conn, session_id, "local", lease_s=600)
        coord_db.upsert_work(conn, "WORK-1", title="Telemetry redaction fixture",
                             assignee="local")
        claim_id = coord_db.claim_work(conn, session_id, "WORK-1", step="launch", lease_s=600)
        fence = conn.execute(
            "SELECT lease_token FROM claims WHERE claim_id=?", (claim_id,)
        ).fetchone()["lease_token"]
    finally:
        conn.close()
    return {"db": str(db), "session_id": session_id, "claim_id": claim_id,
            "claim_fence": str(fence), "work_id": "WORK-1"}


def _run_tracked_job(root: Path, binding: dict[str, str], job_id: str) -> None:
    project = root / "project"
    project.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "COORD_PROJECT_ROOT": str(project),
        "COORD_HOME": str(root / "state"),
        "COORD_DB": binding["db"],
        "COORD_ACTOR": "local",
    }
    proc = subprocess.run(
        [
            sys.executable, "-m", "coordharness.jobs.cli", "launch",
            "--job-id", job_id,
            "--roadmap-id", binding["work_id"],
            "--session-id", binding["session_id"],
            "--claim-id", binding["claim_id"],
            "--claim-fence", binding["claim_fence"],
            "--cap-gb", "1",
            "--", sys.executable, "-c",
            # A command line with something worth redacting in it, so a leak of
            # argv into either surface would be unmistakable.
            "import time; time.sleep(0.1)  # --token sk-" + "not-a-real-secret",
        ],
        cwd=project, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


def test_tracked_launch_sidecar_carries_no_argv_paths_or_bodies(tmp_path: Path) -> None:
    binding = _seed_binding(tmp_path)
    _run_tracked_job(tmp_path, binding, "redaction-probe")

    records = _read_sidecars(tmp_path / "state" / "job_progress")
    assert records, "the tracked launch wrote no sidecar; the sweep would be vacuous"
    assert sum(count_string_leaves(r) for _, r in records) > 0

    assert absolute_path_leaves(records) == []

    findings = sweep(
        records, host_prefixes=(*HOST_PATH_PREFIXES, f"{tmp_path.resolve()}/", f"{tmp_path}/")
    )
    assert findings == [], "\n".join(str(f) for f in findings)


def test_run_records_carry_no_argv_and_no_output_bodies(tmp_path: Path) -> None:
    """Every text column of every `runs` row, from a real tracked launch.

    `sidecar_path` is excluded from the host-path rule and *only* from it: the
    column is a live filesystem handle that `coord/projection.py` and
    `safety/doctor.py` open to read the sidecar, so redacting it at the writer
    would break those readers rather than fix anything. It is reported as an
    unfixed at-rest leak, not silently allowed -- and the assertion is an upper
    bound, so a second column acquiring a host path fails here.
    """
    binding = _seed_binding(tmp_path)
    _run_tracked_job(tmp_path, binding, "redaction-probe")

    records, columns = _read_runs(Path(binding["db"]))
    assert records, "no runs rows were written; the sweep would be vacuous"
    assert columns, "runs exposed no text columns; the sweep would be vacuous"
    assert sum(count_string_leaves(r) for _, r in records) > 0

    prefixes = (*HOST_PATH_PREFIXES, f"{tmp_path.resolve()}/", f"{tmp_path}/",
                f"/private{tmp_path.resolve()}/")
    # Liveness: a prefix list that matched none of the absolute paths actually
    # present would report a clean table for the wrong reason.
    uncovered = [leaf for leaf in absolute_path_leaves(records)
                 if not any(leaf.startswith(p) for p in prefixes)]
    assert uncovered == [], (
        "the host-path rule is blind to paths this board really wrote:\n"
        + "\n".join(uncovered)
    )

    findings = sweep(records, host_prefixes=prefixes)

    argv_and_bodies = [f for f in findings if f.kind != "host_path"]
    assert argv_and_bodies == [], "\n".join(str(f) for f in argv_and_bodies)

    leaking_columns = {f.location.split(".")[-1] for f in findings if f.kind == "host_path"}
    assert leaking_columns <= {"sidecar_path"}, (
        "a runs column other than the known sidecar_path handle leaked a host path:\n"
        + "\n".join(str(f) for f in findings)
    )


def test_runs_sweep_would_catch_a_new_leaking_column() -> None:
    """The upper-bound assertion above is only meaningful if it can fail."""
    record = {"run_id": "job:x:1", "state": "/" + "Users/someone/scratch/out.json"}
    leaking = {
        f.location.split(".")[-1]
        for f in sweep_record("runs[0]", record)
        if f.kind == "host_path"
    }
    assert leaking == {"state"}
    assert not leaking <= {"sidecar_path"}
