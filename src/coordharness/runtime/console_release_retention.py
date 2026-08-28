
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Mapping


SCHEMA_PLAN = "coordharness.console-release-retention-plan.v1"
SCHEMA_RECEIPT = "coordharness.console-release-retention-receipt.v1"
COMPLETE_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
PARTIAL_RE = re.compile(
    r"^[0-9a-f]{40}(?:[0-9a-f]{24})?\.partial(?:\.blocked-[0-9]+)?$"
)
DEFAULT_PARTIAL_MIN_AGE_S = 6 * 60 * 60
DEFAULT_KEEP_UNREFERENCED_COMPLETE = 2
MAX_COMPLETE_BEFORE_PREPARE = 4


class RetentionError(RuntimeError):
    pass


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _payload_sha256(value: Mapping[str, Any], field: str) -> str:
    payload = {key: item for key, item in value.items() if key != field}
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _runtime_and_releases(runtime_root: Path) -> tuple[Path, Path]:
    if runtime_root.is_symlink():
        raise RetentionError("runtime root must not be a symlink")
    runtime = runtime_root.resolve(strict=True)
    releases = runtime / "releases"
    if releases.is_symlink() or not releases.is_dir():
        raise RetentionError("release root must be a real directory")
    return runtime, releases.resolve(strict=True)


def _pointer_target(runtime: Path, releases: Path, name: str) -> Path:
    pointer = runtime / name
    if not pointer.is_symlink():
        raise RetentionError(f"{name} must be a symlink")
    target = pointer.resolve(strict=True)
    if target.parent != releases or not COMPLETE_RE.fullmatch(target.name):
        raise RetentionError(f"{name} escapes the content-addressed release root")
    if target.is_symlink() or not target.is_dir():
        raise RetentionError(f"{name} target is not a real release directory")
    return target


