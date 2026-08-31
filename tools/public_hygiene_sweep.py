#!/usr/bin/env python3
"""Fail closed on what must never reach a public remote.

Every wave of this project ended with a maintainer eyeballing changed files
for absolute home paths, personal names and emails, private-project
vocabulary, and the private origin project's internal work-id grammar. This
turns that manual sweep into a gate: it scans the tracked tree (or, with
``--diff-range``, only the files a git range touched) and exits non-zero on
a real hit.

The hard part is telling a genuine leak apart from a DETECTOR PATTERN --
this file's own regexes, and the fixtures in its test suite, legitimately
contain the shapes they hunt. That is solved by construction, not by a
heuristic: a small, named allowlist of scanner-owned paths (below), printed
on every run so it can never grow silently.

Personal names and other private-project vocabulary have no reliable
generic pattern, so this file never hardcodes any of it. A maintainer
supplies a phrase list out-of-band -- ``--vocabulary``, or the
``PUBLIC_HYGIENE_VOCAB_FILE`` / ``PUBLIC_HYGIENE_VOCAB`` environment
variables -- and when none is supplied, the sweep still runs its generic,
pattern-based checks (home paths, emails, work-id grammar) but says plainly
that vocabulary coverage was skipped rather than reporting a clean sweep it
did not perform.

"Absolute home paths" means paths that disclose a username, such as
``/Users/<name>`` or ``/home/<name>``: that is what ``REAL_HOME_RE`` below
matches. Shell-relative forms such as ``~/Developer/x`` or ``$HOME/Developer/x``
are deliberately not flagged -- neither one discloses who ran the command,
so there is nothing to catch.

This is the second scanner in the repository. ``tools/privacy_hygiene.py``
is the older, broader one: it walks reachable git history as well as the
working tree and takes its denylist from a maintainer-side vocabulary file
that is itself gitignored (``tools/extract/vocabulary.example.json`` is only
the template). This sweep is narrower and newer -- current tracked tree
only, no history -- and exists for the four checks above, each backed by a
name-shaped regex rather than a maintainer's private word list. Its own
``--vocabulary`` / ``PUBLIC_HYGIENE_VOCAB_FILE`` / ``PUBLIC_HYGIENE_VOCAB``
channel is a *second*, independent maintainer-supplied phrase list -- run
both scanners; configuring one's vocabulary does not configure the other's.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
from dataclasses import dataclass


ROOT = Path(__file__).resolve().parents[1]
MAX_SCAN_BYTES = 2_000_000

# Scanner-owned paths: the only files allowed to legitimately contain the
# patterns below as literal text (a regex example, a planted-leak fixture).
# Kept short and explicit on purpose -- widening it is a reviewable one-line
# diff, never a silent workaround -- and every run prints it verbatim.
SCANNER_OWNED_PATHS = (
    "tools/public_hygiene_sweep.py",
    "tests/test_public_hygiene_sweep.py",
)

REAL_HOME_RE = re.compile(r"/(?:Users|home)/([A-Za-z0-9._-]+)")
GENERIC_HOME_NAMES = {"example", "name", "operator", "runner", "user", "username"}

EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9][A-Za-z0-9._%+-]*@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,24}\b"
)
# Addresses/domains that read as a mailbox but are not a person's: CI
# service accounts, RFC 2606 placeholder domains, and GitHub's noreply
# relay. Also filters filename-shaped false positives such as
# "icon@2x.png", where the "domain" TLD is really a file extension.
GENERIC_EMAIL_ADDRESSES = {"noreply@anthropic.com", "actions@github.com", "support@github.com"}
GENERIC_EMAIL_DOMAIN_SUFFIXES = ("users.noreply.github.com",)
FILENAME_LIKE_TLDS = {
    "png", "jpg", "jpeg", "gif", "svg", "ico", "webp", "pdf", "zip",
    "json", "csv", "txt", "md", "py", "ts", "tsx", "js", "jsx", "css",
    "html", "yml", "yaml", "xml", "mp4", "mov", "wasm", "woff", "woff2", "ttf",
}

# The private origin project's durable-id grammar: a capital-N date-stamped
# serial, an owning-lane code, and a slug -- e.g. an id shaped like
# "N0831-CLA-...". This hardcodes the *shape* only, never a real instance,
# which is exactly what lets a stray report or fixture copied from that
# project be caught before it reaches this public remote. Companion rule:
# tests/test_public_generalization.py pins that a *stranger's* id grammar
# must never be required by this project's own tooling.
WORKID_GRAMMAR_RE = re.compile(r"\bN\d{4}-[A-Z]{2,6}(?:-[A-Z0-9]{1,24}){1,8}\b")


@dataclass(frozen=True)
class Finding:
    path: str
    category: str
    detail: str

    def render(self) -> str:
        return f"{self.path}: {self.detail} [{self.category}]"


class SweepError(RuntimeError):
    pass


def _run_git(*args: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), *args], check=False, capture_output=True
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise SweepError(detail or f"git command failed: {args!r}")
    return proc.stdout


def _tracked_paths() -> list[str]:
    out = _run_git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return [p.decode("utf-8", errors="surrogateescape") for p in out.split(b"\0") if p]


def _diff_range_paths(diff_range: str) -> list[str]:
    out = _run_git("diff", "--name-only", "-z", diff_range)
    return [p.decode("utf-8", errors="surrogateescape") for p in out.split(b"\0") if p]


def _load_vocabulary(explicit_path: Path | None) -> tuple[list[str], str]:
    """Load maintainer-supplied phrases. Returns (phrases, source-description).

    Nothing here is ever written back to the repo, and the phrases
    themselves are never printed -- only a short digest, for correlation.
    """
    phrases: list[str] = []
    sources: list[str] = []

    path = explicit_path
    env_source = "--vocabulary"
    if path is None:
        env_file = os.environ.get("PUBLIC_HYGIENE_VOCAB_FILE")
        if env_file:
            path = Path(env_file)
            env_source = "PUBLIC_HYGIENE_VOCAB_FILE"
    if path is not None:
        if not path.is_file():
            raise SweepError(f"vocabulary file not found: {path}")
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                phrases.append(line)
        sources.append(f"file ({env_source})")

    inline = os.environ.get("PUBLIC_HYGIENE_VOCAB")
    if inline:
        parts = [p.strip() for p in re.split(r"[,\n]", inline) if p.strip()]
        phrases.extend(parts)
        sources.append("PUBLIC_HYGIENE_VOCAB")

    return phrases, "+".join(sources) if sources else "none"


def _email_is_generic(address: str) -> bool:
    local, _, domain = address.partition("@")
    if not domain:
        return True
    domain_l = domain.casefold()
    if address.casefold() in GENERIC_EMAIL_ADDRESSES:
        return True
    if any(
        domain_l == suffix or domain_l.endswith("." + suffix)
        for suffix in GENERIC_EMAIL_DOMAIN_SUFFIXES
    ):
        return True
    # RFC 2606 reserved second-level domain: example.com/org/net/invalid/test
    # and any subdomain of it, whichever reserved TLD is used. Testing the
    # registrable label (not a prefix) is what catches a subdomain such as
    # "a@sub.example.com" -- domain_l.startswith("example.") would not,
    # since "sub.example.com" does not start with "example.".
    if domain_l.split(".")[-2:][0] == "example":
        return True
    tld = domain_l.rsplit(".", 1)[-1]
    if tld in FILENAME_LIKE_TLDS:
        return True
    return False


def _vocabulary_hits(text: str, phrases: list[str]) -> list[str]:
    """Digest prefixes of matched phrases, never the phrases themselves."""
    lowered = text.casefold()
    hits: list[str] = []
    for phrase in phrases:
        needle = phrase.casefold().strip()
        if needle and needle in lowered:
            hits.append(hashlib.sha256(phrase.encode("utf-8")).hexdigest()[:12])
    return hits


def _scan_text(label: str, text: str, phrases: list[str]) -> list[Finding]:
    findings: list[Finding] = []

    for match in REAL_HOME_RE.finditer(text):
        if match.group(1).casefold() not in GENERIC_HOME_NAMES:
            findings.append(Finding(label, "home_path", "absolute host home path"))
            break

    for match in EMAIL_RE.finditer(text):
        if not _email_is_generic(match.group(0)):
            findings.append(Finding(label, "email", "personal-looking email address"))
            break

    if WORKID_GRAMMAR_RE.search(text):
        findings.append(Finding(label, "workid_grammar", "internal work-id grammar"))

    for digest_prefix in _vocabulary_hits(text, phrases):
        findings.append(
            Finding(label, "vocabulary", f"private-vocabulary match (digest {digest_prefix})")
        )

    return findings


# Reasons a payload was never decoded into text. "scanner_owned" is a
# deliberate exclusion (see SCANNER_OWNED_PATHS above, printed on every run);
# "oversize" and "binary" are payloads the sweep could not safely read as
# text. A skip reason of any kind means the payload was NOT scanned -- it
# must never be counted toward `scanned` in the summary line, or the sweep
# would be crediting itself with coverage it did not perform.
SkipReason = str  # "scanner_owned" | "oversize" | "binary"


def _scan_payload(
    relative_path: str, payload: bytes, phrases: list[str]
) -> tuple[list[Finding], SkipReason | None]:
    """Return (findings, skip_reason). ``skip_reason`` is ``None`` only when
    the payload was actually decoded and text-scanned; otherwise it names why
    it was not, so the caller can report skips separately from scans.
    """
    if relative_path in SCANNER_OWNED_PATHS:
        return [], "scanner_owned"
    if len(payload) > MAX_SCAN_BYTES:
        return [], "oversize"
    if b"\0" in payload[:8192]:
        return [], "binary"
    text = payload.decode("utf-8", errors="ignore")
    return _scan_text(relative_path, text, phrases), None


@dataclass(frozen=True)
class SweepCounts:
    scanned: int
    skipped_binary: int
    skipped_oversize: int
    skipped_scanner_owned: int

    @property
    def skipped(self) -> int:
        return self.skipped_binary + self.skipped_oversize + self.skipped_scanner_owned

    def render(self) -> str:
        return (
            f"{self.scanned} file(s) scanned, {self.skipped} file(s) skipped "
            f"({self.skipped_binary} binary, {self.skipped_oversize} oversize, "
            f"{self.skipped_scanner_owned} scanner-owned)"
        )


def sweep(
    relative_paths: list[str], phrases: list[str]
) -> tuple[list[Finding], SweepCounts]:
    findings: list[Finding] = []
    scanned = 0
    skipped_binary = 0
    skipped_oversize = 0
    skipped_scanner_owned = 0
    for relative in relative_paths:
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            continue
        payload_findings, skip_reason = _scan_payload(
            relative, path.read_bytes(), phrases
        )
        if skip_reason is None:
            scanned += 1
        elif skip_reason == "binary":
            skipped_binary += 1
        elif skip_reason == "oversize":
            skipped_oversize += 1
        elif skip_reason == "scanner_owned":
            skipped_scanner_owned += 1
        else:  # pragma: no cover - defensive, no other reason is produced
            raise SweepError(f"unknown skip reason: {skip_reason!r}")
        findings.extend(payload_findings)
    return findings, SweepCounts(
        scanned=scanned,
        skipped_binary=skipped_binary,
        skipped_oversize=skipped_oversize,
        skipped_scanner_owned=skipped_scanner_owned,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vocabulary",
        type=Path,
        help="path to a maintainer-side phrase list (one phrase per line, "
        "'#' comments allowed); overrides PUBLIC_HYGIENE_VOCAB_FILE",
    )
    parser.add_argument(
        "--diff-range",
        help="scan only files touched by this git range (e.g. "
        "'origin/main...HEAD') instead of the whole tracked tree",
    )
    args = parser.parse_args(argv)

    try:
        phrases, vocab_source = _load_vocabulary(args.vocabulary)
        relative_paths = (
            _diff_range_paths(args.diff_range) if args.diff_range else _tracked_paths()
        )
    except SweepError as exc:
        print(f"public hygiene sweep: FAIL ({exc})", file=sys.stderr)
        return 1

    scope = f"diff range {args.diff_range!r}" if args.diff_range else "tracked tree"
    findings, counts = sweep(relative_paths, phrases)

    print(
        "public hygiene sweep: scanner-owned allowlist (excluded from every "
        f"scan, never grows silently): {', '.join(SCANNER_OWNED_PATHS)}"
    )
    if phrases:
        print(
            f"public hygiene sweep: private vocabulary loaded -- "
            f"{len(phrases)} phrase(s) from {vocab_source}"
        )
    else:
        print(
            "public hygiene sweep: private vocabulary NOT CONFIGURED -- "
            "ran generic checks only (home paths, emails, work-id grammar); "
            "personal names and project vocabulary were NOT checked. This is "
            "REDUCED COVERAGE, not a clean sweep. Set --vocabulary, "
            "PUBLIC_HYGIENE_VOCAB_FILE, or PUBLIC_HYGIENE_VOCAB to check them."
        )

    if findings:
        for finding in findings:
            print(finding.render(), file=sys.stderr)
        print(
            f"public hygiene sweep: FAIL ({len(findings)} finding(s) across "
            f"{counts.render()} in the {scope})",
            file=sys.stderr,
        )
        return 1

    print(
        f"public hygiene sweep: PASS ({counts.render()} in the {scope}, "
        f"{len(phrases)} vocabulary phrase(s))"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
