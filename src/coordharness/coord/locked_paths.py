
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
from typing import Iterable

from coordharness import config as harness_config


DATA_LOCAL_ALIAS = Path(".coordharness/deployment-data")
DATA_LOCAL_TARGET = ".deployment-data"


def _git_blob_oid(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload).hexdigest()


EXPECTED_DATA_LOCAL_BLOB = _git_blob_oid(DATA_LOCAL_TARGET.encode("utf-8"))


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
    ):
        env.pop(key, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
        env=env,
    )


def _index_entry_findings(lines: Iterable[str]) -> list[str]:
    entries: list[tuple[str, str, str, str]] = []
    malformed: list[str] = []
    for raw in lines:
        line = str(raw).rstrip("\n")
        if not line:
            continue
        try:
            prefix, path = line.split("\t", 1)
            mode, oid, stage = prefix.split()
        except ValueError:
            malformed.append(line)
            continue
        entries.append((mode, oid, stage, path))
    found: list[str] = []
    if malformed:
        found.append(f"locked data-local index: malformed stage row(s): {malformed[:3]!r}")
    if len(entries) != 1:
        found.append(
            "locked data-local index: expected exactly one stage-0 entry for "
            f"{DATA_LOCAL_ALIAS}; found {len(entries)}"
        )
        return found
    mode, oid, stage, path = entries[0]
    if path != str(DATA_LOCAL_ALIAS):
        found.append(f"locked data-local index: unexpected path {path!r}")
    if stage != "0":
        found.append(f"locked data-local index: unresolved stage {stage}; expected stage 0")
    if mode != "120000":
        found.append(f"locked data-local index: mode {mode}; expected symlink mode 120000")
    if oid != EXPECTED_DATA_LOCAL_BLOB:
        found.append(
            f"locked data-local index: blob {oid}; expected exact target blob "
            f"{EXPECTED_DATA_LOCAL_BLOB}"
        )
    return found


def _effective_bool_config(repo_root: Path, key: str) -> tuple[bool | None, str | None]:
    proc = _run_git(repo_root, "config", "--bool", "--get", key)
    if proc.returncode == 1 and not (proc.stdout or "").strip():
        return None, None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        return None, detail
    value = (proc.stdout or "").strip().lower()
    if value in {"true", "yes", "on", "1"}:
        return True, None
    if value in {"false", "no", "off", "0"}:
        return False, None
    return None, f"unrecognized bool {value!r}"


def _git_dir(repo_root: Path) -> Path | None:
    marker = repo_root / ".git"
    if marker.is_dir():
        return marker
    if marker.is_file():
        try:
            line = marker.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if line.lower().startswith("gitdir:"):
            value = line.split(":", 1)[1].strip()
            path = Path(value)
            return path if path.is_absolute() else (repo_root / path).resolve()
    return None


def locked_data_local_findings(repo_root: Path | str) -> list[str]:
    root = Path(repo_root).resolve()
    alias = root / DATA_LOCAL_ALIAS
    physical = alias.parent / DATA_LOCAL_TARGET
    found: list[str] = []

    if not alias.is_symlink():
        kind = "real directory" if alias.is_dir() else "non-symlink path" if alias.exists() else "missing"
        found.append(
            f"locked data-local alias: {alias} is {kind}; expected symlink "
            f"to {DATA_LOCAL_TARGET}"
        )
    else:
        try:
            target = os.readlink(alias)
        except OSError as exc:
            found.append(f"locked data-local alias: cannot read {alias}: {exc}")
        else:
            if target != DATA_LOCAL_TARGET:
                found.append(
                    f"locked data-local alias: {alias} points to {target!r}; "
                    f"expected exact relative target {DATA_LOCAL_TARGET!r}"
                )

    if not physical.is_dir() or physical.is_symlink():
        found.append(f"locked data-local target: {physical} is not a physical directory")

    index = _run_git(root, "ls-files", "--stage", "--", str(DATA_LOCAL_ALIAS))
    if index.returncode != 0:
        detail = (index.stderr or index.stdout or f"exit {index.returncode}").strip()
        found.append(f"locked data-local index: cannot read index: {detail}")
    else:
        found.extend(_index_entry_findings((index.stdout or "").splitlines()))

    for key in ("core.sparseCheckout", "core.sparseCheckoutCone", "index.sparse"):
        enabled, error = _effective_bool_config(root, key)
        if error:
            found.append(f"locked data-local sparse guard: cannot read effective {key}: {error}")
        elif enabled:
            found.append(f"locked data-local sparse guard: effective {key}=true on primary repo")

    git_dir = _git_dir(root)
    if git_dir is None:
        found.append("locked data-local sparse guard: primary Git directory is not resolvable")
    else:
        sparse_spec = git_dir / "info" / "sparse-checkout"
        if sparse_spec.exists():
            found.append(
                f"locked data-local sparse guard: stale primary sparse spec exists at {sparse_spec}"
            )
    return found


def assert_locked_data_local(repo_root: Path | str) -> None:
    findings = locked_data_local_findings(repo_root)
    if findings:
        raise RuntimeError("; ".join(findings))


def assert_deployment_locked_data_local(repo_root: Path | str) -> None:
    """Apply the locked-layout guard only in the strict profile.

    The generic profile coordinates an arbitrary project. Strict deployments
    retain the fail-closed symlink and Git-index guard using neutral local
    paths; no product-specific warehouse topology is assumed.
    """
    if harness_config.is_strict_deployment():
        assert_locked_data_local(repo_root)


__all__ = [
    "DATA_LOCAL_ALIAS",
    "DATA_LOCAL_TARGET",
    "EXPECTED_DATA_LOCAL_BLOB",
    "assert_deployment_locked_data_local",
    "assert_locked_data_local",
    "locked_data_local_findings",
]
