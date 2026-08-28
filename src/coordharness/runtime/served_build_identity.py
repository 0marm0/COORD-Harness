
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
from typing import Callable, Mapping


SCHEMA_VERSION = "coordharness.served-build-identity.v1"
_GIT_OID = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class ServedBuildIdentity:
    commit: str | None
    tree: str | None
    source: str
    captured_at_utc: str
    exact_commit: bool
    checkout_dirty_at_boot: bool | None

    def as_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "served_commit": self.commit,
            "served_tree": self.tree,
            "identity_source": self.source,
            "captured_at_utc": self.captured_at_utc,
            "exact_commit": self.exact_commit,
            "checkout_dirty_at_boot": self.checkout_dirty_at_boot,
            "immutable_for_process_lifetime": True,
        }


def _git_identity(repo: Path) -> tuple[str, str, bool]:
    commit = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
    ).stdout.strip().lower()
    tree = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{tree}"],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
    ).stdout.strip().lower()
    if _GIT_OID.fullmatch(commit) is None or _GIT_OID.fullmatch(tree) is None:
        raise RuntimeError("git returned a non-SHA-1 build identity")
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        ).stdout
    )
    return commit, tree, dirty


def capture_served_build_identity(
    *,
    environ: Mapping[str, str] | None = None,
    repo: Path | None = None,
    git_identity: Callable[[Path], tuple[str, str, bool]] = _git_identity,
    captured_at_utc: str | None = None,
) -> ServedBuildIdentity:

    env = os.environ if environ is None else environ
    captured = captured_at_utc or datetime.now(timezone.utc).isoformat()
    release_commit = str(env.get("COORD_RELEASE_COMMIT") or "").strip().lower()
    release_tree = str(env.get("COORD_RELEASE_TREE") or "").strip().lower()
    integrity_required = env.get("COORD_RELEASE_INTEGRITY_REQUIRED") == "1"
    if release_commit or release_tree or integrity_required:
        if (
            _GIT_OID.fullmatch(release_commit) is None
            or _GIT_OID.fullmatch(release_tree) is None
        ):
            if integrity_required:
                raise RuntimeError("release build identity pins are missing or malformed")
            return ServedBuildIdentity(
                None,
                None,
                "invalid_release_env",
                captured,
                False,
                None,
            )
        return ServedBuildIdentity(
            release_commit,
            release_tree,
            "release_env",
            captured,
            True,
            False,
        )

    from coordharness import config as _harness_config

    checkout = repo or _harness_config.project_root()
    try:
        commit, tree, dirty = git_identity(checkout)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return ServedBuildIdentity(None, None, "unavailable", captured, False, None)
    return ServedBuildIdentity(
        commit,
        tree,
        "checkout_head_at_boot",
        captured,
        not dirty,
        dirty,
    )


def compare_served_build(
    expected_commit: str,
    observed: Mapping[str, object],
) -> dict[str, object]:

    expected = expected_commit.strip().lower()
    observed_commit = str(observed.get("served_commit") or "").strip().lower()
    schema = observed.get("schema_version")
    if _GIT_OID.fullmatch(expected) is None:
        return {
            "verdict": "UNKNOWN_EXPECTED_BUILD",
            "expected_commit": expected or None,
            "observed_commit": observed_commit or None,
        }
    if (
        schema != SCHEMA_VERSION
        or _GIT_OID.fullmatch(observed_commit) is None
        or observed.get("exact_commit") is not True
    ):
        return {
            "verdict": "UNKNOWN_SERVED_BUILD",
            "expected_commit": expected,
            "observed_commit": observed_commit or None,
        }
    return {
        "verdict": "MATCHED" if observed_commit == expected else "STALE_BUILD",
        "expected_commit": expected,
        "observed_commit": observed_commit,
    }


SERVED_BUILD_IDENTITY = capture_served_build_identity()
