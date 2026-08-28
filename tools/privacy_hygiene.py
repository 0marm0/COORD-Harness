#!/usr/bin/env python3
"""Fail closed when a public candidate contains private vocabulary or host paths.

The repository stores only SHA-256 digests of forbidden normalized phrases.
Maintainers can supply an additional raw vocabulary file locally; its contents
are hashed in memory and are never printed.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DENYLIST = ROOT / ".github" / "privacy-denylist.sha256"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
REAL_HOME = re.compile(r"/(?:Users|home)/([A-Za-z0-9._-]+)")
GENERIC_HOME_NAMES = {"example", "name", "operator", "runner", "user", "username"}
WORD = re.compile(r"[a-z0-9]+")
MAX_SCAN_BYTES = 2_000_000


def _run_git(*args: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=False,
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"git command failed: {args!r}")
    return proc.stdout


def _phrase_digest(words: list[str]) -> str:
    return hashlib.sha256(" ".join(words).encode("utf-8")).hexdigest()


def _load_digests(path: Path) -> set[str]:
    found: set[str] = set()
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = raw.strip().lower()
        if not value or value.startswith("#"):
            continue
        if not HEX64.fullmatch(value):
            raise ValueError(f"{path}:{line_number}: expected one SHA-256 digest")
        found.add(value)
    if not found:
        raise ValueError(f"{path}: denylist must not be empty")
    return found


def _load_private_vocabulary(path: Path | None) -> set[str]:
    if path is None:
        return set()
    found: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        words = WORD.findall(raw.casefold())
        if words:
            found.add(_phrase_digest(words))
    return found


def _matches(label: str, payload: bytes, denylist: set[str]) -> list[str]:
    if len(payload) > MAX_SCAN_BYTES:
        if b"\0" in payload[:8192]:
            return []
        return [f"{label}: oversized text payload requires explicit review"]
    text = payload.decode("utf-8", errors="ignore")
    findings: list[str] = []
    if any(
        match.group(1).casefold() not in GENERIC_HOME_NAMES
        for match in REAL_HOME.finditer(text)
    ):
        findings.append(f"{label}: absolute host home path")
    words = WORD.findall(text.casefold())
    seen: set[str] = set()
    for width in range(1, min(4, len(words)) + 1):
        for offset in range(0, len(words) - width + 1):
            digest = _phrase_digest(words[offset : offset + width])
            if digest in denylist and digest not in seen:
                findings.append(f"{label}: forbidden phrase digest {digest[:12]}")
                seen.add(digest)
    return findings


def _tracked_payloads() -> list[tuple[str, bytes]]:
    paths = [
        item.decode("utf-8", errors="surrogateescape")
        for item in _run_git(
            "ls-files", "-z", "--cached", "--others", "--exclude-standard"
        ).split(b"\0")
        if item
    ]
    payloads: list[tuple[str, bytes]] = []
    for relative in paths:
        path = ROOT / relative
        if path.is_file() and not path.is_symlink():
            payloads.append((f"tree:{relative}", path.read_bytes()))
    return payloads


def _reachable_payloads() -> list[tuple[str, bytes]]:
    payloads: list[tuple[str, bytes]] = []
    for row in _run_git("rev-list", "--objects", "--all").splitlines():
        oid = row.split(b" ", 1)[0].decode("ascii")
        kind = _run_git("cat-file", "-t", oid).decode("ascii").strip()
        if kind in {"blob", "commit"}:
            payloads.append((f"history:{kind}:{oid}", _run_git("cat-file", "-p", oid)))
    return payloads


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--denylist", type=Path, default=DEFAULT_DENYLIST)
    parser.add_argument("--vocabulary", type=Path)
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args(argv)

    denylist = _load_digests(args.denylist)
    denylist.update(_load_private_vocabulary(args.vocabulary))
    payloads = _tracked_payloads()
    if args.history:
        payloads.extend(_reachable_payloads())

    findings: list[str] = []
    for label, payload in payloads:
        findings.extend(_matches(label, payload, denylist))
    if findings:
        for finding in sorted(set(findings)):
            print(finding, file=sys.stderr)
        print(f"privacy hygiene: FAIL ({len(set(findings))} findings)", file=sys.stderr)
        return 1
    print(
        f"privacy hygiene: PASS ({len(payloads)} payloads, {len(denylist)} denylist digests)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
