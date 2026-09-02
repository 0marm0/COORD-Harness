"""Hermetic publication gate for the public repository.

Default CI uses only structural rules committed in this module. Maintainers may
add source-specific rules with ``--vocabulary PATH``; ignored local vocabulary
files are never auto-discovered. The publication inventory is Git's index, not a
permissive filesystem walk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import subprocess
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import port  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
INFRASTRUCTURE = {
    ".gitignore",
    "LICENSE",
    "pyproject.toml",
    "tools/extract/port.py",
    "tools/extract/gate.py",
    "tools/extract/manifest.json",
    "tools/extract/authored.json",
    "tools/extract/port_receipt.json",
    "tools/extract/vocabulary.example.json",
}
RULE_DEFINITION_PATHS = {
    "tools/extract/gate.py",
    "tools/extract/port.py",
    "tools/extract/vocabulary.example.json",
}
BINARY_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".a",
    ".o",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".duckdb",
    ".parquet",
    ".pth",
    ".safetensors",
    ".bin",
    ".joblib",
    ".pkl",
    ".zip",
    ".gz",
    ".tar",
    ".xz",
    ".bz2",
    ".whl",
    ".dmg",
    ".pdf",
    ".jpg",
    ".jpeg",
    ".webp",
}
SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "node_modules",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "DerivedData",
}
MAX_BYTES = 512 * 1024
MAX_IMAGE_BYTES = 1_200 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_DECOMPRESSED_IMAGE_BYTES = 128 * 1024 * 1024
IMAGE_DIR = "docs/assets/"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
FORBIDDEN_PNG_METADATA = {b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME", b"iCCP"}
PNG_PROVENANCE_CLASSES = {
    "synthetic-web-capture",
    "clean-room-native-capture",
    # Artwork the maintainer supplies rather than a capture of the running UI.
    # Held to the same declaration -- hash, dimensions, what it is used for --
    # so an image cannot arrive without a statement of where it came from.
    "maintainer-artwork",
}
# Directories a runtime PNG may live in. Marks are loaded by the apps at run
# time, so confining every image to the documentation tree would mean the
# artwork could not ship at all. Each directory is named rather than globbed:
# the point of the rule is that an image cannot appear somewhere unconsidered.
RUNTIME_IMAGE_DIRS = (
    "apps/brand/Assets/",
    "apps/brand/Assets.xcassets/",
    "src/coordharness/board/static/",
)
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
# A boundary-free scan is a plain substring search, so a short token matches
# ordinary English and ordinary code: a two- or three-character source name
# fires inside unrelated words on nearly every line, and a check people learn to
# wave through is worse than no check at all. Four characters is the shortest
# length at which a project-specific token is likelier than a coincidence.
# Shorter tokens stay with the `forbidden` rules, which can carry the context
# that makes them precise.
MIN_SOURCE_TOKEN_LENGTH = 4
# Characters that end a literal run because what follows them is matched by
# structure rather than verbatim.
_LITERAL_BREAKS = frozenset(".^$|)]}")
_QUANTIFIERS = frozenset("*+?")
COMMIT_RE = re.compile(r"[0-9a-f]{40,64}\Z")
IDENTITY_HEADER_RE = re.compile(
    r"(?P<name>.*) <(?P<email>[^<>]*)> -?\d+ [+-]\d{4}\Z"
)


@dataclass(frozen=True)
class IndexedPath:
    rel: str
    mode: str
    oid: str
    stage: int


@dataclass
class Report:
    files_scanned: int = 0
    coverage: list[str] = field(default_factory=list)
    fidelity: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    shape: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)

    @property
    def failures(self) -> int:
        return sum(
            map(len, (self.coverage, self.fidelity, self.patterns, self.shape, self.history))
        )


def _git(root: Path, args: Sequence[str], *, text: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=text, check=False)


def _safe_rel(value: object, *, label: str) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return None, f"{label}: path must be a non-empty normalized relative path"
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        return None, f"{label}: absolute or traversal path is forbidden"
    if pure.as_posix() != value:
        return None, f"{label}: path is not normalized"
    return value, None


def _load_json(path: Path, failures: list[str], label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        failures.append(f"{label}: missing or invalid JSON")
        return {}
    if not isinstance(value, dict):
        failures.append(f"{label}: root must be an object")
        return {}
    return value


def indexed_paths(root: Path, report: Report) -> list[IndexedPath]:
    result = _git(root, ["ls-files", "--stage", "-z"])
    if result.returncode:
        report.shape.append("repository: unable to inventory Git index")
        return []
    entries: list[IndexedPath] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            meta, path_raw = raw.split(b"\t", 1)
            mode_raw, oid_raw, stage_raw = meta.split(b" ", 2)
            rel = path_raw.decode("utf-8")
            mode, oid, stage = mode_raw.decode(), oid_raw.decode(), int(stage_raw)
        except (ValueError, UnicodeDecodeError):
            report.shape.append("repository: malformed or non-UTF-8 Git index entry")
            continue
        normalized, error = _safe_rel(rel, label="index")
        if error:
            report.shape.append(error)
        else:
            entries.append(IndexedPath(normalized, mode, oid, stage))
    return entries


def _indexed_blobs(
    root: Path,
    entries: Iterable[IndexedPath],
    report: Report,
) -> dict[str, bytes]:
    blobs: dict[str, bytes] = {}
    for entry in entries:
        if entry.stage or entry.mode not in {"100644", "100755"}:
            continue
        result = _git(root, ["cat-file", "blob", entry.oid])
        if result.returncode:
            report.shape.append(f"{entry.rel}: indexed blob is unreadable")
            continue
        blobs[entry.rel] = result.stdout
    return blobs


def _load_index_json(
    blobs: dict[str, bytes],
    rel: str,
    failures: list[str],
    label: str,
) -> dict:
    try:
        value = json.loads(blobs[rel].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError):
        failures.append(f"{label}: missing or invalid staged JSON")
        return {}
    if not isinstance(value, dict):
        failures.append(f"{label}: root must be an object")
        return {}
    return value


def _png_dimensions(data: bytes) -> tuple[tuple[int, int] | None, str | None]:
    if not data.startswith(PNG_SIGNATURE):
        return None, "invalid PNG signature"
    offset, dimensions, saw_idat, saw_iend = len(PNG_SIGNATURE), None, False, False
    chunks: list[bytes] = []
    image_payloads: list[bytes] = []
    while offset < len(data):
        if saw_iend:
            return None, "PNG has trailing bytes after IEND"
        if len(data) - offset < 12:
            return None, "truncated PNG chunk"
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(data):
            return None, "truncated PNG chunk payload"
        payload = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length : end])[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            return None, "PNG chunk CRC mismatch"
        if saw_iend:
            return None, "PNG has trailing bytes after IEND"
        chunks.append(kind)
        if kind in FORBIDDEN_PNG_METADATA:
            return None, f"PNG contains forbidden metadata chunk {kind.decode('ascii', 'replace')}"
        if kind == b"IHDR":
            if dimensions is not None or len(chunks) != 1 or length != 13:
                return None, "invalid PNG IHDR"
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
            if (
                width <= 0
                or height <= 0
                or width * height > MAX_IMAGE_PIXELS
                or color not in depths
                or depth not in depths[color]
                or compression
                or filtering
                or interlace not in {0, 1}
            ):
                return None, "invalid PNG dimensions or IHDR parameters"
            dimensions = (width, height)
        elif kind == b"IDAT":
            if dimensions is None:
                return None, "PNG IDAT precedes IHDR"
            saw_idat = True
            image_payloads.append(payload)
        elif kind == b"IEND":
            if length or not saw_idat:
                return None, "invalid PNG IEND"
            saw_iend = True
        offset = end
    if not saw_iend or dimensions is None or offset != len(data):
        return None, "PNG is missing IHDR/IEND or has trailing bytes"
    try:
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(b"".join(image_payloads), MAX_DECOMPRESSED_IMAGE_BYTES + 1)
        if len(decoded) > MAX_DECOMPRESSED_IMAGE_BYTES or not decoder.eof or decoder.unused_data:
            return None, "invalid or oversized compressed PNG image data"
    except zlib.error:
        return None, "invalid compressed PNG image data"
    return dimensions, None


def _png_provenance(blobs: dict[str, bytes], report: Report) -> dict[str, dict]:
    data = _load_index_json(
        blobs,
        "docs/assets/provenance.json",
        report.shape,
        "docs/assets/provenance.json",
    )
    assets = data.get("assets", [])
    if not isinstance(assets, list):
        report.shape.append("docs/assets/provenance.json: assets must be a list")
        return {}
    output: dict[str, dict] = {}
    for index, item in enumerate(assets):
        if not isinstance(item, dict):
            report.shape.append(f"provenance entry {index}: must be an object")
            continue
        rel, error = _safe_rel(item.get("path"), label=f"provenance entry {index}")
        if error:
            report.shape.append(error)
            continue
        if not rel.endswith(".png"):
            continue
        if rel in output:
            report.shape.append(f"{rel}: duplicate PNG provenance entry")
            continue
        if item.get("provenance_class") not in PNG_PROVENANCE_CLASSES:
            report.shape.append(f"{rel}: invalid PNG provenance_class")
        if not isinstance(item.get("sha256"), str) or not SHA256_RE.fullmatch(item["sha256"]):
            report.shape.append(f"{rel}: PNG provenance requires full lowercase sha256")
        for key in ("capture_method", "viewport_or_device"):
            if not isinstance(item.get(key), (str, dict)) or not item[key]:
                report.shape.append(f"{rel}: PNG provenance requires {key}")
        if item.get("synthetic") is not True:
            report.shape.append(f"{rel}: PNG provenance requires synthetic=true")
        if not isinstance(item.get("source_truth"), list) or not item["source_truth"]:
            report.shape.append(f"{rel}: PNG provenance requires nonempty source_truth")
        else:
            for source_index, source_path in enumerate(item["source_truth"]):
                _, source_error = _safe_rel(source_path, label=f"{rel} source_truth {source_index}")
                if source_error:
                    report.shape.append(source_error)
        width, height = item.get("width"), item.get("height")
        if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
            report.shape.append(f"{rel}: PNG provenance requires positive integer width/height")
        epoch_ok = type(item.get("source_date_epoch")) is int and item["source_date_epoch"] >= 0
        fixture_ok = isinstance(item.get("deterministic_fixture"), str) and bool(
            item["deterministic_fixture"]
        )
        if fixture_ok:
            _, fixture_error = _safe_rel(
                item["deterministic_fixture"], label=f"{rel} deterministic_fixture"
            )
            if fixture_error:
                report.shape.append(fixture_error)
        if epoch_ok == fixture_ok:
            report.shape.append(
                f"{rel}: PNG provenance requires exactly one reproducibility anchor"
            )
        output[rel] = item
    return output


def check_shape(
    entries: list[IndexedPath],
    blobs: dict[str, bytes],
    report: Report,
) -> None:
    pngs = [entry for entry in entries if entry.rel.lower().endswith(".png")]
    provenance = _png_provenance(blobs, report) if pngs else {}
    tracked_pngs = {entry.rel for entry in pngs}
    for stale in sorted(set(provenance) - tracked_pngs):
        report.shape.append(f"{stale}: PNG provenance entry has no tracked PNG")
    for entry in entries:
        rel = entry.rel
        if entry.stage:
            report.shape.append(f"{rel}: unmerged index stage {entry.stage}")
            continue
        if any(part in SKIP_DIRS for part in PurePosixPath(rel).parts):
            report.shape.append(f"{rel}: tracked content under skipped/cache directory")
        if entry.mode not in {"100644", "100755"}:
            kind = {"120000": "symlink", "160000": "submodule"}.get(entry.mode, "non-regular mode")
            report.shape.append(f"{rel}: tracked {kind} ({entry.mode})")
            continue
        data = blobs.get(rel)
        if data is None:
            continue
        suffix = PurePosixPath(rel).suffix.lower()
        if suffix == ".png":
            allowed = (IMAGE_DIR, *RUNTIME_IMAGE_DIRS)
            if not rel.startswith(allowed):
                report.shape.append(
                    f"{rel}: PNG files are only allowed under {', '.join(allowed)}"
                )
                continue
            if len(data) > MAX_IMAGE_BYTES:
                report.shape.append(f"{rel}: image exceeds {MAX_IMAGE_BYTES} bytes")
                continue
            dimensions, png_error = _png_dimensions(data)
            if png_error:
                report.shape.append(f"{rel}: {png_error}")
                continue
            declared = provenance.get(rel)
            if not declared:
                report.shape.append(f"{rel}: missing exact PNG provenance entry")
                continue
            if hashlib.sha256(data).hexdigest() != declared.get("sha256"):
                report.shape.append(f"{rel}: PNG SHA-256 differs from provenance")
            if dimensions != (declared.get("width"), declared.get("height")):
                report.shape.append(f"{rel}: PNG dimensions differ from provenance")
            continue
        if suffix in BINARY_SUFFIXES:
            report.shape.append(f"{rel}: binary/bytecode suffix {suffix}")
        elif len(data) > MAX_BYTES:
            report.shape.append(f"{rel}: {len(data)} bytes exceeds {MAX_BYTES}")
        else:
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                report.shape.append(f"{rel}: not readable UTF-8 text")


def _classifications(
    manifest: dict,
    authored: dict,
    blobs: dict[str, bytes],
    report: Report,
) -> tuple[set[str], set[str], set[str]]:
    ported: set[str] = set()
    items = manifest.get("files", [])
    if not isinstance(items, list):
        report.coverage.append("manifest.json: files must be a list")
        items = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            report.coverage.append(f"manifest entry {index}: must be an object")
            continue
        rel, error = _safe_rel(item.get("dest"), label=f"manifest entry {index}")
        if error:
            report.coverage.append(error)
        elif rel in ported:
            report.coverage.append(f"{rel}: duplicate public manifest destination")
        else:
            ported.add(rel)
    raw_authored = authored.get("files", {})
    if not isinstance(raw_authored, dict):
        report.coverage.append("authored.json: files must be an object")
        raw_authored = {}
    written: set[str] = set()
    for value, reason in raw_authored.items():
        rel, error = _safe_rel(value, label="authored.json files")
        if error:
            report.coverage.append(error)
        elif not isinstance(reason, str) or not reason.strip():
            report.coverage.append(f"{rel}: authored entry requires a reason")
        else:
            written.add(rel)
    raw_derived = authored.get("derived", {})
    if not isinstance(raw_derived, dict):
        report.coverage.append("authored.json: derived must be an object")
        raw_derived = {}
    derived: set[str] = set()
    for value, metadata in raw_derived.items():
        rel, error = _safe_rel(value, label="authored.json derived")
        if error:
            report.coverage.append(error)
            continue
        derived.add(rel)
        if not isinstance(metadata, dict):
            report.coverage.append(f"{rel}: derived entry must be an object")
            continue
        for key in ("origin", "reason"):
            if not isinstance(metadata.get(key), str) or not metadata[key].strip():
                report.coverage.append(f"{rel}: derived entry requires {key}")
        reviewed = metadata.get("reviewed_sha256")
        if not isinstance(reviewed, str) or not SHA256_RE.fullmatch(reviewed):
            report.coverage.append(f"{rel}: derived entry requires full reviewed_sha256")
        else:
            blob = blobs.get(rel)
            if blob is not None and hashlib.sha256(blob).hexdigest() != reviewed:
                report.coverage.append(f"{rel}: content differs from reviewed_sha256")
    for left_name, left, right_name, right in (
        ("manifest", ported, "authored", written),
        ("manifest", ported, "derived", derived),
        ("authored", written, "derived", derived),
    ):
        for rel in sorted(left & right):
            report.coverage.append(f"{rel}: overlaps {left_name} and {right_name} classifications")
    return ported, written, derived


def check_coverage(
    entries: list[IndexedPath],
    blobs: dict[str, bytes],
    manifest: dict,
    authored: dict,
    report: Report,
) -> None:
    ported, written, derived = _classifications(manifest, authored, blobs, report)
    accounted = INFRASTRUCTURE | ported | written | derived
    tracked = {entry.rel for entry in entries}
    for entry in entries:
        if entry.rel not in accounted:
            report.coverage.append(f"{entry.rel}: unaccounted tracked file")
    for class_name, declared in (("manifest", ported), ("authored", written), ("derived", derived)):
        for rel in sorted(declared - tracked):
            report.coverage.append(f"{rel}: {class_name} declaration is not tracked")


def _pinned_source_blob(
    source_root: Path,
    commit: str,
    rel: str,
) -> tuple[bytes | None, str | None]:
    """Read one exact regular-file blob from a pinned commit, never the worktree."""
    tree = _git(
        source_root,
        [
            "--literal-pathspecs",
            "ls-tree",
            "-z",
            "--full-tree",
            commit,
            "--",
            rel,
        ],
    )
    if tree.returncode:
        return None, "pinned source tree is unreadable"
    records = [record for record in tree.stdout.split(b"\0") if record]
    if len(records) != 1:
        return None, "pinned source path is absent"
    try:
        meta, raw_path = records[0].split(b"\t", 1)
        mode, kind, oid = meta.decode("ascii").split(" ", 2)
        pinned_rel = raw_path.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None, "pinned source entry is malformed or undecodable"
    if pinned_rel != rel:
        return None, "pinned source path did not resolve exactly"
    if mode == "120000":
        return None, "pinned source entry is a symlink"
    if kind != "blob" or mode not in {"100644", "100755"}:
        return None, f"pinned source entry is non-regular ({mode} {kind})"
    blob = _git(source_root, ["cat-file", "blob", oid])
    if blob.returncode:
        return None, "pinned source blob is unreadable"
    return blob.stdout, None


def check_fidelity(
    root: Path,
    public: dict,
    source_manifest: dict | None,
    source_root: Path | None,
    entries_by_rel: dict[str, IndexedPath],
    blobs: dict[str, bytes],
    report: Report,
    *,
    renames,
    forbidden,
) -> None:
    if source_root is None:
        return
    if source_root.is_symlink() or not source_root.is_dir():
        report.fidelity.append("source root: missing, non-directory, or symlink")
        return
    source_resolved, root_resolved = source_root.resolve(strict=True), root.resolve(strict=True)
    if (
        source_resolved == root_resolved
        or source_resolved.is_relative_to(root_resolved)
        or root_resolved.is_relative_to(source_resolved)
    ):
        report.fidelity.append("source root: source and destination overlap")
        return
    if not source_manifest:
        report.fidelity.append("source manifest: required with --source-root")
        return
    snapshot = source_manifest.get("source_snapshot")
    expected_commit = snapshot.get("commit") if isinstance(snapshot, dict) else None
    if not isinstance(expected_commit, str) or not COMMIT_RE.fullmatch(expected_commit):
        report.fidelity.append("source manifest: missing exact source_snapshot.commit")
        return
    pinned = _git(
        source_root, ["rev-parse", "--verify", f"{expected_commit}^{{commit}}"], text=True
    )
    if pinned.returncode or pinned.stdout.strip() != expected_commit:
        report.fidelity.append(
            "source root: source_snapshot.commit is absent or not an exact commit"
        )
        return
    # Every comparison below reads blobs at `expected_commit`, so the source
    # checkout does not need to be parked there -- and demanding it be parked
    # there meant the check could only run when the source repository happened
    # to be idle, which for one under active development is close to never.
    #
    # What is worth verifying is that the pin is a real committed state rather
    # than a loose object that was created and abandoned: a dangling commit can
    # be fabricated to match whatever the tree happens to contain.
    # `rev-parse --verify` above is satisfied by a dangling commit, which is
    # measured, not assumed: a commit that has been reset away still resolves.
    # Reachability from a ref is what separates a real published state from an
    # object someone produced to match whatever the tree happens to contain.
    named = _git(
        source_root,
        ["for-each-ref", "--contains", expected_commit, "--count=1", "--format=%(refname)"],
        text=True,
    )
    if named.returncode or not named.stdout.strip():
        report.fidelity.append(
            "source root: source_snapshot.commit is not reachable from any ref"
        )
    public_dests = {item.get("dest") for item in public.get("files", []) if isinstance(item, dict)}
    private_items = source_manifest.get("files", [])
    if not isinstance(private_items, list):
        report.fidelity.append("source manifest: files must be a list")
        return
    seen: set[str] = set()
    for index, item in enumerate(private_items):
        if not isinstance(item, dict):
            report.fidelity.append(f"source manifest entry {index}: must be an object")
            continue
        src_rel, src_error = _safe_rel(item.get("source"), label=f"source entry {index} source")
        dest_rel, dest_error = _safe_rel(item.get("dest"), label=f"source entry {index} dest")
        if src_error or dest_error:
            report.fidelity.extend(error for error in (src_error, dest_error) if error)
            continue
        if dest_rel in seen:
            report.fidelity.append(f"{dest_rel}: duplicate source-manifest destination")
            continue
        seen.add(dest_rel)
        if dest_rel not in public_dests:
            report.fidelity.append(f"{dest_rel}: absent from public manifest")
        source_blob, source_error = _pinned_source_blob(source_root, expected_commit, src_rel)
        if source_error:
            report.fidelity.append(f"{dest_rel}: {source_error}")
            continue
        dest_entry = entries_by_rel.get(dest_rel)
        dest_blob = blobs.get(dest_rel)
        if (
            dest_entry is None
            or dest_entry.stage
            or dest_entry.mode not in {"100644", "100755"}
            or dest_blob is None
        ):
            report.fidelity.append(f"{dest_rel}: staged destination is missing or non-regular")
            continue
        try:
            source_text = source_blob.decode("utf-8")
        except UnicodeDecodeError:
            report.fidelity.append(f"{dest_rel}: pinned source blob is not UTF-8 text")
            continue
        try:
            dest_text = dest_blob.decode("utf-8")
        except UnicodeDecodeError:
            report.fidelity.append(f"{dest_rel}: staged destination is not UTF-8 text")
            continue
        try:
            expected, result = port.port_text(
                source_text,
                dest=Path(dest_rel),
                is_python=PurePosixPath(src_rel).suffix == ".py",
                drop=item.get("drop_symbols", ()),
                edits=item.get("edits", ()),
                renames=renames,
                forbidden=forbidden,
            )
        except (ValueError, KeyError) as exc:
            report.fidelity.append(f"{dest_rel}: porter reproduction failed ({type(exc).__name__})")
            continue
        if result.violations:
            report.fidelity.append(
                f"{dest_rel}: porter reports {len(result.violations)} violation(s)"
            )
        elif dest_text != expected:
            report.fidelity.append(f"{dest_rel}: differs from fresh port of pinned source")
    for missing in sorted(public_dests - seen):
        if isinstance(missing, str):
            report.fidelity.append(f"{missing}: absent from source manifest")



def _is_probably_text(data: bytes) -> bool:
    """True when a blob is text a reviewer could read.

    A NUL byte, or a decode that fails outright, means the bytes were never
    prose; scanning them for prose-shaped patterns produces findings that no
    edit can clear.
    """
    if b"\x00" in data[:8192]:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True

def _scan_text(
    text: str, identity: str, forbidden, output: list[str], *, neutral_noreply: bool = False
) -> None:
    for line_no, line in enumerate(text.splitlines(), 1):
        for pattern, reason in forbidden:
            matches = list(pattern.finditer(line))
            if neutral_noreply and reason == "email address":
                matches = [
                    match for match in matches if not port.is_neutral_noreply(match.group(0))
                ]
            if matches:
                output.append(f"{identity}:{line_no}: {reason}")


def check_patterns(
    entries: Iterable[IndexedPath],
    blobs: dict[str, bytes],
    report: Report,
    forbidden,
    *,
    extra_excluded: set[str],
) -> None:
    excluded = RULE_DEFINITION_PATHS | extra_excluded
    for entry in entries:
        if entry.rel in excluded or entry.mode not in {"100644", "100755"}:
            continue
        data = blobs.get(entry.rel)
        if data is None:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        _scan_text(text, entry.rel, forbidden, report.patterns)


def _literal_runs(pattern: str) -> list[str]:
    r"""Reduce a rename pattern to the substrings it matches verbatim.

    Escaped punctuation is unescaped (``\.`` is a dot); zero-width and class
    escapes end a run rather than contributing a character (``\b`` is a
    boundary, ``\d`` is a class); character classes, groups, alternation and
    quantified atoms end a run too. A pattern yields only what it concretely
    names -- never an approximation of it, because a guessed token would fail
    the gate on text the vocabulary never mentioned.
    """
    runs: list[str] = []
    current: list[str] = []
    index, length = 0, len(pattern)

    def flush() -> None:
        if current:
            runs.append("".join(current))
            current.clear()

    while index < length:
        char = pattern[index]
        index += 1
        if char == "\\":
            if index >= length:
                break
            escaped = pattern[index]
            index += 1
            if escaped.isalnum():
                flush()
            else:
                current.append(escaped)
        elif char == "[":
            flush()
            if index < length and pattern[index] == "^":
                index += 1
            if index < length and pattern[index] == "]":
                index += 1
            while index < length and pattern[index] != "]":
                index += 2 if pattern[index] == "\\" else 1
            index += 1
        elif char == "(":
            flush()
            if index < length and pattern[index] == "?":
                # Consume the group prefix -- `?:`, `?=`, `?!`, `?<=`,
                # `?P<name>`, `?i)` -- up to the character opening the body.
                while index < length and pattern[index] not in ":=!>)":
                    index += 1
                index += 1
        elif char == "{":
            if current:
                current.pop()
            flush()
            while index < length and pattern[index] != "}":
                index += 1
            index += 1
        elif char in _QUANTIFIERS:
            # The quantified atom is optional or repeated, so it is not text
            # the pattern is guaranteed to match.
            if current:
                current.pop()
            flush()
        elif char in _LITERAL_BREAKS:
            flush()
        else:
            current.append(char)
    flush()
    return runs


@dataclass(frozen=True)
class SourceToken:
    """One case-folded token a rename rule names, with that rule's index."""

    index: int
    text: str


