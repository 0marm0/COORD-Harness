"""Port a source file into this repository with all inherited prose removed.

The extraction this repository came from had a specific hazard: business-confidential
content lived in prose -- docstrings, comments, fixtures -- not in identifiers. Detecting
such content with a wordlist is a guard scoped by the vocabulary it already knows about,
which cannot fail on the vocabulary it does not.

So this tool does not detect. It removes:

  * every comment,
  * every docstring,
  * every string literal flagged by the manifest as prose-bearing,

and then rewrites identifiers through an explicit rename table. What lands is mechanism
with no inherited English in it. Documentation is written fresh against the result.

A file is written only if the manifest lists it. There is no glob expansion and no
directory copy: a directory-shaped rule silently swallows untracked bytecode, symlinks
into data stores, and editor droppings.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMIT_RE = re.compile(r"[0-9a-f]{40,64}\Z")


# ---------------------------------------------------------------------------
# rename and refusal vocabulary
# ---------------------------------------------------------------------------
# Structural rules are hermetic. Source-specific vocabulary is loaded only from
# an explicit CLI/API path; an ignored local file never changes default CI.
_STRUCTURAL_FORBIDDEN_SPECS: tuple[tuple[str, str, int], ...] = (
    (r"/Users/[A-Za-z0-9_]+", "absolute home path", 0),
    (r"/Volumes/[A-Za-z0-9_ ]+", "named external volume", 0),
    (r"\b(?!127\.)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "bare IPv4 address", 0),
    # The trailing exclusion is not cosmetic: Apple's retina asset convention
    # produces `icon_16x16@2x.png`, which is a textbook match for an address --
    # local part, at-sign, dot, two-letter-plus "TLD". Every project with an
    # asset catalogue trips it, and a guard that fires on ordinary filenames is
    # one people learn to wave through.
    (r"[A-Za-z0-9._%+-]+@(?!example\.|.*\.invalid\b)[A-Za-z0-9.-]+"
     r"\.(?!(?:png|jpe?g|gif|webp|pdf|svg|heic|tiff?|ico|icns)\b)[A-Za-z]{2,}",
     "email address", 0),
    (r"\b(sk-ant-|sk-proj-|sk-|ghp_|gho_|AKIA|xox[bp]-)[A-Za-z0-9_-]{8,}", "credential-shaped token", 0),
)


def load_vocabulary(path: Path) -> dict:
    """Load only the explicitly named maintainer vocabulary."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("vocabulary root must be an object")
    return value


def compile_vocabulary(vocabulary: dict) -> tuple[
    tuple[tuple[str, str], ...], tuple[tuple[re.Pattern[str], str], ...]
]:
    renames: list[tuple[str, str]] = []
    for item in vocabulary.get("renames", []):
        if not isinstance(item, list) or len(item) != 2 or not all(isinstance(v, str) for v in item):
            raise ValueError("each rename must be [pattern, replacement]")
        re.compile(item[0])
        renames.append((item[0], item[1]))
    specs = list(_STRUCTURAL_FORBIDDEN_SPECS)
    for item in vocabulary.get("forbidden", []):
        if not isinstance(item, list) or len(item) not in {2, 3}:
            raise ValueError("each forbidden rule must be [pattern, reason, optional flags]")
        pattern, reason = item[:2]
        if not isinstance(pattern, str) or not isinstance(reason, str) or not reason:
            raise ValueError("forbidden pattern and reason must be strings")
        flags = re.I if len(item) == 3 and "i" in str(item[2]).lower() else 0
        specs.append((pattern, reason, flags))
    forbidden = tuple((re.compile(pattern, flags), reason) for pattern, reason, flags in specs)
    return tuple(renames), forbidden


RENAMES, FORBIDDEN = compile_vocabulary({})

# The vocabulary is not committed, so it is found by convention or named
# outright. The convention is "beside the manifest in use" -- resolved per run
# rather than against this file, so a test porting a synthetic tree with its own
# manifest is not judged against the real repository's vocabulary.
VOCABULARY_BASENAME = "vocabulary.json"


class PortRefusal(ValueError):
    """A refusal whose message is safe to print.

    Most failures here are reported by type alone, because the text can quote a
    manifest `find` string or a source path -- the very content this tool
    exists to keep out of the published tree. This subclass marks the failures
    whose messages are known to contain neither, so the operator gets something
    actionable instead of a bare exception name.
    """


