#!/usr/bin/env python3

from __future__ import annotations

import argparse
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
from typing import Any

from coordharness import config as _harness_config

ROOT = _harness_config.project_root()
ARCHIVE_PREFIX = os.environ.get("COORD_DOC_ARCHIVE_PREFIX", "docs/archive/")
PROTECTED_DOC_PREFIX = os.environ.get("COORD_DOC_PROTECTED_PREFIX", "docs/")
ROOT_PROTECTED = set(
    filter(None, os.environ.get("COORD_DOC_ROOT_PROTECTED", "").split(","))
)


def is_protected_markdown(path: str) -> bool:
    normalized = PurePosixPath(path).as_posix().lstrip("./")
    return (
        normalized in ROOT_PROTECTED
        or (normalized.startswith(PROTECTED_DOC_PREFIX) and normalized.endswith(".md"))
    )


def is_archive_path(path: str) -> bool:
    normalized = PurePosixPath(path).as_posix().lstrip("./")
    return normalized.startswith(ARCHIVE_PREFIX) and normalized.endswith(".md")


def _git(repo: Path, *args: str, check: bool = True) -> bytes:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        error = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {error}")
    return proc.stdout


def _parse_name_status_z(payload: bytes) -> list[dict[str, str]]:
    fields = payload.decode("utf-8", errors="surrogateescape").split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    changes: list[dict[str, str]] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if not status:
            continue
        code = status[:1]
        if code in {"R", "C"}:
            if index + 1 >= len(fields):
                raise ValueError("truncated Git rename/copy record")
            source, destination = fields[index], fields[index + 1]
            index += 2
            changes.append({
                "status": status,
                "code": code,
                "source": source,
                "destination": destination,
            })
            continue
        if index >= len(fields):
            raise ValueError("truncated Git name-status record")
        path = fields[index]
        index += 1
        changes.append({"status": status, "code": code, "path": path})
    return changes


def _diff_changes(repo: Path, *, base_ref: str, target_ref: str | None) -> list[dict[str, str]]:
    args = ["diff", "--name-status", "-z", "--find-renames=50%", base_ref]
    if target_ref:
        args.append(target_ref)
    args.append("--")
    return _parse_name_status_z(_git(repo, *args))


def _markdown_paths(repo: Path, *, target_ref: str | None) -> list[str]:
    if target_ref:
        payload = _git(repo, "ls-tree", "-r", "-z", "--name-only", target_ref, "--")
        paths = payload.decode("utf-8", errors="surrogateescape").split("\0")
        return sorted({path for path in paths if path.endswith(".md")})
    payload = _git(repo, "ls-files", "-z", "-c", "-o", "--exclude-standard", "--", "*.md")
    paths = payload.decode("utf-8", errors="surrogateescape").split("\0")
    return sorted({path for path in paths if path.endswith(".md") and (repo / path).is_file()})


def _content(repo: Path, path: str, *, ref: str | None) -> bytes | None:
    if ref is None:
        candidate = repo / path
        try:
            return candidate.read_bytes()
        except OSError:
            return None
    proc = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=repo,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return proc.stdout if proc.returncode == 0 else None


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _line_similarity(left: bytes, right: bytes) -> float:
    left_lines = left.decode("utf-8", errors="replace").splitlines()
    right_lines = right.decode("utf-8", errors="replace").splitlines()
    return float(SequenceMatcher(None, left_lines, right_lines, autojunk=False).ratio())


def _recovery_commit(repo: Path, *, base_ref: str, path: str) -> str | None:
    value = _git(repo, "log", "-1", "--format=%H", base_ref, "--", path, check=False)
    commit = value.decode("utf-8", errors="replace").strip()
    return commit or None


def build_audit(
    repo: Path = ROOT,
    *,
    base_ref: str = "HEAD",
    target_ref: str | None = None,
) -> dict[str, Any]:
    repo = repo.resolve()
    changes = _diff_changes(repo, base_ref=base_ref, target_ref=target_ref)
    target_content_ref = target_ref

    digest_paths: dict[str, list[str]] = {}
    path_payloads: dict[str, bytes] = {}
    for path in _markdown_paths(repo, target_ref=target_ref):
        payload = _content(repo, path, ref=target_content_ref)
        if payload is None:
            continue
        path_payloads[path] = payload
        digest_paths.setdefault(_sha256(payload), []).append(path)

    decisions: list[dict[str, Any]] = []
    for change in changes:
        code = change["code"]
        if code in {"R", "C"}:
            source = change["source"]
            if code != "R" or not is_protected_markdown(source):
                continue
            destination = change["destination"]
            disposition = "safe_archive_move" if is_archive_path(destination) else "relocated_non_archive"
            decisions.append({
                "source": source,
                "disposition": disposition,
                "surviving_paths": [destination],
                "git_status": change["status"],
                "recovery_commit": _recovery_commit(repo, base_ref=base_ref, path=source),
            })
            continue
        if code != "D":
            continue
        source = change["path"]
        if not is_protected_markdown(source):
            continue
        source_payload = _content(repo, source, ref=base_ref)
        source_digest = _sha256(source_payload) if source_payload is not None else None
        surviving_paths = sorted(digest_paths.get(source_digest or "", []))
        similarity: float | None = 1.0 if surviving_paths else None
        if not surviving_paths and source_payload is not None:
            same_basename = [
                path for path in path_payloads
                if PurePosixPath(path).name == PurePosixPath(source).name
            ]
            scored = sorted(
                (
                    (_line_similarity(source_payload, path_payloads[path]), path)
                    for path in same_basename
                ),
                reverse=True,
            )
            if scored and scored[0][0] >= 0.85:
                similarity = round(scored[0][0], 6)
                surviving_paths = sorted(path for score, path in scored if score >= 0.85)
        archive_paths = [path for path in surviving_paths if is_archive_path(path)]
        if archive_paths:
            disposition = "safe_archive_copy"
            surviving_paths = archive_paths
        elif surviving_paths:
            disposition = "relocated_non_archive"
        else:
            disposition = "unpreserved_deletion"
        decisions.append({
            "source": source,
            "disposition": disposition,
            "surviving_paths": surviving_paths,
            "git_status": change["status"],
            "source_sha256": source_digest,
            "best_line_similarity": similarity,
            "recovery_commit": _recovery_commit(repo, base_ref=base_ref, path=source),
        })

    findings = [
        decision
        for decision in decisions
        if decision["disposition"] in {"relocated_non_archive", "unpreserved_deletion"}
    ]
    return {
        "schema": "coordharness.doc-deletion-guard.v1",
        "read_only": True,
        "repo": str(repo),
        "base_ref": base_ref,
        "target": target_ref or "WORKTREE",
        "status": "WARN" if findings else "PASS",
        "protected_deletions": len(decisions),
        "finding_count": len(findings),
        "decisions": decisions,
        "findings": findings,
        "remediation": (
            "Restore the source from recovery_commit, extract unique content, then git mv it into "
            f"{ARCHIVE_PREFIX} with path-alias/referrer updates; do not hard-delete it."
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--base-ref", default="HEAD")
    parser.add_argument("--target-ref")
    parser.add_argument("--fail-on-findings", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    audit = build_audit(args.repo_root, base_ref=args.base_ref, target_ref=args.target_ref)
    print(json.dumps(audit, indent=2 if args.pretty else None, sort_keys=True))
    return 1 if args.fail_on_findings and audit["finding_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
