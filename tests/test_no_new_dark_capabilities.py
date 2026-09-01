"""A capability that nothing calls must not be added silently.

Four were found in a single night: an event kind with no producer, a
work-in-progress cap that existed only in a failing test, a review-quorum module
whose own docstring said nothing called it, and a reaper that released a thousand
claims without recording one. None was untested — three were fully tested and
green, which is exactly why they stayed invisible.

This is a **test**, not a standalone lint, and that is the point. Nine lint
modules in this repo are themselves unimported: the port carried the module and
dropped the driver. A checker that needed its own driver would have become the
next entry on the list it exists to shorten. Tests already run.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ALLOWLIST = REPO / "tools" / "dark_capability_allowlist.json"


def _check():
    spec = importlib.util.spec_from_file_location(
        "dark_capability_check", REPO / "tools" / "dark_capability_check.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_module_is_unreachable_without_a_written_reason():
    check = _check()
    allowed = check.load_allowlist(REPO)
    unexplained = [
        (name, where) for name, where in check.dark_modules(REPO) if name not in allowed
    ]
    assert not unexplained, (
        "these modules are imported by nothing outside the tests:\n  "
        + "\n  ".join(f"{name} ({where})" for name, where in unexplained)
        + "\nWire it, delete it, or add it to tools/dark_capability_allowlist.json"
        " with a reason a later reader can act on."
    )


def test_no_module_claims_to_be_unwired_while_something_calls_it():
    """A stale marker is worse than silence: it hands the next reader a false
    map of what this harness enforces. One existed the instant the quorum module
    was wired, and the check caught it on its first run."""
    check = _check()
    stale = check.stale_markers(REPO)
    assert not stale, (
        "these say they are unwired but are imported:\n  "
        + "\n  ".join(f"{name} ({where})" for name, where in stale)
    )


def test_every_allowlist_entry_carries_a_reason_somebody_can_act_on():
    """'not used yet' is how the four findings became invisible in the first
    place, so an entry has to say something a later reader can decide on."""
    entries = json.loads(ALLOWLIST.read_text(encoding="utf-8"))["dark"]
    assert entries, "an empty allowlist means the check was never run against the tree"
    for name, reason in entries.items():
        assert len(reason.split()) >= 8, f"{name}: reason is too thin to act on: {reason!r}"


def test_the_allowlist_does_not_outlive_what_it_excuses():
    """An entry for a module that no longer exists is a stale exemption, and a
    stale exemption silently re-admits the next module to take that name."""
    check = _check()
    entries = json.loads(ALLOWLIST.read_text(encoding="utf-8"))["dark"]
    live = {name for name, _ in check.dark_modules(REPO)}
    orphaned = sorted(set(entries) - live)
    assert not orphaned, (
        f"allowlisted but no longer dark (wired or deleted): {orphaned}. "
        "Remove the entries -- the count is supposed to go down."
    )


def test_the_check_actually_notices_a_new_dark_module(tmp_path: Path):
    """The guard's own ablation. A checker that reports nothing on a tree that
    contains something is the failure mode this whole exercise is about."""
    check = _check()
    fake = tmp_path / "repo"
    (fake / "src" / "pkg").mkdir(parents=True)
    (fake / "src" / "pkg" / "__init__.py").write_text("")
    (fake / "src" / "pkg" / "reachable.py").write_text("def f():\n    return 1\n")
    (fake / "src" / "pkg" / "orphan.py").write_text("def g():\n    return 2\n")
    (fake / "src" / "pkg" / "caller.py").write_text(
        "from pkg import reachable\n\n\ndef h():\n    return reachable.f()\n"
    )
    # caller.py itself is imported by nothing, so it is dark too; the module
    # under test is `orphan`, and it must appear.
    found = {name for name, _ in check.dark_modules(fake)}
    assert "orphan" in found
    assert "reachable" not in found, "an imported module must not be reported"


def test_build_metadata_is_not_mistaken_for_a_caller(tmp_path: Path):
    """Provenance manifests and egg-info LIST every file. Counting them made two
    genuinely unwired lint modules look reachable on the first run."""
    check = _check()
    fake = tmp_path / "repo"
    (fake / "src" / "pkg").mkdir(parents=True)
    (fake / "src" / "pkg" / "__init__.py").write_text("")
    (fake / "src" / "pkg" / "orphan.py").write_text("def g():\n    return 2\n")
    (fake / "src" / "pkg.egg-info").mkdir(parents=True)
    (fake / "src" / "pkg.egg-info" / "SOURCES.txt").write_text("src/pkg/orphan.py\n")

    assert "orphan" in {name for name, _ in check.dark_modules(fake)}


def test_packaging_output_is_not_mistaken_for_a_caller(tmp_path: Path):
    """A wheel build leaves build/lib/<pkg>/ holding a copy of every module. On
    2026-09-01 that copy carried the only import of a freshly wired lint, so an
    ablation that removed the real driver stayed green and named one module
    where it should have named two."""
    check = _check()
    fake = tmp_path / "repo"
    (fake / "src" / "pkg").mkdir(parents=True)
    (fake / "src" / "pkg" / "__init__.py").write_text("")
    (fake / "src" / "pkg" / "orphan.py").write_text("def g():\n    return 2\n")
    for out in ("build/lib/pkg", "dist/pkg"):
        (fake / out).mkdir(parents=True)
        (fake / out / "driver.py").write_text("from pkg import orphan\n")

    assert "orphan" in {name for name, _ in check.dark_modules(fake)}
    # And the exclusion is by top-level prefix, so a real package directory
    # that happens to be called build/ is still a caller.
    (fake / "src" / "pkg" / "build").mkdir()
    (fake / "src" / "pkg" / "build" / "__init__.py").write_text("")
    (fake / "src" / "pkg" / "build" / "wire.py").write_text("from pkg import orphan\n")
    assert "orphan" not in {name for name, _ in check.dark_modules(fake)}