def _allocated_bytes(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        stat_result = current.lstat()
        total += int(getattr(stat_result, "st_blocks", 0)) * 512
        if current.is_dir() and not current.is_symlink():
            with os.scandir(current) as entries:
                stack.extend(Path(entry.path) for entry in entries)
    return total


def _external_json_references(runtime: Path, releases: Path) -> dict[str, list[str]]:
    references: dict[str, list[str]] = {}
    for path in sorted(runtime.rglob("*.json")):
        try:
            path.resolve(strict=False).relative_to(releases)
            continue
        except ValueError:
            pass
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in set(re.findall(r"[0-9a-f]{40}(?:[0-9a-f]{24})?(?:\.partial(?:\.blocked-[0-9]+)?)?", text)):
            references.setdefault(match, []).append(path.relative_to(runtime).as_posix())
    return references


def _entry(path: Path, now: float, references: Mapping[str, list[str]]) -> dict[str, Any]:
    stat_result = path.lstat()
    if path.is_symlink() or not path.is_dir():
        raise RetentionError(f"release entry must be a real directory: {path}")
    kind = (
        "complete"
        if COMPLETE_RE.fullmatch(path.name)
        else "partial"
        if PARTIAL_RE.fullmatch(path.name)
        else "unknown"
    )
    return {
        "name": path.name,
        "kind": kind,
        "path": str(path),
        "realpath": str(path.resolve(strict=True)),
        "device": stat_result.st_dev,
        "inode": stat_result.st_ino,
        "mtime_ns": stat_result.st_mtime_ns,
        "age_s": max(0, int(now - stat_result.st_mtime)),
        "allocated_bytes": _allocated_bytes(path),
        "external_json_references": sorted(references.get(path.name, [])),
    }


def build_retention_plan(
    runtime_root: Path,
    *,
    partial_min_age_s: int = DEFAULT_PARTIAL_MIN_AGE_S,
    keep_unreferenced_complete: int = DEFAULT_KEEP_UNREFERENCED_COMPLETE,
    now: float | None = None,
) -> dict[str, Any]:

    if partial_min_age_s < 60:
        raise RetentionError("partial_min_age_s must be at least 60")
    if keep_unreferenced_complete < 0:
        raise RetentionError("keep_unreferenced_complete must be nonnegative")
    now = time.time() if now is None else float(now)
    runtime, releases = _runtime_and_releases(runtime_root)
    current = _pointer_target(runtime, releases, "current")
    previous = _pointer_target(runtime, releases, "previous")
    protected_names = {current.name, previous.name}
    references = _external_json_references(runtime, releases)

    entries = [
        _entry(path, now, references)
        for path in sorted(releases.iterdir(), key=lambda item: item.name)
        if path.name != ".DS_Store"
    ]
    unknown = [entry["name"] for entry in entries if entry["kind"] == "unknown"]
    if unknown:
        raise RetentionError(f"unknown release-root entries: {unknown}")

    partial_delete = [
        entry
        for entry in entries
        if entry["kind"] == "partial"
        and entry["age_s"] >= partial_min_age_s
        and not entry["external_json_references"]
    ]
    rollback_complete = sorted(
        (
            entry
            for entry in entries
            if entry["kind"] == "complete"
            and entry["name"] not in protected_names
        ),
        key=lambda entry: (entry["mtime_ns"], entry["name"]),
        reverse=True,
    )
    retained_rollback = rollback_complete[:keep_unreferenced_complete]
    complete_delete = rollback_complete[keep_unreferenced_complete:]
    delete_entries = sorted(
        [*partial_delete, *complete_delete],
        key=lambda entry: (entry["kind"], entry["mtime_ns"], entry["name"]),
    )
    delete_names = {entry["name"] for entry in delete_entries}
    keep_entries = [entry for entry in entries if entry["name"] not in delete_names]

    plan: dict[str, Any] = {
        "schema": SCHEMA_PLAN,
        "created_at_epoch_s": now,
        "runtime_root": str(runtime),
        "releases_root": str(releases),
        "pointer_snapshot": {
            "current": current.name,
            "previous": previous.name,
        },
        "policy": {
            "partial_min_age_s": partial_min_age_s,
            "keep_unreferenced_complete": keep_unreferenced_complete,
            "max_complete_before_prepare": MAX_COMPLETE_BEFORE_PREPARE,
            "partial_limit_before_prepare": 0,
        },
        "inventory": {
            "entry_count": len(entries),
            "complete_count": sum(entry["kind"] == "complete" for entry in entries),
            "partial_count": sum(entry["kind"] == "partial" for entry in entries),
            "allocated_bytes": sum(entry["allocated_bytes"] for entry in entries),
        },
        "protected": sorted(protected_names),
        "retained_rollback": [entry["name"] for entry in retained_rollback],
        "delete": delete_entries,
        "keep": keep_entries,
        "planned_reclaim_bytes": sum(entry["allocated_bytes"] for entry in delete_entries),
        "authority": "EXACT_PLAN_NO_MUTATION",
        "plan_sha256": "",
    }
    plan["plan_sha256"] = _payload_sha256(plan, "plan_sha256")
    return plan


def assert_prepare_capacity(runtime_root: Path) -> None:

    runtime_root = runtime_root.resolve(strict=False)
    releases = runtime_root / "releases"
    if not releases.exists():
        return
    _runtime, resolved_releases = _runtime_and_releases(runtime_root)
    names = [path.name for path in resolved_releases.iterdir() if path.name != ".DS_Store"]
    unknown = [
        name
        for name in names
        if not COMPLETE_RE.fullmatch(name) and not PARTIAL_RE.fullmatch(name)
    ]
    partials = [name for name in names if PARTIAL_RE.fullmatch(name)]
    completes = [name for name in names if COMPLETE_RE.fullmatch(name)]
    if unknown:
        raise RetentionError(f"unknown release-root entries block preparation: {unknown}")
    if partials:
        raise RetentionError(
            "existing partial release(s) block another materialization; "
            "run the retention planner: " + ", ".join(sorted(partials))
        )
    if len(completes) > MAX_COMPLETE_BEFORE_PREPARE:
        raise RetentionError(
            f"{len(completes)} complete releases exceed the "
            f"{MAX_COMPLETE_BEFORE_PREPARE}-release pre-materialization bound"
        )


def _open_handle_paths(releases: Path) -> set[str]:
    try:
        proc = subprocess.run(
            ["lsof", "-nP"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RetentionError(f"cannot prove open-handle safety: {exc}") from exc
    if proc.returncode not in {0, 1}:
        raise RetentionError(f"lsof failed with exit {proc.returncode}")
    prefix = str(releases) + os.sep
    paths: set[str] = set()
    for line in proc.stdout.splitlines():
        if prefix not in line:
            continue
        paths.add(line[line.index(prefix) :].strip())
    return paths


def apply_retention_plan(
    plan: Mapping[str, Any],
    *,
    confirm_plan_sha256: str,
) -> dict[str, Any]:

    plan = dict(plan)
    if plan.get("schema") != SCHEMA_PLAN:
        raise RetentionError("retention plan schema mismatch")
    observed_sha = _payload_sha256(plan, "plan_sha256")
    if plan.get("plan_sha256") != observed_sha or confirm_plan_sha256 != observed_sha:
        raise RetentionError("retention plan hash mismatch")
    runtime, releases = _runtime_and_releases(Path(str(plan["runtime_root"])))
    lock_path = runtime / "activation.lock"
    lock_path.touch(exist_ok=True)

    with lock_path.open("a+b") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RetentionError("activation lock is held") from exc

        current = _pointer_target(runtime, releases, "current")
        previous = _pointer_target(runtime, releases, "previous")
        if plan["pointer_snapshot"] != {
            "current": current.name,
            "previous": previous.name,
        }:
            raise RetentionError("current/previous pointer snapshot changed")

        protected = {current.name, previous.name}
        references = _external_json_references(runtime, releases)
        open_paths = _open_handle_paths(releases)
        targets: list[tuple[Path, Mapping[str, Any]]] = []
        for record in plan["delete"]:
            name = str(record["name"])
            if name in protected:
                raise RetentionError(f"plan attempts to delete protected release {name}")
            path = releases / name
            if path.is_symlink() or not path.is_dir() or path.resolve(strict=True).parent != releases:
                raise RetentionError(f"delete target is no longer an exact real directory: {name}")
            stat_result = path.lstat()
            identity = (stat_result.st_dev, stat_result.st_ino, stat_result.st_mtime_ns)
            expected = (record["device"], record["inode"], record["mtime_ns"])
            if identity != expected or str(path.resolve(strict=True)) != record["realpath"]:
                raise RetentionError(f"delete target identity changed: {name}")
            observed_references = sorted(references.get(name, []))
            if observed_references != record["external_json_references"]:
                raise RetentionError(f"delete target reference set changed: {name}")
            if record["kind"] == "partial" and observed_references:
                raise RetentionError(f"partial delete target is externally referenced: {name}")
            prefix = str(path.resolve(strict=True)) + os.sep
            if any(open_path == str(path) or open_path.startswith(prefix) for open_path in open_paths):
                raise RetentionError(f"delete target has an open process handle: {name}")
            targets.append((path, record))

        removed: list[dict[str, Any]] = []
        for path, record in targets:
            quarantine = releases / f".retention-delete-{uuid.uuid4().hex}-{path.name}"
            os.replace(path, quarantine)
            shutil.rmtree(quarantine)
            removed.append(
                {
                    "name": record["name"],
                    "kind": record["kind"],
                    "allocated_bytes": record["allocated_bytes"],
                }
            )

        post_current = _pointer_target(runtime, releases, "current")
        post_previous = _pointer_target(runtime, releases, "previous")
        remaining = [
            path.name
            for path in releases.iterdir()
            if path.name != ".DS_Store"
        ]
        receipt: dict[str, Any] = {
            "schema": SCHEMA_RECEIPT,
            "plan_sha256": observed_sha,
            "completed_at_epoch_s": time.time(),
            "runtime_root": str(runtime),
            "pointer_snapshot_after": {
                "current": post_current.name,
                "previous": post_previous.name,
            },
            "removed": removed,
            "removed_count": len(removed),
            "reclaimed_allocated_bytes_estimate": sum(
                int(record["allocated_bytes"]) for record in removed
            ),
            "remaining_entry_count": len(remaining),
            "remaining_complete_count": sum(
                bool(COMPLETE_RE.fullmatch(name)) for name in remaining
            ),
            "remaining_partial_count": sum(
                bool(PARTIAL_RE.fullmatch(name)) for name in remaining
            ),
            "authority": "APPLIED_EXACT_PLAN_POINTERS_UNCHANGED",
            "receipt_sha256": "",
        }
        receipt["receipt_sha256"] = _payload_sha256(receipt, "receipt_sha256")
        return receipt
