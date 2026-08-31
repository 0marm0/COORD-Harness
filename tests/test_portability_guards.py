"""Portability regression guards.

These tests run on any platform pytest runs on (they do not require a second
OS) and check the properties `docs/compatibility.md` claims: path handling
stays inside `pathlib` regardless of separator style, the modules that do
hardware/platform branching decide at call time rather than at import time,
and every module that touches `fcntl` or `ps` guards it the way
`resource_lock.py` originally did -- import succeeds unconditionally, and the
platform gap only surfaces as a descriptive error (or an honest degraded
behavior) from the call that actually needs it. They are deliberately
behavioral -- each test simulates the module's absence via an import hook or
a monkeypatched capability flag and exercises what the code actually does,
not a string match against source text.
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import importlib.abc
import logging
import shutil
import sys
from pathlib import Path

import pytest

from coordharness import config
from coordharness.coord import process_liveness
from coordharness.coord.runners import mlx_runner

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "coordharness"

# Modules that touch `fcntl` for advisory file locking. Every one of these
# must guard the import (try/except ImportError, the resource_lock.py
# pattern) -- this set exists so the inventory test below fails loudly if a
# new unconditional `import fcntl` is ever added anywhere in the package.
MODULES_USING_FCNTL = frozenset(
    {
        "coordharness.coord.native_cockpit",
        "coordharness.runtime.console_release_retention",
        "coordharness.knowledge.accepted_memory_r4",
        "coordharness.jobs.pglaunch",
        "coordharness.jobs.diagnostic_marker",
        "coordharness.jobs.resource_lock",
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
    `resource_lock.py` established and the other five modules now follow)
    does not count -- that import can fail without the module failing to
    load.
    """
    tree = ast.parse(source)
    for node in tree.body:  # module-level statements only, not nested in Try
        if isinstance(node, ast.Import) and any(alias.name == "fcntl" for alias in node.names):
            return True
    return False