def source_tokens(renames: Sequence[tuple[str, str]]) -> tuple[SourceToken, ...]:
    """Derive the concrete source tokens a rename vocabulary names."""
    candidates: list[SourceToken] = []
    seen: set[str] = set()
    for index, rule in enumerate(renames):
        for run in _literal_runs(rule[0]):
            folded = run.casefold()
            if len(folded) < MIN_SOURCE_TOKEN_LENGTH or folded in seen:
                continue
            seen.add(folded)
            candidates.append(SourceToken(index, folded))
    # A token containing a shorter token can only occur where the shorter one
    # does, so scanning the shorter one alone is equivalent -- and it keeps a
    # single leak from being reported once per rule that spells it.
    return tuple(
        token
        for token in candidates
        if not any(
            other.text != token.text and other.text in token.text for other in candidates
        )
    )


def check_source_tokens(
    entries: Iterable[IndexedPath],
    blobs: dict[str, bytes],
    report: Report,
    tokens: Sequence[SourceToken],
    *,
    extra_excluded: set[str],
) -> None:
    r"""Assert no source token survives in tracked text, boundary or not.

    The `forbidden` rules are regexes, and a regex for a name is almost always
    written `\bname\b`. Neither an underscore nor a capital letter is a word
    boundary, so `_name`, `name_`, `innamed` and `NAMESpec` all pass a table
    that has a rule for the bare word -- the same miss escaped four times, each
    time past a rule that was already there. This asks the one question with no
    boundary in it: does the token occur at all?

    A failure names the file, the line, and the token's index in the
    vocabulary, and never the token itself: reporting what you redact discloses
    it, and this output is read where the source name must not reach. The files
    that define the rules are excluded, which is also what keeps the shipped
    `vocabulary.example.json` placeholders from failing their own check.
    """
    if not tokens:
        return
    excluded = RULE_DEFINITION_PATHS | extra_excluded
    for entry in entries:
        if entry.rel in excluded or entry.mode not in {"100644", "100755"}:
            continue
        data = blobs.get(entry.rel)
        if data is None:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            folded = line.casefold()
            for token in tokens:
                if token.text in folded:
                    report.patterns.append(
                        f"{entry.rel}:{line_no}: source token {token.index} "
                        "occurs (boundary-free match)"
                    )
                    break


