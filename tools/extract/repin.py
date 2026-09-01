"""Keep ``tools/extract/authored.json`` complete and current.

The publication gate requires every tracked file to be accounted for -- ported
in ``manifest.json``, written here in ``authored.json``'s ``files`` map, or
adapted and pinned in its ``derived`` map -- and requires each ``derived`` pin
to equal the hash of the content that is actually checked in. Both drift on
their own: a change adds files nobody declares, and an edit to an adapted file
makes its ``reviewed_sha256`` stale. The gate then reports failures that have
nothing to do with the change in front of it, and a gate that is always red is
one people learn to wave through.

``--check`` names both kinds of drift and prints the exact command that fixes
each. ``--apply`` performs the mechanical half only. It will not invent a
description for a file it was not given one for, and it will not re-pin a hash
without a reason, because both of those are statements a person is making about
content: *what this file is* and *why its new bytes are acceptable*. A tool that
writes them is forging a review. Every automated re-pin is stamped as
tool-assisted so the record never reads as an independent re-review, and the
stamp is folded into the entry's ``reason`` so the field a reader sees carries
the qualification. Both statements meet one floor, and a re-pin takes one
reason per file: a single sentence spread over several files is a rubber stamp,
not a judgement about any of them.

The inventory is Git's index -- exactly what ``gate.py`` reads -- so this tool
and CI cannot disagree. A worktree edit that has not been staged is therefore
invisible to both; ``--check`` reports such divergence separately, as advice,
rather than folding it into a verdict CI would not reach.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate  # noqa: E402

REPO_ROOT = gate.REPO_ROOT
AUTHORED_REL = "tools/extract/authored.json"
MANIFEST_REL = "tools/extract/manifest.json"
TOOL_REL = "tools/extract/repin.py"

#: Recorded on every automated re-pin. The point of the sentence is that a hash
#: matching again proves only that someone accepted the bytes, not that a
#: reviewer read them.
REPIN_METHOD = (
    "tool-assisted: the hash was updated to the staged bytes by "
    f"{TOOL_REL} --apply. This records acceptance of the new content, "
    "not an independent re-review of it."
)

#: A description or reason that begins with one of these is not a statement
#: about the file; it is a space where one was supposed to go. Matching only at
#: the start keeps honest prose that happens to mention the word -- "template
#: with placeholders only" describes real file content and must pass.
_VACANT_RE = re.compile(
    r"^(todo|tbd|fixme|xxx|wip|n/?a|none|unknown|placeholder|"
    r"undescribed|see above|same as above|\?+)\b",
    re.IGNORECASE,
)
#: The marker a design that auto-filled descriptions would have written. This
#: tool never writes it; the check rejects it so that such a value cannot merge
#: no matter which hand or tool put it there.
VACANT_MARKER = "UNDESCRIBED-PLACEHOLDER"
#: One floor for every human statement in the manifest -- a file's description
#: and a re-pin's reason alike. Two floors were worse than one: descriptions
#: used to need four characters, so ``--description "asdf"`` declared a file and
#: turned the accounting property into a string-length check.
MIN_STATEMENT_CHARS = 12
MIN_STATEMENT_WORDS = 3


@dataclass(frozen=True)
class Finding:
    """One reason the manifest does not match the tree, plus its fix."""

    kind: str
    path: str
    detail: str
    fix: str


def _git(root: Path, args: list[str], *, text: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=text, check=False)


def describe_command(rel: str) -> str:
    return (
        f"python {TOOL_REL} --apply --declare {shlex.quote(rel)} "
        '--description "what this file is and why it is here"'
    )


def repin_command(rel: str) -> str:
    return (
        f"python {TOOL_REL} --apply --repin {shlex.quote(rel)} "
        '--reason "why the new content is acceptable"'
    )


def vacant_statement(value: object) -> str | None:
    """Say why a description or reason is not a statement, or None if it is."""
    if not isinstance(value, str) or not value.strip():
        return "is empty"
    text = value.strip()
    if VACANT_MARKER in text:
        return f"carries the {VACANT_MARKER} marker"
    if _VACANT_RE.match(text):
        return "begins with a placeholder token"
    if len(text) < MIN_STATEMENT_CHARS or not any(character.isalpha() for character in text):
        return f"is too short to be a statement ({len(text)} characters)"
    if len(text.split()) < MIN_STATEMENT_WORDS:
        return f"is fewer than {MIN_STATEMENT_WORDS} words"
    return None


def load_index_state(root: Path) -> tuple[dict[str, bytes], list[str]]:
    """Read the staged inventory the gate reads: path -> blob bytes."""
    report = gate.Report()
    entries = gate.indexed_paths(root, report)
    # gate._indexed_blobs is deliberately reused rather than reimplemented: two
    # readers of the index that drift apart is the failure this tool exists to
    # prevent, one level up.
    blobs = gate._indexed_blobs(root, entries, report)
    problems = [f"index: {item}" for item in report.shape]
    for entry in entries:
        if entry.stage:
            problems.append(f"{entry.rel}: unmerged index entry")
    return blobs, problems


def manifest_destinations(blobs: dict[str, bytes]) -> set[str]:
    try:
        manifest = json.loads(blobs[MANIFEST_REL].decode("utf-8"))
        items = manifest.get("files", [])
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return set()
    return {
        item["dest"]
        for item in items
        if isinstance(item, dict) and isinstance(item.get("dest"), str)
    }


def load_authored(source: bytes | None) -> dict:
    if source is None:
        return {}
    try:
        value = json.loads(source.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def check(root: Path) -> tuple[list[Finding], list[str], int]:
    """Compare the staged tree against the staged manifest."""
    blobs, problems = load_index_state(root)
    findings: list[Finding] = []
    for problem in problems:
        findings.append(
            Finding("index", problem.split(":", 1)[0], problem, "resolve the index state first")
        )
    authored = load_authored(blobs.get(AUTHORED_REL))
    files = authored.get("files") if isinstance(authored.get("files"), dict) else {}
    derived = authored.get("derived") if isinstance(authored.get("derived"), dict) else {}
    if not authored:
        findings.append(
            Finding(
                "manifest",
                AUTHORED_REL,
                "staged authored.json is missing or is not a JSON object",
                f"stage a valid {AUTHORED_REL}",
            )
        )
    accounted = gate.INFRASTRUCTURE | manifest_destinations(blobs) | set(files) | set(derived)
    for rel in sorted(blobs):
        if rel not in accounted:
            findings.append(
                Finding(
                    "undeclared",
                    rel,
                    "tracked file appears in no manifest",
                    describe_command(rel),
                )
            )
    for rel, description in sorted(files.items()):
        problem = vacant_statement(description)
        if problem is not None:
            findings.append(
                Finding("vacant", rel, f"authored description {problem}", describe_command(rel))
            )
    for rel, entry in sorted(derived.items()):
        if not isinstance(entry, dict):
            findings.append(
                Finding("derived", rel, "derived entry is not an object", repin_command(rel))
            )
            continue
        problem = vacant_statement(entry.get("reason"))
        if problem is not None:
            findings.append(Finding("vacant", rel, f"derived reason {problem}", repin_command(rel)))
        stamp = entry.get("repin")
        if stamp is not None:
            if not isinstance(stamp, dict):
                findings.append(
                    Finding("vacant", rel, "repin stamp is not an object", repin_command(rel))
                )
            else:
                stamp_problem = vacant_statement(stamp.get("reason"))
                if stamp_problem is not None:
                    findings.append(
                        Finding(
                            "vacant",
                            rel,
                            f"recorded re-pin reason {stamp_problem}",
                            repin_command(rel),
                        )
                    )
        pinned = entry.get("reviewed_sha256")
        blob = blobs.get(rel)
        if blob is None:
            findings.append(
                Finding(
                    "untracked-declaration",
                    rel,
                    "declared as derived but not present in the index",
                    f"stage {shlex.quote(rel)} or remove its derived entry",
                )
            )
        elif not isinstance(pinned, str) or not gate.SHA256_RE.fullmatch(pinned):
            findings.append(
                Finding("stale", rel, "derived entry has no full reviewed_sha256", repin_command(rel))
            )
        elif hashlib.sha256(blob).hexdigest() != pinned:
            findings.append(
                Finding("stale", rel, "staged content differs from reviewed_sha256", repin_command(rel))
            )
    for rel in sorted(set(files) - set(blobs)):
        findings.append(
            Finding(
                "untracked-declaration",
                rel,
                "declared as authored but not present in the index",
                f"stage {shlex.quote(rel)} or remove its authored entry",
            )
        )
    return findings, advisories(root, blobs, derived), len(blobs)


def advisories(root: Path, blobs: dict[str, bytes], derived: dict) -> list[str]:
    """Worktree facts the index-based verdict cannot see, reported as advice."""
    notes: list[str] = []
    untracked = _git(root, ["ls-files", "--others", "--exclude-standard", "-z"])
    if not untracked.returncode:
        names = sorted(name for name in untracked.stdout.split(b"\0") if name)
        shown = [name.decode("utf-8", "replace") for name in names[:10]]
        if names:
            notes.append(
                f"{len(names)} file(s) exist in the worktree but not in the index; each will "
                "need a declaration once staged: " + ", ".join(shown)
                + (" ..." if len(names) > 10 else "")
            )
    for rel in sorted(derived):
        path = root / PurePosixPath(rel)
        blob = blobs.get(rel)
        if blob is None or not path.is_file():
            continue
        try:
            current = path.read_bytes()
        except OSError:
            continue
        if current != blob:
            notes.append(
                f"{rel}: worktree content differs from the staged content this check measured; "
                "its pin will go stale when you stage the file"
            )
    authored_path = root / PurePosixPath(AUTHORED_REL)
    staged_authored = blobs.get(AUTHORED_REL)
    if authored_path.is_file() and staged_authored is not None:
        if authored_path.read_bytes() != staged_authored:
            notes.append(
                f"{AUTHORED_REL}: edited in the worktree but not staged; this check and the "
                "publication gate both read the staged copy"
            )
    return notes


def qualified_reason(existing: object, repin_reason: str) -> str:
    """Carry a re-pin's qualification into the field a human actually reads.

    An entry's ``reason`` describes the content that was reviewed. A re-pin
    moves ``reviewed_sha256`` to different content, so a ``reason`` left
    byte-identical then sits directly above a hash of bytes it does not
    describe -- a specific old review paragraph reading as though it covered
    the new file. The qualification used to live only in the nested ``repin``
    object, a key ``check`` never requires and nothing surfaces.
    """
    base = existing.strip() if isinstance(existing, str) and existing.strip() else ""
    addition = f"Re-pinned: {repin_reason.strip()} ({REPIN_METHOD})"
    if base.endswith(addition):
        return base
    return f"{base}\n\n{addition}" if base else addition


def _reinsert_sorted(mapping: dict, key: str, value: object) -> dict:
    """Insert a key so an already-sorted map stays sorted and diffs stay small."""
    if key in mapping:
        mapping[key] = value
        return mapping
    items = list(mapping.items())
    position = len(items)
    for index, (existing, _) in enumerate(items):
        if existing > key:
            position = index
            break
    items.insert(position, (key, value))
    return dict(items)


def apply(
    root: Path,
    *,
    declare: list[str],
    descriptions: list[str],
    repin: list[str],
    repin_all: bool,
    reasons: list[str],
) -> tuple[list[str], list[str]]:
    """Register declarations and re-pin hashes. Returns (changes, refusals)."""
    changes: list[str] = []
    refusals: list[str] = []
    if len(declare) != len(descriptions):
        refusals.append(
            f"refused: {len(declare)} --declare path(s) but {len(descriptions)} --description "
            "value(s). A description says what a file is, which this tool cannot know; "
            "supply exactly one --description per --declare, in the same order."
        )
        return changes, refusals
    blobs, problems = load_index_state(root)
    refusals.extend(f"refused: {problem}" for problem in problems)
    if refusals:
        return changes, refusals
    authored_path = root / PurePosixPath(AUTHORED_REL)
    try:
        document = json.loads(authored_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return changes, [f"refused: {AUTHORED_REL} is missing or is not valid JSON"]
    files = document.get("files")
    derived = document.get("derived")
    if not isinstance(files, dict) or not isinstance(derived, dict):
        return changes, [f"refused: {AUTHORED_REL} needs object 'files' and 'derived' maps"]

    accounted = gate.INFRASTRUCTURE | manifest_destinations(blobs) | set(files) | set(derived)
    for rel, description in zip(declare, descriptions):
        problem = vacant_statement(description)
        if problem is not None:
            refusals.append(
                f"refused {rel}: the description {problem}. This tool does not write a "
                "description it was not given -- an undescribed file would then be declared "
                "without anyone having said what it is."
            )
            continue
        staged = rel in blobs
        if not staged and not (root / PurePosixPath(rel)).is_file():
            refusals.append(
                f"refused {rel}: no such file in the index or the worktree. A declaration "
                "names a file that exists; check the path."
            )
            continue
        if rel in accounted:
            refusals.append(f"refused {rel}: already accounted for in a manifest")
            continue
        files = _reinsert_sorted(files, rel, description.strip())
        accounted.add(rel)
        # A `files` entry pins no hash, so declaring a file before it is staged is
        # safe and matches how people work: write the file, describe it, stage
        # both. It only counts once staged, which --check reports either way.
        suffix = "" if staged else " (not yet staged; the gate sees it only once you stage it)"
        changes.append(f"declared {rel}{suffix}")

    # One reason per file, always. A reason is a judgement about one file's new
    # bytes; reusing a single sentence across several files launders it into a
    # rubber stamp, which is exactly what --repin-all made a one-liner.
    explicit = list(dict.fromkeys(repin))
    if len(explicit) != len(repin):
        refusals.append("refused: the same --repin path was given more than once")
        explicit = []
    stale: list[str] = []
    if repin_all:
        if explicit:
            refusals.append(
                "refused: --repin-all discovers its own targets; do not combine it with --repin."
            )
        else:
            for rel, entry in sorted(derived.items()):
                blob = blobs.get(rel)
                if not isinstance(entry, dict) or blob is None:
                    continue
                if entry.get("reviewed_sha256") != hashlib.sha256(blob).hexdigest():
                    stale.append(rel)
            if len(stale) > 1:
                refusals.append(
                    f"refused: --repin-all found {len(stale)} stale pin(s) and would re-pin them "
                    "all under one reason. A reason states why one file's new bytes are "
                    "acceptable, so it cannot stand for several files at once. Re-pin them one "
                    "at a time, each with its own reason:\n"
                    + "\n".join(f"      {repin_command(rel)}" for rel in stale)
                )
                stale = []
    requested = explicit + stale
    targets: list[str] = []
    reason_for: dict[str, str] = {}
    if requested:
        if len(reasons) != len(requested):
            refusals.append(
                f"refused: {len(requested)} file(s) to re-pin but {len(reasons)} --reason "
                "value(s). Re-pinning says the new bytes are acceptable, which is a judgement "
                "about one file; supply exactly one --reason per file, in the same order."
            )
        else:
            for rel, value in zip(requested, reasons):
                problem = vacant_statement(value)
                if problem is not None:
                    refusals.append(
                        f"refused {rel}: --reason {problem}. The reason is recorded next to the "
                        "hash and read as the review, so it has to be one."
                    )
                    continue
                targets.append(rel)
                reason_for[rel] = value
    for rel in sorted(targets):
        entry = derived.get(rel)
        if not isinstance(entry, dict):
            refusals.append(f"refused {rel}: no derived entry to re-pin")
            continue
        blob = blobs.get(rel)
        if blob is None:
            refusals.append(f"refused {rel}: not in the Git index")
            continue
        path = root / PurePosixPath(rel)
        if path.is_file() and path.read_bytes() != blob:
            refusals.append(
                f"refused {rel}: worktree content differs from staged content. Re-pinning the "
                "staged bytes here would bless content you are not about to commit; stage the "
                "file, then re-pin."
            )
            continue
        current = hashlib.sha256(blob).hexdigest()
        previous = entry.get("reviewed_sha256")
        if previous == current:
            refusals.append(f"skipped {rel}: pin already matches the staged content")
            continue
        entry["reviewed_sha256"] = current
        entry["repin"] = {
            "reason": reason_for[rel].strip(),
            "method": REPIN_METHOD,
            "previous_sha256": previous if isinstance(previous, str) else "",
        }
        entry["reason"] = qualified_reason(entry.get("reason"), reason_for[rel])
        changes.append(f"re-pinned {rel}")

    if changes:
        document["files"] = files
        document["derived"] = derived
        authored_path.write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return changes, refusals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--check", action="store_true", help="report undeclared files and stale pins (default)"
    )
    parser.add_argument("--apply", action="store_true", help="register declarations, re-pin hashes")
    parser.add_argument(
        "--declare", action="append", default=[], help="path to register as authored"
    )
    parser.add_argument(
        "--description", action="append", default=[], help="what the declared file is"
    )
    parser.add_argument("--repin", action="append", default=[], help="derived path to re-pin")
    parser.add_argument(
        "--repin-all",
        action="store_true",
        help="re-pin a single stale derived pin; refused when more than one is stale",
    )
    parser.add_argument(
        "--reason",
        action="append",
        default=[],
        help="why the new content of a re-pinned file is acceptable (one per --repin)",
    )
    args = parser.parse_args(argv)
    if args.check and args.apply:
        parser.error("--check and --apply are separate runs")
    root = args.root

    if args.apply:
        if not (args.declare or args.repin or args.repin_all):
            parser.error("--apply needs --declare, --repin, or --repin-all")
        changes, refusals = apply(
            root,
            declare=args.declare,
            descriptions=args.description,
            repin=args.repin,
            repin_all=args.repin_all,
            reasons=args.reason,
        )
        for change in changes:
            print(f"  {change}")
        for refusal in refusals:
            print(f"  {refusal}")
        if changes:
            print(
                f"\nWrote {AUTHORED_REL}. Stage it -- this check and the publication gate both "
                "read the Git index, so an unstaged edit changes neither verdict."
            )
        return 1 if refusals else 0

    findings, notes, scanned = check(root)
    print(f"staged files  : {scanned}")
    for kind, label in (
        ("undeclared", "undeclared"),
        ("stale", "stale pins"),
        ("vacant", "unstated"),
        ("untracked-declaration", "not staged"),
        ("derived", "malformed"),
        ("index", "index"),
    ):
        print(f"{label:<14}: {sum(1 for item in findings if item.kind == kind)} failure(s)")
    for finding in findings:
        print(f"\n  {finding.kind.upper()}  {finding.path}: {finding.detail}")
        print(f"      fix: {finding.fix}")
    for note in notes:
        print(f"\n  NOTE  {note}")
    if findings:
        print(
            f"\nMANIFEST STALE: {len(findings)} finding(s). The publication gate reports these "
            "as coverage failures; run the fix printed under each one."
        )
        return 1
    print("\nMANIFEST CURRENT: every staged file is declared and every pin matches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