def _require_vocabulary(
    vocabulary_path: Path | None,
    renames: Sequence[object],
    *,
    default: Path,
) -> None:
    """Refuse to port with no renames when a vocabulary was available.

    Omitting `--vocabulary` used to be a quiet no-op: every file still ported,
    the run still reported zero violations, and the tree kept every source
    identifier the vocabulary exists to remove. The output of a correct run and
    a catastrophic one differed by one easily-missed line in the summary.

    A tool whose worst outcome looks like success is the wrong shape. If a
    vocabulary is sitting where one is expected, running without it is a
    mistake, and the run stops rather than producing a tree that has to be
    caught downstream.
    """
    if renames:
        return
    if vocabulary_path is not None:
        raise PortRefusal(
            f"vocabulary {vocabulary_path} declares no renames; "
            "porting would leave every source identifier in place"
        )
    if default.exists():
        raise PortRefusal(
            f"no --vocabulary given but {default} exists. Porting without it "
            "would silently leave every source identifier in the tree. Pass "
            f"--vocabulary {default}, or move the file aside to port raw."
        )


#: Addresses that identify a service rather than a person, so finding one in a
#: commit trailer discloses nothing. The AI-assistant entries matter because
#: every commit co-authored by an assistant carries one, which otherwise makes
#: the history check red on ordinary work -- and a gate whose red is routine
#: stops being read, which is the failure this whole tool exists to prevent.
_NEUTRAL_NOREPLY = re.compile(
    r"(?:noreply@github\.com|noreply@users\.noreply\.github\.com|"
    r"(?:\d+\+)?[A-Za-z0-9-]+\[bot\]@users\.noreply\.github\.com|"
    r"noreply@anthropic\.com|noreply@openai\.com)\Z",
    re.I,
)


def is_neutral_noreply(value: str) -> bool:
    return bool(_NEUTRAL_NOREPLY.fullmatch(value))


@dataclass
class PortResult:
    source: Path
    dest: Path
    source_sha256: str
    docstrings_removed: int
    comments_removed: int
    renames_applied: int
    lines_in: int
    lines_out: int
    violations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "source": str(self.source),
            "dest": str(self.dest),
            "source_sha256": self.source_sha256,
            "docstrings_removed": self.docstrings_removed,
            "comments_removed": self.comments_removed,
            "renames_applied": self.renames_applied,
            "lines_in": self.lines_in,
            "lines_out": self.lines_out,
            "violations": self.violations,
        }