#: A git trailer line: ``Key: value`` where the key is a single hyphenated token.
_TRAILER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:[ \t].+$")


def _split_trailers(message: str) -> tuple[str, str]:
    """Split a commit message into its body and its trailing ``Key: value`` block.

    Only a contiguous run of trailer lines at the very end counts, which is what
    ``git interpret-trailers`` recognises. A message with no such run returns
    unchanged, so nothing is reclassified out of the strict body by accident.
    """
    lines = message.rstrip("\n").split("\n")
    index = len(lines)
    while index > 0 and _TRAILER_RE.match(lines[index - 1]):
        index -= 1
    if index == len(lines) or index == 0:
        return message, ""
    return "\n".join(lines[:index]), "\n".join(lines[index:])


def _commit_metadata_fields(
    data: bytes, commit: str, report: Report
) -> tuple[tuple[str, str, bool], ...]:
    """Extract only the identity fields and full message from a commit object."""
    headers, separator, message = data.partition(b"\n\n")
    if not separator:
        report.history.append(f"history commit {commit}: malformed commit object")
        return ()
    identities: dict[bytes, str] = {}
    for line in headers.splitlines():
        if line.startswith(b" "):
            continue
        key, found, value = line.partition(b" ")
        if found and key in {b"author", b"committer"} and key not in identities:
            identities[key] = value.decode("utf-8", "replace")

    fields: list[tuple[str, str, bool]] = []
    for header, label in ((b"author", "author"), (b"committer", "committer")):
        match = IDENTITY_HEADER_RE.fullmatch(identities.get(header, ""))
        if match is None:
            report.history.append(f"history commit {commit}: malformed {label} header")
            continue
        fields.extend(
            (
                (f"{label} name", match.group("name"), False),
                (f"{label} email", match.group("email"), True),
            )
        )
    # A commit's trailer block is structured identity metadata -- the same kind of
    # thing as the author and committer headers -- not prose. Scanning it with the
    # neutral-noreply allowance OFF made every AI-co-authored commit fail on its own
    # `Co-Authored-By: ... <noreply@...>` line. The body stays strict, so a personal
    # address written into the message still fails; only exact service addresses pass.
    body, trailers = _split_trailers(message.decode("utf-8", "replace"))
    fields.append(("message", body, False))
    if trailers:
        fields.append(("message trailers", trailers, True))
    return tuple(fields)