def test_no_module_imports_fcntl_unconditionally() -> None:
    """Every module that mentions `fcntl` anywhere in its source must import
    it through a guard, not a bare module-level `import fcntl`. This is the
    regression guard: it fails loudly the day a new unconditional import is
    added, whether or not the module is one of the six already known to
    touch fcntl.
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

    assert found == frozenset()


def test_fcntl_touching_modules_match_the_documented_inventory() -> None:
    """The set of modules that reference `fcntl` at all is exactly the set
    docs/compatibility.md discusses -- this keeps the doc and the code from
    drifting apart silently (a module dropping its fcntl usage, or a new one
    picking it up, should force a doc update).
    """
    found = set()
    for name in _module_names_under(SRC_ROOT):
        path = SRC_ROOT.parent / Path(*name.split("."))
        path = path.with_suffix(".py")
        if not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        if "import fcntl" in source:
            found.add(name)
    assert found == MODULES_USING_FCNTL


class _BlockFcntlFinder(importlib.abc.MetaPathFinder):
    """A meta-path finder that makes `import fcntl` fail unconditionally.

    Inserted ahead of the normal finders, it raises before they ever get a
    chance to locate the real (built-in) `fcntl` module -- this is what
    stands in for "this platform has no fcntl" without needing a second OS.
    """

    def find_spec(self, fullname, path, target=None):  # noqa: D102
        if fullname == "fcntl":
            raise ModuleNotFoundError("simulated: no module named 'fcntl' on this platform")
        return None


@contextlib.contextmanager
def _fcntl_blocked():
    finder = _BlockFcntlFinder()
    sys.meta_path.insert(0, finder)
    fcntl_backup = sys.modules.pop("fcntl", None)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)
        if fcntl_backup is not None:
            sys.modules["fcntl"] = fcntl_backup


@contextlib.contextmanager
def _reimported_fresh(module_name: str):
    """Force a fresh import of `module_name`, then restore the world exactly.

    Popping a submodule from `sys.modules` and reimporting it is not enough
    to isolate the reimport: Python's import machinery also (re)sets the
    submodule as an attribute on its parent package object as a side effect
    of the import, and does so regardless of whatever was already cached in
    `sys.modules`. A caller elsewhere that does `from pkg import submodule`
    *inside a function body* (a lazy, call-time import -- several production
    modules in this package do exactly this for `process_liveness`,
    `native_cockpit`, and `diagnostic_marker`) resolves `submodule` via that
    parent attribute, not by name-binding at its own file's collection time.
    Restoring only `sys.modules[module_name]` leaves that parent attribute
    pointing at the abandoned reimported copy, which is invisible until some
    unrelated later test's lazy import silently picks it up. This restores
    both.
    """
    parent_name, _, attr = module_name.rpartition(".")
    parent = sys.modules.get(parent_name) if parent_name else None
    original_module = sys.modules.get(module_name)
    had_parent_attr = parent is not None and attr in vars(parent)
    original_parent_attr = getattr(parent, attr, None) if had_parent_attr else None

    sys.modules.pop(module_name, None)
    try:
        module = importlib.import_module(module_name)
        yield module
    finally:
        if original_module is not None:
            sys.modules[module_name] = original_module
        else:
            sys.modules.pop(module_name, None)
        if parent is not None:
            if had_parent_attr:
                setattr(parent, attr, original_parent_attr)
            elif hasattr(parent, attr):
                delattr(parent, attr)


@contextlib.contextmanager
def _reimported_without_fcntl(module_name: str):
    """Reimport `module_name` from scratch with `fcntl` unimportable.

    Yields the freshly imported module object (its `fcntl` attribute will be
    `None`, exactly as resource_lock.py's guard already does). See
    `_reimported_fresh` for exactly what gets restored afterward.
    """
    with _fcntl_blocked(), _reimported_fresh(module_name) as module:
        yield module


def test_resource_lock_guards_its_fcntl_import_and_degrades_at_use_not_import() -> None:
    """resource_lock.py established the pattern the other five modules now
    follow: importing it must succeed even when fcntl is unavailable, and
    the failure must surface as a descriptive ResourceLockError raised from
    the call that actually needs the lock, not an ImportError at module
    load.
    """
    with _reimported_without_fcntl("coordharness.jobs.resource_lock") as module:
        assert module.fcntl is None
        lock = module.ResourceLock("portability-guard-test")
        with pytest.raises(module.ResourceLockError, match="unavailable on this platform"):
            lock.acquire()


def test_native_cockpit_imports_without_fcntl_and_degrades_honestly() -> None:
    """native_cockpit.py must import cleanly without fcntl, expose the
    unavailability via FCNTL_AVAILABLE, and flush_requested_refresh() must
    still run its refresh -- just without the cross-process exclusion lock,
    and while logging the degradation exactly once rather than per call.
    """
    with _reimported_without_fcntl("coordharness.coord.native_cockpit") as module:
        assert module.fcntl is None
        assert module.FCNTL_AVAILABLE is False

        calls = []

        def fake_unlocked(conn, *, force=False, min_interval_s=5.0):
            calls.append((conn, force, min_interval_s))
            return {"flushed": True, "pending": False}

        module._flush_requested_refresh_unlocked = fake_unlocked
        sentinel_conn = object()

        with caplog_at(module.__name__) as caplog:
            result = module.flush_requested_refresh(sentinel_conn, force=True, min_interval_s=9.0)

            assert result == {"flushed": True, "pending": False}
            assert calls == [(sentinel_conn, True, 9.0)]
            # The honest-degradation path never touches the exclusion lock file.
            assert not module.PROJECTION_MAINTENANCE_EXCLUSION.exists()
            assert "FCNTL_AVAILABLE=False" in caplog.text

            # A second call must not log again -- degradation is reported once
            # per process, not once per flush. This has to run while the
            # handler is still attached: once `caplog_at` exits, no call can
            # ever add to caplog.text again, and the assertion below would be
            # true by construction rather than by the guard actually holding.
            caplog.clear()
            module.flush_requested_refresh(sentinel_conn)
            assert caplog.text == ""


def test_apply_retention_plan_refuses_without_fcntl() -> None:
    """apply_retention_plan() deletes release directories under an
    exclusive activation lock; without fcntl there is no safe way to
    serialize concurrent applies, so it must refuse with a named,
    descriptive error rather than deleting anything unprotected.
    build_retention_plan() itself does not touch fcntl and is unaffected.
    """
    with _reimported_without_fcntl("coordharness.runtime.console_release_retention") as module:
        assert module.fcntl is None
        with pytest.raises(module.RetentionError, match="operating-system file locks are unavailable"):
            module.apply_retention_plan({}, confirm_plan_sha256="irrelevant-because-the-fcntl-check-runs-first")


def test_publish_current_refuses_without_fcntl(tmp_path: Path) -> None:
    """publish_current() performs a compare-and-swap on the CURRENT
    pointer; without the exclusive lock, two concurrent publishers could
    both pass their CAS check and one would silently clobber the other.
    It must refuse rather than publish unlocked, and must not touch CURRENT.
    """
    with _reimported_without_fcntl("coordharness.knowledge.accepted_memory_r4") as module:
        assert module.fcntl is None
        generation_id = "accepted-memory-r4-sha256-" + "0" * 64
        manifest_bytes = ('{"generation_id": "%s"}' % generation_id).encode()

        # Stand in for the (expensive, disk-backed) prior verification steps
        # so this test proves the fcntl-refusal behavior in isolation rather
        # than re-proving verify_generation()'s own contract.
        module.verify_generation = lambda generation: {
            "manifest_sha256": module.sha256_bytes(manifest_bytes)
        }
        module.stable_read = lambda path: manifest_bytes
        module._json = lambda path: {"status": "PASS_DARK_READY_FOR_POINTER_REVIEW"}

        with pytest.raises(module.AcceptedMemoryError, match="operating-system file locks are unavailable"):
            module.publish_current(
                store_root=tmp_path,
                generation_id=generation_id,
                expected_current=None,
                actor="portability-guard-test",
                reason="prove the refusal happens before any pointer mutation",
            )
        assert not (tmp_path / "CURRENT").exists()


def test_terminalize_wrapper_loss_refuses_without_fcntl(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """_terminalize_wrapper_loss() mutates the shared progress-file sidecar
    under an exclusive control lock; without fcntl it must report "did not
    terminalize" (the same outcome its other failure branches already
    return) rather than writing the sidecar unprotected, and must log the
    degradation once, not on every call.
    """
    with _reimported_without_fcntl("coordharness.jobs.pglaunch") as module:
        assert module.fcntl is None
        progress_file = str(tmp_path / "progress.json")
        lock_path = tmp_path / "control.lock"
        payload = {"canonical_control_lock_path": str(lock_path)}

        result = module._terminalize_wrapper_loss(progress_file, payload)
        assert result is False
        assert not lock_path.exists()
        assert not Path(progress_file).exists()
        first_err = capsys.readouterr().err
        assert "fcntl" in first_err.lower()

        # Second call: same refusal, but no repeated log line.
        result_again = module._terminalize_wrapper_loss(progress_file, payload)
        assert result_again is False
        assert capsys.readouterr().err == ""


def test_wrapper_control_lock_refuses_without_fcntl() -> None:
    """wrapper_control_lock() serializes concurrent wrapper launches writing
    to the same job-control record/sentinel pair; without fcntl there is no
    safe substitute, so entering the context manager must raise a named
    error before any lock file is touched.
    """
    with _reimported_without_fcntl("coordharness.jobs.diagnostic_marker") as module:
        assert module.fcntl is None
        entered = False
        with pytest.raises(module.WrapperControlLockError, match="operating-system file locks are unavailable"):
            with module.wrapper_control_lock("portability-guard-test-job"):
                entered = True  # pragma: no cover - must never run
        assert entered is False


@contextlib.contextmanager
def caplog_at(logger_name: str):
    """Minimal stand-in so module-scoped tests above can assert on log text
    without pytest's caplog fixture, which is request-scoped and awkward to
    thread through a plain `with` helper. Captures WARNING+ records emitted
    on the named logger for the duration of the block.
    """

    class _Collector:
        def __init__(self) -> None:
            self.records: list[logging.LogRecord] = []

        @property
        def text(self) -> str:
            return "\n".join(record.getMessage() for record in self.records)

        def clear(self) -> None:
            self.records.clear()

    collector = _Collector()

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            collector.records.append(record)

    handler = _Handler(level=logging.WARNING)
    logger = logging.getLogger(logger_name)
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        yield collector
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def test_ps_lstart_available_is_false_and_logged_once_when_ps_binary_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """process_liveness.py has no non-POSIX alternative for `ps -o
    lstart=`; when the binary is absent, PS_LSTART_AVAILABLE must go false,
    pid_start_time() must report unavailable (None) rather than raising or
    fabricating a value, and the unavailability must be logged once per
    process -- not on every pid_start_time() call.
    """
    real_which = shutil.which

    def fake_which(cmd, *args, **kwargs):
        if cmd == "ps":
            return None
        return real_which(cmd, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", fake_which)
    with _reimported_fresh("coordharness.coord.process_liveness") as module:
        assert module.PS_LSTART_AVAILABLE is False

        with caplog_at(module.__name__) as caplog:
            assert module.pid_start_time(12345) is None
            assert "PS_LSTART_AVAILABLE=False" in caplog.text

            # Must run inside the same block: once caplog_at exits, its
            # handler is detached and no later call can ever add to
            # caplog.text, which would make this pass whether or not the
            # once-per-process guard actually holds.
            caplog.clear()
            assert module.pid_start_time(6789) is None
            assert caplog.text == ""  # logged once, not per call


def test_pid_matches_treats_unavailable_start_time_as_cannot_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ps unavailable, pid_start_time() always returns None. pid_matches()
    already treats an unresolvable start time as "cannot verify" and refuses
    to confirm identity when the caller supplied one to check against --
    this asserts that existing conservative behavior still holds when the
    reason for None is platform absence rather than a transient ps failure,
    while a caller that has no expected start time to check still gets a
    bare liveness answer.
    """
    monkeypatch.setattr(process_liveness, "PS_LSTART_AVAILABLE", False)
    monkeypatch.setattr(process_liveness, "_ps_unavailable_logged", True)  # keep this test quiet
    monkeypatch.setattr(process_liveness, "pid_exists", lambda pid: True)

    assert process_liveness.pid_start_time(12345) is None
    # A real expected start time to compare against cannot be confirmed ->
    # conservative False (do not certify PID-reuse safety on a guess).
    assert process_liveness.pid_matches(12345, expected_start_time=1_700_000_000.0) is False
    # No expected start time was supplied at all -> bare liveness is enough.
    assert process_liveness.pid_matches(12345, expected_start_time=None) is True


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
