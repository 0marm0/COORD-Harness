"""Read-only structural validation for the repository documentation."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote_to_bytes, urlsplit

SUCCESS = (
    "documentation validation passed: links, JSON, SVG accessibility, "
    "PNG hashes, provenance"
)
MAX_MESSAGES = 25
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
PNG_PROVENANCE_CLASSES = {
    "synthetic-web-capture",
    "clean-room-native-capture",
    "maintainer-artwork",
}
RUNTIME_IMAGE_DIRS = (
    "apps/brand/Assets/",
    "apps/brand/Assets.xcassets/",
    "src/coordharness/board/static/",
)
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
REFERENCE_RE = re.compile(
    r"(?m)^ {0,3}\[[^\]\n]+\]:[ \t]*(?:<(?P<angle>[^>\n]+)>|(?P<plain>\S+))"
)
# A prose citation names a doc and a "§4a"-style section inside a Markdown
# link's visible label, e.g. "[release readiness checklist §2a](next-steps.md#...)".
CITATION_RE = re.compile(
    r"\[(?P<label>[^\]\n]*§(?P<section>[0-9]+[a-z]?)\b[^\]\n]*)\]\((?P<target>[^)\n]+)\)"
)
HEADING_TOKEN_RE = re.compile(r"^ {0,3}#{1,6}\s+(?P<token>[0-9]+[a-z]?)(?=[.:)\s]|$)")


@dataclass
class Report:
    """A token-bounded report that still counts every discovered issue.

    ``notes`` is a second, non-fatal channel. Everything this validator checked
    before was pass/fail, so a finding and a failure were the same thing. The
    documentation-custody check is not: a protected document that MOVED still
    exists, and refusing a rename would make this tool something a maintainer
    routes around. Notes are printed either way and change no exit code.
    """

    total: int = 0
    messages: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, message: str) -> None:
        self.total += 1
        if len(self.messages) < MAX_MESSAGES:
            self.messages.append(message)

    def note(self, message: str) -> None:
        if len(self.notes) < MAX_MESSAGES:
            self.notes.append(message)


class _HTMLLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.targets: list[tuple[int, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._capture(attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._capture(attrs)

    def _capture(self, attrs: list[tuple[str, str | None]]) -> None:
        line, _ = self.getpos()
        for name, value in attrs:
            if name.lower() in {"href", "src", "poster"} and value is not None:
                self.targets.append((line, value))


def _display(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _brief(value: str, limit: int = 160) -> str:
    cleaned = value.replace("\n", "\\n").replace("\r", "\\r")
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3] + "..."


def _mask_markdown_code(text: str) -> str:
    """Blank fenced, indented, and inline code while preserving offsets."""

    lines = text.splitlines(keepends=True)
    masked: list[str] = []
    fence_char = ""
    fence_length = 0
    for line in lines:
        candidate = line.rstrip("\r\n")
        match = FENCE_RE.match(candidate)
        if fence_char:
            masked.append("".join("\n" if char == "\n" else "\r" if char == "\r" else " " for char in line))
            if re.match(
                rf"^ {{0,3}}{re.escape(fence_char)}{{{fence_length},}}[ \t]*$", candidate
            ):
                fence_char = ""
            continue
        if match:
            run = match.group(1)
            fence_char, fence_length = run[0], len(run)
            masked.append("".join("\n" if char == "\n" else "\r" if char == "\r" else " " for char in line))
        elif line.startswith(("    ", "\t")):
            masked.append("".join("\n" if char == "\n" else "\r" if char == "\r" else " " for char in line))
        else:
            masked.append(line)

    chars = list("".join(masked))
    index = 0
    while index < len(chars):
        if chars[index] != "`":
            index += 1
            continue
        end_run = index
        while end_run < len(chars) and chars[end_run] == "`":
            end_run += 1
        marker = "`" * (end_run - index)
        close = "".join(chars).find(marker, end_run)
        if close < 0:
            index = end_run
            continue
        for position in range(index, close + len(marker)):
            if chars[position] not in "\r\n":
                chars[position] = " "
        index = close + len(marker)
    return "".join(chars)


def _markdown_targets(text: str) -> list[tuple[int, str]]:
    targets: list[tuple[int, str]] = []
    for match in REFERENCE_RE.finditer(text):
        target = match.group("angle") or match.group("plain") or ""
        start = match.start("angle") if match.group("angle") is not None else match.start("plain")
        targets.append((text.count("\n", 0, start) + 1, target))

    index = 0
    while True:
        marker = text.find("](", index)
        if marker < 0:
            break
        cursor = marker + 2
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        start = cursor
        if cursor < len(text) and text[cursor] == "<":
            start = cursor + 1
            cursor = start
            while cursor < len(text) and text[cursor] != ">" and text[cursor] not in "\r\n":
                cursor += 1
            if cursor < len(text) and text[cursor] == ">":
                targets.append((text.count("\n", 0, start) + 1, text[start:cursor]))
        else:
            depth = 0
            escaped = False
            while cursor < len(text):
                char = text[cursor]
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == "(":
                    depth += 1
                elif char == ")":
                    if depth == 0:
                        break
                    depth -= 1
                elif char.isspace() and depth == 0:
                    break
                cursor += 1
            targets.append((text.count("\n", 0, start) + 1, text[start:cursor]))
        index = marker + 2
    return targets


def _check_link(root: Path, source: Path, line: int, raw: str, report: Report) -> None:
    target = raw.strip()
    if not target or target.startswith("#"):
        return
    try:
        parsed = urlsplit(target)
    except ValueError:
        report.add(f"{_display(root, source)}:{line}: malformed link target: {_brief(target)}")
        return
    if parsed.scheme.lower() in {"http", "https", "mailto"} or target.startswith("//"):
        return
    if parsed.scheme:
        return
    if not parsed.path:
        return
    try:
        decoded = unquote_to_bytes(parsed.path).decode("utf-8")
    except UnicodeDecodeError:
        report.add(
            f"{_display(root, source)}:{line}: link target has invalid UTF-8 URL encoding: "
            f"{_brief(target)}"
        )
        return
    if "\x00" in decoded:
        report.add(f"{_display(root, source)}:{line}: link target contains a NUL byte")
        return
    candidate = (root / decoded.lstrip("/")) if decoded.startswith("/") else source.parent / decoded
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        report.add(
            f"{_display(root, source)}:{line}: local link escapes repository: {_brief(target)}"
        )
        return
    if not resolved.exists():
        report.add(
            f"{_display(root, source)}:{line}: local link target does not exist: {_brief(target)}"
        )


def _tracked_paths(root: Path, report: Report) -> list[str] | None:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        report.add(f"repository: cannot run git ls-files: {type(exc).__name__}")
        return None
    if result.returncode:
        report.add("repository: cannot inventory the Git index")
        return None
    try:
        return sorted(path for path in result.stdout.decode("utf-8").split("\x00") if path)
    except UnicodeDecodeError:
        report.add("repository: Git index contains a non-UTF-8 path")
        return None


def _check_links(root: Path, tracked: list[str], report: Report) -> None:
    if "README.md" not in tracked:
        report.add("README.md: required tracked documentation file is missing")
    sources = [root / rel for rel in tracked if rel.lower().endswith(".md")]
    for source in sources:
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.add(f"{_display(root, source)}: cannot read UTF-8 Markdown: {type(exc).__name__}")
            continue
        masked = _mask_markdown_code(text)
        for line, target in _markdown_targets(masked):
            _check_link(root, source, line, target, report)
        parser = _HTMLLinkParser()
        parser.feed(masked)
        for line, target in parser.targets:
            _check_link(root, source, line, target, report)


def _heading_tokens(text: str) -> set[str]:
    """Leading '§N[a]'-style tokens off every ATX heading, lowercased."""

    tokens: set[str] = set()
    for line in text.splitlines():
        match = HEADING_TOKEN_RE.match(line)
        if match:
            tokens.add(match.group("token").lower())
    return tokens


def _check_prose_citations(root: Path, tracked: list[str], report: Report) -> None:
    """A '[doc name §4a](target)' citation must resolve to a real heading in target."""

    tracked_set = set(tracked)
    sources = [root / rel for rel in tracked if rel.lower().endswith(".md")]
    for source in sources:
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        masked = _mask_markdown_code(text)
        for match in CITATION_RE.finditer(masked):
            target = match.group("target").strip()
            if not target or target.startswith("#"):
                # A same-document anchor citation isn't a cross-doc section claim.
                continue
            try:
                parsed = urlsplit(target)
            except ValueError:
                continue
            if parsed.scheme or not parsed.path:
                continue
            try:
                decoded = unquote_to_bytes(parsed.path).decode("utf-8")
            except UnicodeDecodeError:
                continue
            candidate = (root / decoded.lstrip("/")) if decoded.startswith("/") else source.parent / decoded
            resolved = candidate.resolve(strict=False)
            try:
                rel = resolved.relative_to(root).as_posix()
            except ValueError:
                continue
            if rel not in tracked_set or not resolved.is_file():
                # Already reported (or not) by _check_links; don't double up.
                continue
            try:
                cited_text = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            section = match.group("section").lower()
            if section not in _heading_tokens(cited_text):
                line = masked.count("\n", 0, match.start()) + 1
                report.add(
                    f"{_display(root, source)}:{line}: prose citation "
                    f"§{match.group('section')} has no matching heading in {rel}"
                )


def _load_json(root: Path, tracked: list[str], report: Report) -> dict[Path, object]:
    values: dict[Path, object] = {}
    for rel in tracked:
        if not rel.lower().endswith(".json") or ".coordharness" in PurePosixPath(rel).parts:
            continue
        path = root / rel
        try:
            values[path] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            report.add(
                f"{_display(root, path)}:{exc.lineno}:{exc.colno}: invalid JSON: {exc.msg}"
            )
        except (OSError, UnicodeDecodeError) as exc:
            report.add(f"{_display(root, path)}: cannot read JSON: {type(exc).__name__}")
    return values


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def _check_svgs(root: Path, tracked: list[str], report: Report) -> None:
    svg_paths = (
        root / rel
        for rel in tracked
        if rel.startswith("docs/assets/") and rel.lower().endswith(".svg")
    )
    for path in svg_paths:
        label = _display(root, path)
        try:
            svg = ET.parse(path).getroot()
        except ET.ParseError as exc:
            report.add(f"{label}: invalid SVG XML: {exc}")
            continue
        except OSError as exc:
            report.add(f"{label}: cannot read SVG: {type(exc).__name__}")
            continue
        if _local_name(svg.tag) != "svg":
            report.add(f"{label}: root element must be <svg>")
        if svg.get("role") != "img":
            report.add(f'{label}: root <svg> must have role="img"')
        references = set((svg.get("aria-labelledby") or "").split())
        title_ok = False
        description_ok = False
        for element in svg.iter():
            element_id = element.get("id")
            if element_id in references and "".join(element.itertext()).strip():
                title_ok |= _local_name(element.tag) == "title"
                description_ok |= _local_name(element.tag) == "desc"
            for name, value in element.attrib.items():
                if _local_name(name).lower() != "href":
                    continue
                href = value.strip()
                try:
                    scheme = urlsplit(href).scheme.lower()
                except ValueError:
                    scheme = "invalid"
                if scheme in {"http", "https", "file"} or href.startswith("//"):
                    report.add(f"{label}: external http/file href is forbidden: {_brief(href)}")
        if not title_ok:
            report.add(
                f"{label}: aria-labelledby must reference a nonempty <title> by id"
            )
        if not description_ok:
            report.add(
                f"{label}: aria-labelledby must reference a nonempty <desc> by id"
            )


def _safe_provenance_path(root: Path, value: object) -> tuple[str | None, Path | None]:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return None, None
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        return None, None
    resolved = (root / pure.as_posix()).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        return None, None
    return pure.as_posix(), resolved


def _ihdr_dimensions(data: bytes) -> tuple[int, int] | None:
    if (
        len(data) < 33
        or data[:8] != PNG_SIGNATURE
        or data[8:12] != struct.pack(">I", 13)
        or data[12:16] != b"IHDR"
    ):
        return None
    width, height = struct.unpack(">II", data[16:24])
    return (width, height) if width and height else None


def _tracked_visual_assets(tracked: list[str]) -> set[str]:
    assets: set[str] = set()
    for rel in tracked:
        lowered = rel.lower()
        if rel.startswith("docs/assets/") and lowered.endswith((".png", ".svg")):
            assets.add(rel)
        elif lowered.endswith(".png") and any(rel.startswith(prefix) for prefix in RUNTIME_IMAGE_DIRS):
            assets.add(rel)
    return assets


def _check_asset_provenance(
    root: Path,
    tracked: list[str],
    values: dict[Path, object],
    report: Report,
) -> None:
    provenance_path = root / "docs" / "assets" / "provenance.json"
    if not provenance_path.is_file():
        report.add("docs/assets/provenance.json: required provenance manifest is missing")
        return
    if provenance_path not in values:
        return
    document = values[provenance_path]
    if not isinstance(document, dict) or not isinstance(document.get("assets"), list):
        report.add("docs/assets/provenance.json: root must contain an assets list")
        return

    declared: dict[str, dict[str, object]] = {}
    for index, entry in enumerate(document["assets"]):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            report.add(f"docs/assets/provenance.json: asset {index} needs a path")
            continue
        rel, path = _safe_provenance_path(root, entry["path"])
        item_label = f"docs/assets/provenance.json: asset {index} ({_brief(entry['path'])})"
        if rel is None or path is None:
            report.add(f"{item_label}: path must be a safe normalized repository-relative path")
            continue
        if rel in declared:
            report.add(f"{item_label}: duplicate provenance path")
            continue
        declared[rel] = entry
        if not str(entry.get("purpose") or "").strip():
            report.add(f"{item_label}: purpose must be nonempty")
        source_truth = entry.get("source_truth")
        if not isinstance(source_truth, list) or not source_truth:
            report.add(f"{item_label}: source_truth must be a nonempty list")
        if not rel.lower().endswith(".png"):
            if not path.is_file():
                report.add(f"{item_label}: listed asset does not exist")
            continue
        sha256 = entry.get("sha256")
        if not isinstance(sha256, str) or SHA256_RE.fullmatch(sha256) is None:
            report.add(f"{item_label}: sha256 must be exactly 64 lowercase hexadecimal characters")
        if not path.is_file():
            report.add(f"{item_label}: listed PNG does not exist")
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            report.add(f"{item_label}: cannot read listed PNG: {type(exc).__name__}")
            continue
        actual_sha = hashlib.sha256(data).hexdigest()
        if isinstance(sha256, str) and SHA256_RE.fullmatch(sha256) and sha256 != actual_sha:
            report.add(f"{item_label}: sha256 does not match file bytes")
        dimensions = _ihdr_dimensions(data)
        if dimensions is None:
            report.add(f"{item_label}: file has no valid PNG IHDR")
            continue
        width, height = entry.get("width"), entry.get("height")
        if type(width) is not int or type(height) is not int or (width, height) != dimensions:
            report.add(
                f"{item_label}: width/height {width!r}x{height!r} do not match "
                f"IHDR {dimensions[0]}x{dimensions[1]}"
            )
        if entry.get("provenance_class") not in PNG_PROVENANCE_CLASSES:
            report.add(f"{item_label}: invalid provenance_class")
        if entry.get("synthetic") is not True:
            report.add(f"{item_label}: synthetic must be true")
        for field_name in ("capture_method", "viewport_or_device"):
            if not str(entry.get(field_name) or "").strip():
                report.add(f"{item_label}: {field_name} must be nonempty")
        epoch = entry.get("source_date_epoch")
        has_epoch = type(epoch) is int and epoch >= 0
        has_fixture = bool(str(entry.get("deterministic_fixture") or "").strip())
        if has_epoch == has_fixture:
            report.add(f"{item_label}: exactly one reproducibility anchor is required")

    actual = _tracked_visual_assets(tracked)
    for rel in sorted(actual - set(declared)):
        report.add(f"{rel}: tracked visual asset has no provenance entry")
    for rel in sorted(set(declared) - actual):
        report.add(f"{rel}: provenance entry does not name a tracked visual asset")


def _has_commit(root: Path) -> bool:
    """Whether ``root`` has a HEAD commit to compare the worktree against."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _load_doc_deletion_guard(root: Path):
    """The custody guard module, importable from a source checkout too.

    The guard lives in the package. CI installs the package before running this
    tool, but a maintainer running ``python tools/validate_documentation.py`` in
    a fresh clone has not, and a validator that only works after ``pip install``
    is a validator nobody runs first. The module rather than one function,
    because the archive prefix it classifies against is configurable and the
    remediation sentence must name the prefix actually in force.
    """
    try:
        from coordharness.lints import doc_deletion_guard
    except ImportError:
        source_root = str(root / "src")
        if source_root not in sys.path:
            sys.path.insert(0, source_root)
        from coordharness.lints import doc_deletion_guard
    return doc_deletion_guard