#: The commit-header fields that carry a human's display name, as opposed to a
#: mailbox, a trailer block or prose. Only these are eligible for the approved
#: attribution allowance below.
_ATTRIBUTED_NAME_FIELDS = frozenset({"author name", "committer name"})


def _scan_history_field(
    text: str,
    commit: str,
    field_name: str,
    forbidden,
    tokens: Sequence[SourceToken],
    report: Report,
    *,
    neutral_noreply: bool,
    approved_identities: Sequence[str] = (),
) -> None:
    identity = f"history commit {commit} {field_name}"
    # A repository published under a person's own name has to carry that name in
    # its authorship, or the history is either anonymous or false. The operator
    # declaring an identity in the vocabulary is what makes it the approved
    # public attribution, and this is the only place that declaration applies:
    # the whole field must equal an approved string exactly, so a different
    # person whose name merely contains it -- the substring the privacy phrase
    # was written to catch -- still fails. Nothing else is widened. Email fields
    # keep their own, narrower neutral-noreply rule; the message body and trailer
    # block stay strict; and the same string inside a tracked file is still a
    # finding, because a name in file content is usually an absolute home path
    # or an unported source reference -- a different leak that no attribution
    # decision authorises.
    approved_name = (
        field_name in _ATTRIBUTED_NAME_FIELDS and text.strip() in tuple(approved_identities)
    )
    if not approved_name:
        for pattern, reason in forbidden:
            matches = list(pattern.finditer(text))
            if neutral_noreply and reason == "email address":
                matches = [
                    match for match in matches if not port.is_neutral_noreply(match.group(0))
                ]
            if matches:
                report.history.append(f"{identity}: {reason}")
    folded = text.casefold()
    for token in tokens:
        if token.text in folded:
            report.history.append(
                f"{identity}: source token {token.index} occurs (boundary-free match)"
            )


