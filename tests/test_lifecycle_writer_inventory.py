"""The direct-writer guard was asserted to exist, never asserted to fire.

`doctor.lifecycle_writers` is the check that stops a lifecycle table from
acquiring a second writer: every `INSERT`/`UPDATE`/`DELETE` against `claims`,
`work_items`, `events` and their siblings must live in one of the modules the
doctor allows, so the invariants those modules enforce cannot be routed around
by a new caller opening its own connection.

The only existing coverage asserted that a finding with that id appeared in a
PASS report. That is compatible with the check returning nothing at all: with
`unexpected_writer_modules` stubbed to return an empty list, and with the AST
walker stubbed to find no call sites, the suite stayed green. Those are the two
ablations these tests are shaped against -- the walker must actually find
writes, and an unallowed module holding one must come back BLOCKED and be named.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordharness.safety import doctor
from coordharness.safety.writers import (
    LIFECYCLE_TABLES,
    inventory_lifecycle_writers,
    unexpected_writer_modules,
)

_ROGUE_MODULE = "rogue/side_writer.py"
_ROGUE_SOURCE = """
def promote(conn, claim_id):
    conn.execute("UPDATE claims SET status='running' WHERE claim_id=?", (claim_id,))
    conn.executemany("INSERT INTO events(kind) VALUES (?)", [("x",)])


def read_only(conn):
    return conn.execute("SELECT status FROM claims").fetchall()
"""


@pytest.fixture
def rogue_package(tmp_path: Path) -> Path:
    package = tmp_path / "pkg"
    (package / "rogue").mkdir(parents=True)
    (package / "rogue" / "side_writer.py").write_text(_ROGUE_SOURCE, encoding="utf-8")
    (package / "bootstrap.py").write_text(
        '\ndef seed(conn):\n'
        '    conn.execute("INSERT INTO work_items(work_id) VALUES (?)", ("W",))\n',
        encoding="utf-8",
    )
    return package


def test_the_walker_finds_writes_and_ignores_reads(rogue_package: Path) -> None:
    sites, parse_errors = inventory_lifecycle_writers(rogue_package)

    assert parse_errors == []
    assert sites, "the inventory found no writer call sites at all"
    modules = {site.module for site in sites}
    assert modules == {_ROGUE_MODULE, "bootstrap.py"}

    rogue_sites = [site for site in sites if site.module == _ROGUE_MODULE]
    assert len(rogue_sites) == 2, "execute and executemany must both be inventoried"
    assert {op for site in rogue_sites for op in site.operations} == {
        "INSERT",
        "UPDATE",
    }
    assert {table for site in rogue_sites for table in site.tables} == {
        "claims",
        "events",
    }
    # The module holds three `execute`-family calls and only two are writes:
    # a walker that reported the `SELECT` too would make the allowlist
    # meaningless, and the count above is what refuses it.


def test_an_unallowed_module_is_named(rogue_package: Path) -> None:
    sites, _ = inventory_lifecycle_writers(rogue_package)

    assert unexpected_writer_modules(sites, allowed_modules={"bootstrap.py"}) == [
        _ROGUE_MODULE
    ]
    # Widening the allowlist is the only thing that clears it.
    assert (
        unexpected_writer_modules(
            sites, allowed_modules={"bootstrap.py", _ROGUE_MODULE}
        )
        == []
    )


def test_a_syntax_error_is_reported_rather_than_swallowed(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "broken.py").write_text("def f(:\n", encoding="utf-8")

    sites, parse_errors = inventory_lifecycle_writers(package)

    assert sites == []
    assert parse_errors == ["broken.py"]


def test_doctor_blocks_on_an_unallowed_writer(
    rogue_package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "_ALLOWED_WRITER_MODULES", {"bootstrap.py"})

    finding = doctor._check_writers(rogue_package)

    assert finding.id == "doctor.lifecycle_writers"
    assert finding.status == "BLOCKED"
    assert finding.details["unexpected_modules"] == [_ROGUE_MODULE]
    assert any(
        item.code == "doctor.lifecycle_writers.unexpected_module"
        for item in finding.remediations
    )


def test_doctor_passes_when_every_writer_is_allowed(
    rogue_package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        doctor, "_ALLOWED_WRITER_MODULES", {"bootstrap.py", _ROGUE_MODULE}
    )

    finding = doctor._check_writers(rogue_package)

    assert finding.status == "PASS"
    assert finding.details["unexpected_modules"] == []
    assert finding.details["direct_writer_site_count"] == 3


def test_the_shipped_package_has_no_unallowed_lifecycle_writer() -> None:
    package_root = Path(doctor.__file__).resolve().parents[1]

    sites, parse_errors = inventory_lifecycle_writers(package_root)

    assert parse_errors == []
    assert sites, "no lifecycle writers found in the shipped package"
    assert {table for site in sites for table in site.tables} <= LIFECYCLE_TABLES
    assert (
        unexpected_writer_modules(
            sites, allowed_modules=doctor._ALLOWED_WRITER_MODULES
        )
        == []
    )