def _check_doc_custody(root: Path, report: Report) -> None:
    """A protected document must not leave the tree without a surviving copy.

    Deletion is the one documentation defect the checks above cannot see: they
    validate what is present, and a document that is gone is absent from every
    link, index and manifest they read. The guard diffs the worktree against
    HEAD and classifies each removed ``docs/*.md``.

    Only ``unpreserved_deletion`` -- removed with no copy surviving anywhere in
    the tree, by digest or by an 0.85 line-similarity match on the same file
    name -- is an error, because that is the case where content was actually
    lost. A document that moved to a non-archive path is reported as a note: the
    content still exists, and failing a rename would teach maintainers to skip
    this tool rather than to preserve their documents.
    """
    if not _has_commit(root):
        # Nothing can have been deleted from a history that does not exist yet.
        return
    try:
        guard = _load_doc_deletion_guard(root)
    except ImportError as exc:
        report.add(f"repository: documentation custody guard is not importable: {exc}")
        return
    try:
        audit = guard.build_audit(root)
    except Exception as exc:  # noqa: BLE001 - a guard that cannot run must not read as clean
        report.add(
            f"repository: documentation custody guard failed to run: {type(exc).__name__}: {exc}"
        )
        return
    for finding in audit.get("findings") or []:
        source = str(finding.get("source") or "unknown")
        recovery = str(finding.get("recovery_commit") or "unknown")
        if finding.get("disposition") == "unpreserved_deletion":
            report.add(
                f"{source}: protected documentation was deleted and no copy survives; "
                f"restore it from {recovery} and move it under "
                f"{guard.ARCHIVE_PREFIX} instead of hard-deleting it"
            )
            continue
        surviving = ", ".join(str(path) for path in finding.get("surviving_paths") or [])
        report.note(
            f"{source}: protected documentation left its path; content survives at "
            f"{surviving or 'an unrecorded path'} (recover the original from {recovery})"
        )


def validate(root: Path) -> Report:
    root = root.resolve()
    report = Report()
    tracked = _tracked_paths(root, report)
    if tracked is None:
        return report
    _check_doc_custody(root, report)
    _check_links(root, tracked, report)
    _check_prose_citations(root, tracked, report)
    values = _load_json(root, tracked, report)
    _check_svgs(root, tracked, report)
    _check_asset_provenance(root, tracked, values, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", help="repository root (default: current directory)")
    args = parser.parse_args(argv)
    report = validate(Path(args.root))
    for note in report.notes:
        print(f"NOTE {note}", file=sys.stderr)
    if not report.total:
        print(SUCCESS)
        return 0
    print(
        f"documentation validation failed: {report.total} issue(s); "
        f"showing {len(report.messages)}",
        file=sys.stderr,
    )
    for message in report.messages:
        print(f"ERROR {message}", file=sys.stderr)
    suppressed = report.total - len(report.messages)
    if suppressed:
        print(f"... {suppressed} additional issue(s) suppressed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