def _resolve_commit_ref(root: Path, ref: str) -> str | None:
    if not isinstance(ref, str) or not ref or "\x00" in ref or ref.startswith("-"):
        return None
    selected = _git(root, ["rev-parse", "--verify", f"{ref}^{{commit}}"], text=True)
    selected_commit = selected.stdout.strip()
    if selected.returncode or not COMMIT_RE.fullmatch(selected_commit):
        return None
    return selected_commit


def check_ref_index_identity(root: Path, report: Report, ref: str) -> str | None:
    """Bind an explicit ref to the exact candidate staged in the index."""
    selected_commit = _resolve_commit_ref(root, ref)
    if selected_commit is None:
        report.shape.append("candidate identity: selected ref is invalid or not a commit")
        return None
    selected_tree = _git(
        root, ["rev-parse", "--verify", f"{selected_commit}^{{tree}}"], text=True
    )
    index_tree = _git(root, ["write-tree"], text=True)
    selected_oid, index_oid = selected_tree.stdout.strip(), index_tree.stdout.strip()
    if (
        selected_tree.returncode
        or index_tree.returncode
        or not COMMIT_RE.fullmatch(selected_oid)
        or not COMMIT_RE.fullmatch(index_oid)
    ):
        report.shape.append("candidate identity: unable to compare ref and Git index trees")
    elif selected_oid != index_oid:
        report.shape.append("candidate identity: selected ref and Git index trees differ")
    return selected_commit


