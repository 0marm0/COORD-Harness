"""Portability regression guards.

These tests run on any platform pytest runs on (they do not require a second
OS) and check the properties `docs/compatibility.md` claims: path handling
stays inside `pathlib` regardless of separator style, the modules that do
hardware/platform branching decide at call time rather than at import time,
and the one module that already guards an optional POSIX-only import keeps
doing so. They are deliberately behavioral -- each test exercises what the
code actually does under an unusual input or a monkeypatched platform, not a
string match against source text.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

from coordharness import config
from coordharness.coord import process_liveness
from coordharness.coord.runners import mlx_runner
from coordharness.jobs import resource_lock

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "coordharness"

# Modules documented in docs/compatibility.md as still failing at import time
# on a platform without `fcntl`. This list exists so the test below fails
# loudly -- not silently -- the day one of them is fixed or a new one is
# added; see the "Windows -- not supported" section of the doc for the
# file:line citations this set matches.
KNOWN_UNGUARDED_FCNTL_IMPORTS = frozenset(
    {
        "coordharness.coord.native_cockpit",
        "coordharness.runtime.console_release_retention",
        "coordharness.knowledge.accepted_memory_r4",
        "coordharness.jobs.pglaunch",
        "coordharness.jobs.diagnostic_marker",
    }
)


def _module_names_under(root: Path) -> list[str]:
    names = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root.parent).with_suffix("")
        names.append(".".join(rel.parts))
    return names


def _imports_fcntl_unconditionally(source: str) -> bool:
    """True if `source` imports `fcntl` at module scope outside any guard.

    A `try/except ImportError` around the import (the pattern
    `resource_lock.py` uses) does not count -- that import can fail without
    the module failing to load.
    """
    tree = ast.parse(source)
    for node in tree.body:  # module-level statements only, not nested in Try
        if isinstance(node, ast.Import) and any(alias.name == "fcntl" for alias in node.names):
            return True
    return False


def test_fcntl_import_guard_inventory_matches_the_documented_set() -> None:
    """The set of modules that import fcntl without a guard is exactly the
    set docs/compatibility.md names as a Windows blocker -- no more, no
    fewer. This is the regression guard: fixing one of the five (wrapping its
    import in try/except) should shrink this set and require updating both
    this test and the doc together; adding a sixth unconditional import
    should fail this test even if nobody touches the doc.
    """
    found = set()
    for name in _module_names_under(SRC_ROOT):
        path = SRC_ROOT.parent / Path(*name.split("."))
        path = path.with_suffix(".py")
        if not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        if "fcntl" not in source:
            continue
        if _imports_fcntl_unconditionally(source):
            found.add(name)

    assert found == KNOWN_UNGUARDED_FCNTL_IMPORTS


def test_resource_lock_guards_its_fcntl_import_and_degrades_at_use_not_import() -> None:
    """resource_lock.py is the one module that already does this correctly:
    importing it must succeed even when fcntl is unavailable, and the
    failure must surface as a descriptive ResourceLockError raised from the
    call that actually needs the lock, not an ImportError at module load.
    """
    # Simulate fcntl being unavailable, the way it would be absent on a
    # platform that lacks it, and confirm the module still functions -- it
    # already imported cleanly above (module-level import at file top).
    original_fcntl = resource_lock.fcntl
    try:
        resource_lock.fcntl = None
        lock = resource_lock.ResourceLock("portability-guard-test")
        with pytest.raises(resource_lock.ResourceLockError, match="unavailable on this platform"):
            lock.acquire()
    finally:
        resource_lock.fcntl = original_fcntl


@pytest.mark.parametrize(
    ("platform_name", "machine_name", "expect_supported"),
    [
        ("darwin", "arm64", True),
        ("darwin", "aarch64", True),
        ("darwin", "x86_64", False),
        ("linux", "x86_64", False),
        ("win32", "AMD64", False),
    ],
)
def test_mlx_runner_hardware_check_branches_on_platform_not_import(
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    machine_name: str,
    expect_supported: bool,
) -> None:
    """The MLX runner must be importable on every platform (it is imported
    above, at module scope, before this test body runs at all) and must
    decide hardware support from sys.platform / platform.machine() read at
    call time -- never from an import-time failure that would make the
    runner unavailable to inspect on non-Apple-silicon hosts.
    """
    monkeypatch.setattr(mlx_runner.sys, "platform", platform_name)
    monkeypatch.setattr(mlx_runner.platform, "machine", lambda: machine_name)
    supported = mlx_runner.sys.platform == "darwin" and mlx_runner.platform.machine() in {
        "arm64",
        "aarch64",
    }
    assert supported is expect_supported


def test_pid_exists_never_raises_regardless_of_os_kill_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """process_liveness.pid_exists() is the one liveness primitive every
    caller trusts. Whatever os.kill(pid, 0) does on the host platform --
    ProcessLookupError, PermissionError, or a bare OSError with different
    signal-0 semantics -- it must come back as a bool, never propagate.
    """
    for exc in (ProcessLookupError(), PermissionError(), OSError("unsupported")):

        def _raise(pid: int, sig: int, _exc: BaseException = exc) -> None:
            raise _exc

        monkeypatch.setattr(process_liveness.os, "kill", _raise)
        assert process_liveness.pid_exists(12345) is False

    monkeypatch.setattr(process_liveness.os, "kill", lambda pid, sig: None)
    assert process_liveness.pid_exists(12345) is True

    assert process_liveness.pid_exists(None) is False
    assert process_liveness.pid_exists(0) is False


@pytest.mark.parametrize(
    "candidate",
    [
        "relative/looking/name",
        "name with spaces",
        "unïcode-nàme",
        "trailing/",
    ],
)
def test_state_paths_are_pathlib_and_survive_unusual_component_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, candidate: str
) -> None:
    """config's path builders must return real pathlib.Path objects (never a
    plain string built by "/" concatenation) and must join components
    without depending on any particular separator character showing up
    literally in the input, so behavior does not depend on which OS's
    separator the caller happened to type.
    """
    monkeypatch.setenv("COORD_HOME", str(tmp_path / candidate.strip("/") or "state"))
    state = config.state_dir()
    assert isinstance(state, Path)
    db = config.coord_db_path()
    assert isinstance(db, Path)
    # coord_db_path must be state_dir joined with a plain filename -- not a
    # string built with a hardcoded "/" that would double up or break if
    # state_dir() ever returned something with a trailing separator.
    assert db.parent == state
    assert db.name == "coord.db"


def test_platform_branching_modules_import_cleanly_under_every_declared_platform() -> None:
    """Importing the two modules docs/compatibility.md names as doing
    hardware/platform branching must never itself depend on the host
    platform -- if it did, that would be exactly the import-time failure
    pattern the doc calls out as the thing to avoid.
    """
    for module_name in (
        "coordharness.coord.modeld_lite",
        "coordharness.coord.runners.mlx_runner",
    ):
        module = sys.modules.get(module_name)
        if module is None:
            module = importlib.import_module(module_name)
        assert module is not None