# ---------------------------------------------------------------------------
# docstring removal
# ---------------------------------------------------------------------------
_DOCSTRING_OWNERS = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _docstring_spans(tree: ast.AST) -> list[tuple[int, int, bool]]:
    """Return (start_line, end_line, body_would_be_empty) for every docstring.

    Lines are 1-indexed and inclusive. The third element says whether the
    docstring is the entire body, in which case a `pass` must replace it.
    """
    spans: list[tuple[int, int, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_OWNERS):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if not isinstance(first, ast.Expr):
            continue
        value = first.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        end = getattr(first, "end_lineno", first.lineno) or first.lineno
        alone = len(body) == 1 and not isinstance(node, ast.Module)
        spans.append((first.lineno, end, alone))
    return spans


def _strip_comments(source: str) -> tuple[str, int]:
    """Remove every comment, preserving a shebang and an encoding declaration."""
    removed = 0
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source, 0

    # Map line number -> column at which a comment begins.
    cuts: dict[int, int] = {}
    for tok in tokens:
        if tok.type != tokenize.COMMENT:
            continue
        line, col = tok.start
        text = tok.string
        if line <= 2 and (text.startswith("#!") or "coding" in text):
            continue
        cuts[line] = min(col, cuts.get(line, col))
        removed += 1

    out: list[str] = []
    for idx, raw in enumerate(source.splitlines(), start=1):
        if idx not in cuts:
            out.append(raw)
            continue
        kept = raw[: cuts[idx]].rstrip()
        # A line that was nothing but a comment disappears entirely.
        if kept:
            out.append(kept)
    return "\n".join(out) + ("\n" if source.endswith("\n") else ""), removed


def strip_hash_prose(source: str, marker: str) -> tuple[str, int]:
    """Remove whole-line and trailing comments introduced by `marker`.

    Used for SQL (`--`) and shell (`#`). String literals are respected: a marker
    inside quotes is content, not a comment. This is deliberately conservative --
    it only cuts when the marker is outside any quote.
    """
    kept: list[str] = []
    removed = 0
    for line in source.splitlines():
        out: list[str] = []
        quote = ""
        i, n = 0, len(line)
        cut = False
        while i < n:
            ch = line[i]
            if quote:
                out.append(ch)
                if ch == "\\" and i + 1 < n:
                    out.append(line[i + 1])
                    i += 2
                    continue
                if ch == quote:
                    quote = ""
                i += 1
                continue
            if ch in "'\"":
                quote = ch
                out.append(ch)
                i += 1
                continue
            if line.startswith(marker, i):
                cut = True
                removed += 1
                break
            out.append(ch)
            i += 1
        text = "".join(out).rstrip()
        if cut and not text:
            continue          # the line was nothing but a comment
        kept.append(text if cut else line.rstrip())
    return "\n".join(kept) + ("\n" if source.endswith("\n") else ""), removed


def strip_swift_prose(source: str) -> tuple[str, int]:
    """Remove Swift comments without touching string literals.

    A regex cannot do this: `"http://example"` contains `//`, and a naive pass
    silently truncates every URL in the file. So this walks the source one
    character at a time, tracking whether it is inside a string, an escape, a
    line comment, or a (nestable) block comment.
    """
    out: list[str] = []
    removed = 0
    i, n = 0, len(source)
    in_string = in_line = False
    block_depth = 0

    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""

        if in_line:
            if ch == "\n":
                in_line = False
                out.append(ch)
            i += 1
            continue

        if block_depth:
            if ch == "/" and nxt == "*":
                block_depth += 1
                i += 2
                continue
            if ch == "*" and nxt == "/":
                block_depth -= 1
                i += 2
                continue
            if ch == "\n":
                out.append(ch)
            i += 1
            continue

        if in_string:
            out.append(ch)
            if ch == "\\":
                if i + 1 < n:
                    out.append(source[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue

        if ch == "/" and nxt == "/":
            in_line = True
            removed += 1
            i += 2
            continue

        if ch == "/" and nxt == "*":
            block_depth = 1
            removed += 1
            i += 2
            continue

        out.append(ch)
        i += 1

    text = "".join(out)
    # Drop lines that held nothing but a comment, and trailing whitespace.
    kept = [line.rstrip() for line in text.splitlines()]
    return "\n".join(kept) + ("\n" if source.endswith("\n") else ""), removed


def strip_prose(source: str) -> tuple[str, int, int]:
    """Remove every docstring and comment. Returns (text, n_docstrings, n_comments)."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover - manifest should exclude these
        raise ValueError(f"cannot parse: {exc}") from exc

    spans = _docstring_spans(tree)
    lines = source.splitlines()
    drop: set[int] = set()
    replace: dict[int, str] = {}
    for start, end, alone in spans:
        for line_no in range(start, end + 1):
            drop.add(line_no)
        if alone:
            indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
            replace[start] = " " * indent + "pass"

    kept: list[str] = []
    for idx, raw in enumerate(lines, start=1):
        if idx in replace:
            kept.append(replace[idx])
        elif idx not in drop:
            kept.append(raw)

    text = "\n".join(kept)
    if source.endswith("\n"):
        text += "\n"
    text, n_comments = _strip_comments(text)
    return text, len(spans), n_comments


# ---------------------------------------------------------------------------
# renaming and verification
# ---------------------------------------------------------------------------
def apply_renames(
    text: str, renames: Sequence[tuple[str, str]] = RENAMES
) -> tuple[str, int]:
    total = 0
    for pattern, replacement in renames:
        text, n = re.subn(pattern, replacement, text)
        total += n
    return text, total


def find_violations(
    text: str,
    *,
    dest: Path,
    forbidden: Sequence[tuple[re.Pattern[str], str]] = FORBIDDEN,
) -> list[str]:
    """Report forbidden content without reproducing the matched content."""
    out: list[str] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for pattern, reason in forbidden:
            if pattern.search(line):
                out.append(f"{dest}:{line_no}: {reason}")
    return out


def collapse_blank_runs(text: str, limit: int = 2) -> str:
    """Stripping prose leaves ragged vertical space; normalise it."""
    return re.sub(r"\n{%d,}" % (limit + 1), "\n" * (limit + 1), text)


# ---------------------------------------------------------------------------
# declared edits
# ---------------------------------------------------------------------------
# Some product content survives prose-stripping because it lives in string
# literals -- hardcoded work identifiers, one-off historical repair contracts,
# pointers into the original repository's report tree. Those are removed by
# name, declared per file in the manifest, so that every removal is a reviewable
# decision rather than a regex that might match something else tomorrow.


def _bound_target_names(target: ast.expr) -> tuple[set[str], bool]:
    if isinstance(target, ast.Name):
        return {target.id}, True
    if isinstance(target, (ast.Tuple, ast.List)):
        names: set[str] = set()
        complete = True
        for element in target.elts:
            nested, nested_complete = _bound_target_names(element)
            names.update(nested)
            complete = complete and nested_complete
        return names, complete
    if isinstance(target, ast.Starred):
        return _bound_target_names(target.value)
    return set(), False


def drop_symbols(
    source: str, names: Iterable[str],
) -> tuple[str, list[str], list[str]]:
    """Delete declarations only when every bound assignment target is declared."""
    wanted = set(names)
    if not wanted:
        return source, [], []

    tree = ast.parse(source)
    drop: set[int] = set()
    found: list[str] = []
    problems: list[str] = []

    for node in tree.body:
        bound: set[str] = set()
        targets_complete = True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound = {node.name}
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names_in_target, complete = _bound_target_names(target)
                bound.update(names_in_target)
                targets_complete = targets_complete and complete
        elif isinstance(node, ast.AnnAssign):
            bound, targets_complete = _bound_target_names(node.target)
        selected = wanted.intersection(bound)
        if not selected:
            continue
        found.extend(sorted(selected))
        if not targets_complete or not bound.issubset(wanted):
            undeclared = sorted(bound - wanted)
            detail = ", ".join(undeclared) if undeclared else "non-symbol target"
            problems.append(
                "partial drop_symbol assignment rejected; all bound targets must "
                f"be declared (undeclared: {detail})"
            )
            continue
        start = min(
            [node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]
        )
        for line_no in range(start, (node.end_lineno or node.lineno) + 1):
            drop.add(line_no)

    kept = [ln for i, ln in enumerate(source.splitlines(), start=1) if i not in drop]
    text = "\n".join(kept)
    if source.endswith("\n"):
        text += "\n"
    return text, found, problems


def apply_literal_edits(
    text: str,
    edits: Sequence[dict[str, object]],
    *,
    already_applied: set[int] | None = None,
    report_misses: bool = True,
) -> tuple[str, list[str], set[int]]:
    """Apply declared find/replace pairs.

    Edits run twice per file -- once against the original source and once after
    prose has been stripped -- because a `find` may legitimately be written
    against either form. An author quoting a block that contains a comment can
    only match before stripping; one quoting code whose surrounding comments are
    gone can only match after. Applying at both points removes that trap.

    `already_applied` carries the indices matched in an earlier pass so a pair is
    never applied twice, and so only a pair that matched in *neither* pass is
    reported as a miss.
    """
    applied = set(already_applied or ())
    problems: list[str] = []
    for index, edit in enumerate(edits):
        if index in applied:
            continue
        find = edit.get("find")
        expected = edit.get("count", 1)
        if not isinstance(find, str) or not find:
            problems.append(f"declared edit {index} has an empty/non-string find literal")
            applied.add(index)
            continue
        if type(expected) is not int or expected <= 0:
            problems.append(f"declared edit {index} count must be a positive integer")
            applied.add(index)
            continue
        actual = text.count(find)
        if actual == 0:
            if report_misses:
                problems.append(f"declared edit {index} never matched")
            continue
        if actual != expected:
            problems.append(
                f"declared edit {index} cardinality mismatch: expected {expected}, found {actual}"
            )
            applied.add(index)
            continue
        text = text.replace(find, edit.get("with", ""), expected)
        applied.add(index)
    return text, problems, applied


# ---------------------------------------------------------------------------
# porting
# ---------------------------------------------------------------------------
def port_text(
    source: str,
    *,
    dest: Path,
    is_python: bool,
    drop: Sequence[str] = (),
    edits: Sequence[dict[str, object]] = (),
    renames: Sequence[tuple[str, str]] = RENAMES,
    forbidden: Sequence[tuple[re.Pattern[str], str]] = FORBIDDEN,
) -> tuple[str, PortResult]:
    docstrings = comments = 0
    text = source
    problems: list[str] = []
    suffix = dest.suffix.lower()

    # First pass: match against the original source, comments included.
    applied: set[int] = set()
    if edits:
        text, early_problems, applied = apply_literal_edits(text, edits, report_misses=False)
        problems += early_problems

    if suffix == ".sql":
        text, comments = strip_hash_prose(text, "--")
    elif suffix in {".sh", ".bash", ".toml", ".yaml", ".yml"}:
        text, comments = strip_hash_prose(text, "#")
    elif suffix in {".swift", ".js", ".kt", ".java", ".c", ".h", ".m"}:
        if drop:
            problems.append("drop_symbols is only supported for Python sources")
        text, comments = strip_swift_prose(text)

    if is_python:
        if drop:
            text, found, drop_problems = drop_symbols(text, drop)
            problems += drop_problems
            missed = sorted(set(drop) - set(found))
            problems += [f"declared drop_symbol never found: {name}" for name in missed]
        text, docstrings, comments = strip_prose(text)

    # Second pass: match against the stripped text. Only a pair that matched in
    # neither pass is a real miss.
    if edits:
        text, edit_problems, _ = apply_literal_edits(text, edits, already_applied=applied)
        problems += edit_problems

    text, rename_count = apply_renames(text, renames)
    text = collapse_blank_runs(text)

    result = PortResult(
        source=Path("<memory>"),
        dest=dest,
        source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        docstrings_removed=docstrings,
        comments_removed=comments,
        renames_applied=rename_count,
        lines_in=len(source.splitlines()),
        lines_out=len(text.splitlines()),
        violations=problems + find_violations(text, dest=dest, forbidden=forbidden),
    )
    return text, result


def port_file(
    source: Path,
    dest: Path,
    *,
    source_bytes: bytes,
    dry_run: bool = False,
    drop: Sequence[str] = (),
    edits: Sequence[dict[str, object]] = (),
    renames: Sequence[tuple[str, str]] = RENAMES,
    forbidden: Sequence[tuple[re.Pattern[str], str]] = FORBIDDEN,
) -> PortResult:
    try:
        raw = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("pinned source blob is not UTF-8 text") from exc
    text, result = port_text(
        raw, dest=dest, is_python=source.suffix == ".py", drop=drop, edits=edits,
        renames=renames, forbidden=forbidden,
    )
    result.source = source
    if result.violations:
        return result
    if source.suffix == ".py":
        # A transform that produces unparseable output is a bug in this tool,
        # not a finding about the source. Fail loudly rather than writing it.
        try:
            compile(text, str(dest), "exec")
        except SyntaxError as exc:
            result.violations.append(f"{dest}: port produced invalid Python: {exc}")
            return result
    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    return result


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------
def _safe_reason(
    text: str,
    *,
    renames: Sequence[tuple[str, str]] = RENAMES,
    forbidden: Sequence[tuple[re.Pattern[str], str]] = FORBIDDEN,
) -> str:
    renamed = apply_renames(str(text), renames)[0]
    for pattern, _reason in forbidden:
        renamed = pattern.sub("[redacted]", renamed)
    return renamed


def redact_manifest(
    entries: list[dict[str, object]],
    *,
    renames: Sequence[tuple[str, str]] = RENAMES,
    forbidden: Sequence[tuple[re.Pattern[str], str]] = FORBIDDEN,
) -> dict[str, object]:
    output: list[dict[str, object]] = []
    for entry in entries:
        redacted: dict[str, object] = {
            "dest": entry["dest"],
            "reason": _safe_reason(
                entry.get("reason", ""), renames=renames, forbidden=forbidden
            ),
        }
        if entry.get("drop_symbols"):
            redacted["dropped_symbol_count"] = len(entry["drop_symbols"])
            redacted["drop_category"] = "reviewed top-level symbol removal"
        if entry.get("edits"):
            redacted["edits"] = [
                {
                    "count": edit.get("count", 1),
                    "reason": _safe_reason(edit.get("reason", ""), renames=renames, forbidden=forbidden),
                }
                for edit in entry["edits"]
            ]
        output.append(redacted)
    return {
        "_comment": (
            "Ported files with private source paths and edit text omitted. "
            "Every edit retains its reviewed exact cardinality and safe reason."
        ),
        "files": output,
    }

def _git(
    root: Path, args: Sequence[str], *, text: bool = False,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=text, check=False
    )


def _source_snapshot_commit(path: Path) -> str:
    document = json.loads(path.read_text(encoding="utf-8"))
    snapshot = document.get("source_snapshot") if isinstance(document, dict) else None
    commit = snapshot.get("commit") if isinstance(snapshot, dict) else None
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        raise ValueError("manifest requires exact source_snapshot.commit")
    return commit


def _validate_source_snapshot(source_root: Path, expected_commit: str) -> None:
    pinned = _git(
        source_root, ["rev-parse", "--verify", f"{expected_commit}^{{commit}}"],
        text=True,
    )
    if pinned.returncode or pinned.stdout.strip() != expected_commit:
        raise ValueError("source_snapshot.commit is absent or not an exact commit")
    # Every byte read below comes from `expected_commit`, never the worktree,
    # so where the source checkout is parked does not affect what is ported.
    # Requiring HEAD to equal the pin only meant the tool could not run while
    # anyone was working in the source repository.
    #
    # `rev-parse --verify` above is satisfied by a commit that was reset away,
    # which is measured, not assumed. Reachability from a ref is what separates
    # a published state from an abandoned object.
    named = _git(
        source_root,
        ["for-each-ref", "--contains", expected_commit, "--count=1", "--format=%(refname)"],
        text=True,
    )
    if named.returncode or not named.stdout.strip():
        raise PortRefusal(
            f"source_snapshot.commit {expected_commit[:12]} is not reachable from any "
            "ref in the source repository"
        )


def _pinned_source_blob(source_root: Path, commit: str, rel: str) -> bytes:
    tree = _git(
        source_root,
        [
            "--literal-pathspecs", "ls-tree", "-z", "--full-tree",
            commit, "--", rel,
        ],
    )
    if tree.returncode:
        raise ValueError("pinned source tree is unreadable")
    records = [record for record in tree.stdout.split(b"\0") if record]
    if len(records) != 1:
        raise ValueError(f"pinned source path is absent: {rel}")
    try:
        meta, raw_path = records[0].split(b"\t", 1)
        mode, kind, oid = meta.decode("ascii").split(" ", 2)
        pinned_rel = raw_path.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("pinned source entry is malformed or undecodable") from exc
    if pinned_rel != rel:
        raise ValueError("pinned source path did not resolve exactly")
    if mode == "120000":
        raise ValueError(f"pinned source entry is a symlink: {rel}")
    if kind != "blob" or mode not in {"100644", "100755"}:
        raise ValueError(f"pinned source entry is non-regular ({mode} {kind}): {rel}")
    blob = _git(source_root, ["cat-file", "blob", oid])
    if blob.returncode:
        raise ValueError(f"pinned source blob is unreadable: {rel}")
    try:
        blob.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"pinned source blob is not UTF-8 text: {rel}") from exc
    return blob.stdout


def _safe_manifest_path(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError(f"{field} must be a non-empty normalized relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError(f"{field} forbids absolute and traversal paths: {value!r}")
    if pure.as_posix() != value or any(ch in value for ch in "*?["):
        raise ValueError(f"{field} must be an exact literal normalized path: {value!r}")
    return value


def load_manifest(path: Path) -> list[dict[str, object]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("manifest must be a regular non-symlink file")
    document = json.loads(path.read_text(encoding="utf-8"))
    entries = document.get("files") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise ValueError("manifest files must be a list")
    destinations: set[str] = set()
    normalized: list[dict[str, object]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"manifest entry {index} must be an object")
        source = _safe_manifest_path(entry.get("source"), field=f"entry {index} source")
        dest = _safe_manifest_path(entry.get("dest"), field=f"entry {index} dest")
        if dest in {"tools/extract/manifest.json", "tools/extract/port_receipt.json"}:
            raise ValueError(f"destination collides with generated publication artifact: {dest}")
        if dest in destinations:
            raise ValueError(f"duplicate destination: {dest}")
        destinations.add(dest)
        for edit_index, edit in enumerate(entry.get("edits", ())):
            if not isinstance(edit, dict):
                raise ValueError(f"entry {index} edit {edit_index} must be an object")
            find = edit.get("find")
            count = edit.get("count", 1)
            if not isinstance(find, str) or not find:
                raise ValueError(f"entry {index} edit {edit_index} requires non-empty find")
            if type(count) is not int or count <= 0:
                raise ValueError(f"entry {index} edit {edit_index} count must be positive")
        normalized.append({**entry, "source": source, "dest": dest})
    return normalized


def _safe_join(root: Path, rel: str, *, must_exist: bool) -> Path:
    root_resolved = root.resolve(strict=True)
    current = root
    for part in PurePosixPath(rel).parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink():
                raise ValueError(f"path contains symlink: {rel}")
    resolved = current.resolve(strict=must_exist)
    if not resolved.is_relative_to(root_resolved):
        raise ValueError(f"path escapes root: {rel}")
    if must_exist and not resolved.is_file():
        raise ValueError(f"source is not a regular file: {rel}")
    if not must_exist and current.exists() and not current.is_file():
        raise ValueError(f"destination is not a regular file: {rel}")
    return current


def _derived_destinations(dest_root: Path) -> set[str]:
    authored_path = dest_root / "tools" / "extract" / "authored.json"
    if not authored_path.exists():
        return set()
    if authored_path.is_symlink() or not authored_path.is_file():
        raise ValueError("authored.json must be a regular non-symlink file")
    authored = json.loads(authored_path.read_text(encoding="utf-8"))
    derived = authored.get("derived", {}) if isinstance(authored, dict) else None
    if not isinstance(derived, dict):
        raise ValueError("authored.json derived must be an object")
    return {_safe_manifest_path(rel, field="derived destination") for rel in derived}


def assert_one_way(source_root: Path, dest_root: Path) -> None:
    """Refuse missing, symlinked, or overlapping source/destination roots."""
    if source_root.is_symlink() or not source_root.is_dir():
        raise ValueError("source root must be an existing non-symlink directory")
    if dest_root.is_symlink():
        raise ValueError("destination root must not be a symlink")
    if not dest_root.exists():
        dest_root.mkdir(parents=True)
    if not dest_root.is_dir():
        raise ValueError("destination root must be a directory")
    src = source_root.resolve(strict=True)
    dst = dest_root.resolve(strict=True)
    if src == dst or dst.is_relative_to(src) or src.is_relative_to(dst):
        raise ValueError("source and destination roots must be disjoint")


def _write_stage(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


_atomic_replace = os.replace


def _publish_transaction(stage: Path, dest_root: Path, rels: Sequence[str]) -> None:
    """Publish staged files and restore the exact prior state on any failure."""
    backups = stage / ".backups"
    published: list[tuple[Path, Path | None]] = []
    created_dirs: list[Path] = []
    try:
        for rel in rels:
            target = _safe_join(dest_root, rel, must_exist=False)
            parent = target.parent
            missing_parents: list[Path] = []
            cursor = parent
            while cursor != dest_root and not cursor.exists():
                missing_parents.append(cursor)
                cursor = cursor.parent
            for directory in reversed(missing_parents):
                directory.mkdir()
                created_dirs.append(directory)
            staged = stage / rel
            backup: Path | None = None
            if target.exists():
                backup = backups / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
            _atomic_replace(staged, target)
            published.append((target, backup))
    except BaseException:
        rollback_errors: list[OSError] = []
        for target, backup in reversed(published):
            try:
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    os.replace(backup, target)
            except OSError as exc:
                rollback_errors.append(exc)
        for directory in reversed(created_dirs):
            try:
                directory.rmdir()
            except OSError:
                pass
        if rollback_errors:
            raise RuntimeError("publish failed and rollback was incomplete") from rollback_errors[0]
        raise


def run(
    manifest_path: Path,
    source_root: Path,
    dest_root: Path,
    *,
    dry_run: bool,
    vocabulary_path: Path | None = None,
) -> int:
    try:
        assert_one_way(source_root, dest_root)
        entries = load_manifest(manifest_path)
        expected_commit = _source_snapshot_commit(manifest_path)
        _validate_source_snapshot(source_root, expected_commit)
        vocabulary = load_vocabulary(vocabulary_path) if vocabulary_path else {}
        renames, forbidden = compile_vocabulary(vocabulary)
        _require_vocabulary(
            vocabulary_path,
            renames,
            default=manifest_path.parent / VOCABULARY_BASENAME,
        )
        derived = _derived_destinations(dest_root)
        overlap = sorted({str(entry["dest"]) for entry in entries} & derived)
        if overlap:
            raise ValueError(f"manifest destinations overlap reviewed derived files: {overlap}")
        sources = [
            (
                Path(str(entry["source"])),
                _pinned_source_blob(
                    source_root, expected_commit, str(entry["source"])
                ),
            )
            for entry in entries
        ]
        for entry in entries:
            _safe_join(dest_root, str(entry["dest"]), must_exist=False)
    except PortRefusal as refusal:
        print(f"BLOCKED: {refusal}")
        return 1
    except (OSError, ValueError, json.JSONDecodeError, re.error) as exc:
        print(f"BLOCKED: manifest/path validation failed ({type(exc).__name__})")
        return 1

    results: list[PortResult] = []
    with tempfile.TemporaryDirectory(prefix=".coord-port-stage-", dir=dest_root.parent) as temp:
        stage = Path(temp)
        for entry, (src, source_bytes) in zip(entries, sources, strict=True):
            try:
                result = port_file(
                    src,
                    stage / str(entry["dest"]),
                    source_bytes=source_bytes,
                    dry_run=False,
                    drop=entry.get("drop_symbols", ()),
                    edits=entry.get("edits", ()),
                    renames=renames,
                    forbidden=forbidden,
                )
            except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
                print(f"BLOCKED: transform failed ({type(exc).__name__})")
                return 1
            results.append(result)

        violations = [violation for result in results for violation in result.violations]
        print(f"manifest entries : {len(entries)}")
        print(f"ported           : {len(results) - sum(bool(r.violations) for r in results)}")
        print("missing sources  : 0")
        print(f"docstrings cut   : {sum(r.docstrings_removed for r in results)}")
        print(f"comments cut     : {sum(r.comments_removed for r in results)}")
        print(f"renames applied  : {sum(r.renames_applied for r in results)}")
        print(f"lines in -> out  : {sum(r.lines_in for r in results)} -> {sum(r.lines_out for r in results)}")
        print(f"violations       : {len(violations)}")
        for violation in violations[:60]:
            print(f"  BLOCKED {violation}")
        if violations or dry_run:
            return 1 if violations else 0

        manifest_rel = "tools/extract/manifest.json"
        receipt_rel = "tools/extract/port_receipt.json"
        try:
            _write_stage(
                stage / manifest_rel,
                json.dumps(redact_manifest(entries, renames=renames, forbidden=forbidden), indent=2) + "\n",
            )
        except OSError as exc:
            print(f"BLOCKED: staging publication manifest failed ({type(exc).__name__})")
            return 1
        receipt = []
        for entry, result in zip(entries, results, strict=True):
            public_result = {
                key: (len(value) if key == "violations" else value)
                for key, value in result.as_dict().items()
                if key not in {"source", "dest", "source_sha256"}
            }
            public_result["dest"] = str(entry["dest"])
            receipt.append(public_result)
        try:
            _write_stage(stage / receipt_rel, json.dumps(receipt, indent=2) + "\n")
        except OSError as exc:
            print(f"BLOCKED: staging publication receipt failed ({type(exc).__name__})")
            return 1
        rels = [str(entry["dest"]) for entry in entries] + [manifest_rel, receipt_rel]
        try:
            _publish_transaction(stage, dest_root, rels)
        except BaseException as exc:
            print(f"BLOCKED: atomic publication failed ({type(exc).__name__})")
            return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=REPO_ROOT / "tools" / "extract" / "manifest.private.json",
        help="the working manifest; never published (see redact_manifest)",
    )
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--dest-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--vocabulary", type=Path,
        help="explicit maintainer vocabulary; never auto-discovered",
    )
    args = parser.parse_args(argv)
    return run(
        args.manifest, args.source_root, args.dest_root,
        dry_run=args.dry_run, vocabulary_path=args.vocabulary,
    )


if __name__ == "__main__":
    sys.exit(main())