def check_history(
    root: Path,
    report: Report,
    forbidden,
    tokens: Sequence[SourceToken],
    *,
    ref: str,
    extra_excluded: set[str],
    selected_commit: str | None = None,
    approved_identities: Sequence[str] = (),
) -> None:
    if selected_commit is None:
        selected_commit = _resolve_commit_ref(root, ref)
        if selected_commit is None:
            report.history.append("history: selected ref is invalid or not a commit")
            return
    result = _git(root, ["rev-list", selected_commit], text=True)
    if result.returncode:
        report.history.append("history: unable to enumerate reachable commits")
        return
    scanned_blobs: set[str] = set()
    for commit in filter(None, result.stdout.splitlines()):
        commit_obj = _git(root, ["cat-file", "commit", commit])
        if commit_obj.returncode:
            report.history.append(f"history commit {commit}: unreadable")
        else:
            for field_name, text, neutral_noreply in _commit_metadata_fields(
                commit_obj.stdout, commit, report
            ):
                _scan_history_field(
                    text,
                    commit,
                    field_name,
                    forbidden,
                    tokens,
                    report,
                    neutral_noreply=neutral_noreply,
                    approved_identities=approved_identities,
                )
        tree = _git(root, ["ls-tree", "-r", "-z", "--full-tree", commit])
        if tree.returncode:
            report.history.append(f"history commit {commit[:12]}: tree unreadable")
            continue
        for record in tree.stdout.split(b"\0"):
            if not record:
                continue
            try:
                meta, raw_path = record.split(b"\t", 1)
                mode, kind, oid = meta.decode("ascii").split(" ")
                rel = raw_path.decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                report.history.append(f"history commit {commit[:12]}: malformed tree entry")
                continue
            if (
                kind != "blob"
                or mode not in {"100644", "100755"}
                or rel in RULE_DEFINITION_PATHS | extra_excluded
            ):
                continue
            _scan_text(rel, f"history path {commit[:12]}", forbidden, report.history)
            if oid in scanned_blobs:
                continue
            scanned_blobs.add(oid)
            blob = _git(root, ["cat-file", "blob", oid])
            if blob.returncode:
                report.history.append(f"history blob {oid[:12]}: unreadable")
            elif not _is_probably_text(blob.stdout):
                # Compressed image bytes match text patterns by coincidence: a
                # sanitized PNG raised "email address" on the two characters
                # either side of an @ inside its IDAT stream. Decoding binary
                # with errors="replace" and pattern-matching the result reports
                # the compressor's entropy, not a leak. Binary blobs are held to
                # the shape rules instead, which is where PNG provenance,
                # dimensions and hashes are already enforced.
                pass
            else:
                _scan_text(
                    blob.stdout.decode("utf-8", "replace"),
                    f"history blob {oid[:12]}",
                    forbidden,
                    report.history,
                )


def run(
    root: Path,
    source_root: Path | None = None,
    *,
    source_manifest_path: Path | None = None,
    vocabulary_path: Path | None = None,
    history: bool = False,
    ref: str | None = None,
) -> Report:
    report = Report()
    try:
        root = root.resolve(strict=True)
    except OSError:
        report.shape.append("repository root: missing or unresolvable")
        return report
    entries = indexed_paths(root, report)
    report.files_scanned = len(entries)
    blobs = _indexed_blobs(root, entries, report)
    selected_commit = (
        check_ref_index_identity(root, report, ref) if ref is not None else None
    )
    entries_by_rel = {entry.rel: entry for entry in entries if not entry.stage}
    manifest = _load_index_json(
        blobs, "tools/extract/manifest.json", report.coverage, "manifest.json"
    )
    authored = _load_index_json(
        blobs, "tools/extract/authored.json", report.coverage, "authored.json"
    )
    try:
        vocabulary = port.load_vocabulary(vocabulary_path) if vocabulary_path else {}
        renames, forbidden = port.compile_vocabulary(vocabulary)
        approved_identities = port.compile_approved_identities(vocabulary)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, re.error) as exc:
        report.patterns.append(f"vocabulary: invalid ({type(exc).__name__})")
        renames, forbidden = port.compile_vocabulary({})
        approved_identities = ()
    check_shape(entries, blobs, report)
    check_coverage(entries, blobs, manifest, authored, report)
    if source_manifest_path and (
        source_manifest_path.is_symlink() or not source_manifest_path.is_file()
    ):
        report.fidelity.append("source manifest: must be a regular non-symlink file")
        source_manifest = None
    else:
        source_manifest = (
            _load_json(source_manifest_path, report.fidelity, "source manifest")
            if source_manifest_path
            else None
        )
    check_fidelity(
        root,
        manifest,
        source_manifest,
        source_root,
        entries_by_rel,
        blobs,
        report,
        renames=renames,
        forbidden=forbidden,
    )
    excluded: set[str] = set()
    if vocabulary_path:
        try:
            resolved = vocabulary_path.resolve(strict=True)
            if resolved.is_relative_to(root):
                excluded.add(resolved.relative_to(root).as_posix())
        except OSError:
            pass
    check_patterns(entries, blobs, report, forbidden, extra_excluded=excluded)
    check_source_tokens(
        entries, blobs, report, source_tokens(renames), extra_excluded=excluded
    )
    if history and (ref is None or selected_commit is not None):
        check_history(
            root,
            report,
            forbidden,
            source_tokens(renames),
            ref=ref or "HEAD",
            extra_excluded=excluded,
            selected_commit=selected_commit,
            approved_identities=approved_identities,
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument(
        "--source-manifest", type=Path, help="private manifest with source_snapshot.commit"
    )
    parser.add_argument("--vocabulary", type=Path, help="explicit extended-scan vocabulary")
    parser.add_argument(
        "--history", action="store_true", help="scan commits and blobs reachable from --ref"
    )
    parser.add_argument(
        "--ref",
        help=(
            "commit-ish bound to the staged candidate and scanned by --history "
            "(history defaults to HEAD when omitted)"
        ),
    )
    args = parser.parse_args(argv)
    if bool(args.source_root) != bool(args.source_manifest):
        parser.error("--source-root and --source-manifest must be supplied together")
    report = run(
        args.root,
        args.source_root,
        source_manifest_path=args.source_manifest,
        vocabulary_path=args.vocabulary,
        history=args.history,
        ref=args.ref,
    )
    print(f"files scanned : {report.files_scanned}")
    for label, items, skipped in (
        ("shape", report.shape, ""),
        ("coverage", report.coverage, ""),
        ("fidelity", report.fidelity, "" if args.source_root else " [skipped]"),
        # Without a vocabulary the source-token scan has no rules to apply, so it
        # reports zero because it asked nothing -- not because the tree is clean.
        # Unmarked, that zero is indistinguishable from a real pass, which is how
        # an injected private-token set once came back "CANDIDATE CLEAN".
        ("patterns", report.patterns, "" if args.vocabulary else " [skipped: no vocabulary]"),
        ("history", report.history, "" if args.history else " [skipped]"),
    ):
        print(f"{label:13}: {len(items)} failure(s){skipped}")
    for label, items in (
        ("SHAPE", report.shape),
        ("COVERAGE", report.coverage),
        ("FIDELITY", report.fidelity),
        ("PATTERN", report.patterns),
        ("HISTORY", report.history),
    ):
        for item in items[:40]:
            print(f"  {label}  {item}")
        if len(items) > 40:
            print(f"  {label}  ... and {len(items) - 40} more")
    if report.failures:
        print(f"\nNOT PUBLISHABLE: {report.failures} failure(s)")
        return 1
    skipped: list[str] = []
    if not args.source_root:
        skipped.append("fidelity")
    if not args.vocabulary:
        skipped.append("patterns")
    if not args.history:
        skipped.append("history")
    if skipped:
        print(
            "\nCANDIDATE CLEAN: enabled checks passed; "
            f"not a publication verdict (skipped: {', '.join(skipped)})"
        )
        return 0
    print("\nPUBLISHABLE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
