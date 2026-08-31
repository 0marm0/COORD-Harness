
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on non-POSIX installs
    fcntl = None


PLANES = frozenset({"product", "harness", "infrastructure", "shared"})
LIFECYCLES = frozenset({"current", "historical", "superseded", "quarantined"})
CLIENTS = frozenset({"claude", "codex"})
KERNEL_MAX_BYTES = 15 * 1024
KERNEL_MAX_INPUT_FRACTION_NUMERATOR = 3
KERNEL_MAX_INPUT_FRACTION_DENOMINATOR = 4
HEADING_RE = re.compile(br"(?m)^#{1,6}[ \t]+[^\r\n]*(?:\r?\n|$)")
MAX_ATOM_BYTES = 8_192


class AcceptedMemoryError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def stable_read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise AcceptedMemoryError(f"expected regular non-symlink file: {path}")
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after or len(raw) != before.st_size:
        raise AcceptedMemoryError(f"file changed during read: {path}")
    return raw


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(stable_read(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptedMemoryError(f"invalid JSON authority {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AcceptedMemoryError(f"JSON authority must be an object: {path}")
    return value


def _write_new(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _write_json_new(path: Path, value: Any) -> None:
    _write_new(path, json.dumps(value, indent=2, sort_keys=True).encode() + b"\n")


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _logical_basename(logical_path: str) -> str:
    prefix = "claude-project-memory/"
    if not logical_path.startswith(prefix):
        raise AcceptedMemoryError(f"invalid accepted-memory path: {logical_path}")
    basename = logical_path[len(prefix) :]
    if Path(basename).name != basename or not basename.endswith(".md"):
        raise AcceptedMemoryError(f"invalid accepted-memory basename: {logical_path}")
    return basename


def _validate_g1(g1_acceptance: Path) -> tuple[dict[str, Any], str]:
    raw = stable_read(g1_acceptance)
    value = json.loads(raw)
    generation = str(value.get("generation_id") or "")
    if value.get("status") != "PASS_FROZEN_LIVE":
        raise AcceptedMemoryError("G1 authority is not frozen live")
    if not generation.startswith("coord-authority-sha256-"):
        raise AcceptedMemoryError("G1 generation identifier is invalid")
    return value, sha256_bytes(raw)


def atomize_markdown(
    raw: bytes,
    *,
    logical_path: str,
    note_version_id: str,
    plane: str | None,
    lifecycle: str,
) -> list[dict[str, Any]]:
    starts = [match.start() for match in HEADING_RE.finditer(raw)]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    starts = sorted(set(starts))
    heading_boundaries = starts + [len(raw)]
    spans: list[tuple[int, int]] = []
    for start, end in zip(heading_boundaries, heading_boundaries[1:]):
        spans.extend(_bounded_atom_spans(raw, start=start, end=end))
    atoms: list[dict[str, Any]] = []
    for index, (start, end) in enumerate(spans):
        if end <= start:
            continue
        body = raw[start:end]
        digest = sha256_bytes(body)
        first_line = body.decode("utf-8", errors="replace").splitlines()[0].strip()
        hook = re.sub(r"^#{1,6}\s+", "", first_line).strip() or _logical_basename(logical_path)
        atom_id = "memory-atom-sha256-" + sha256_bytes(
            canonical_bytes(
                {
                    "logical_path": logical_path,
                    "note_version_id": note_version_id,
                    "byte_start": start,
                    "byte_end": end,
                    "span_sha256": digest,
                }
            )
        )
        atoms.append(
            {
                "atom_id": atom_id,
                "logical_path": logical_path,
                "note_version_id": note_version_id,
                "ordinal": index,
                "byte_start": start,
                "byte_end": end,
                "span_sha256": digest,
                "hook": hook[:240],
                "subject_plane": plane,
                "lifecycle": lifecycle,
            }
        )
    if b"".join(raw[a["byte_start"] : a["byte_end"]] for a in atoms) != raw:
        raise AcceptedMemoryError(f"atom coverage is not byte-exact: {logical_path}")
    return atoms


def _bounded_atom_spans(
    raw: bytes,
    *,
    start: int,
    end: int,
    max_bytes: int = MAX_ATOM_BYTES,
) -> list[tuple[int, int]]:

    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    if end <= start:
        return []
    spans: list[tuple[int, int]] = []
    cursor = start
    while end - cursor > max_bytes:
        limit = cursor + max_bytes
        newline = raw.rfind(b"\n", cursor, limit + 1)
        if newline >= cursor:
            boundary = newline + 1
        else:
            boundary = limit
            while boundary > cursor and boundary < end and raw[boundary] & 0xC0 == 0x80:
                boundary -= 1
            if boundary == cursor:
                boundary = limit
        spans.append((cursor, boundary))
        cursor = boundary
    if cursor < end:
        spans.append((cursor, end))
    return spans


def _declarations_by_path(
    declaration: dict[str, Any], source_entries: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    if declaration.get("schema") != "coordharness.accepted-memory-r4-declarations.v1":
        raise AcceptedMemoryError("unsupported R4 declaration schema")
    raw_rows = declaration.get("rows")
    if not isinstance(raw_rows, list):
        raise AcceptedMemoryError("R4 declarations require rows[]")
    rows: dict[str, dict[str, Any]] = {}
    for candidate in raw_rows:
        if not isinstance(candidate, dict):
            raise AcceptedMemoryError("R4 declaration row must be an object")
        row = dict(candidate)
        logical_path = str(row.get("logical_path") or "")
        if logical_path in rows:
            raise AcceptedMemoryError(f"duplicate R4 declaration: {logical_path}")
        source = source_entries.get(logical_path)
        if source is None:
            raise AcceptedMemoryError(f"declaration has no accepted source: {logical_path}")
        if row.get("source_sha256") != source.get("source_sha256"):
            raise AcceptedMemoryError(f"declaration source hash mismatch: {logical_path}")
        state = row.get("authority_state")
        plane = row.get("subject_plane")
        lifecycle = row.get("lifecycle")
        if state == "exact":
            if plane not in PLANES:
                raise AcceptedMemoryError(f"exact declaration has invalid plane: {logical_path}")
            if plane == "shared" and not str(row.get("shared_basis") or "").strip():
                raise AcceptedMemoryError(f"shared declaration lacks exact basis: {logical_path}")
            if lifecycle not in LIFECYCLES - {"quarantined"}:
                raise AcceptedMemoryError(f"exact declaration has invalid lifecycle: {logical_path}")
        elif state == "quarantined_unknown":
            if plane is not None or lifecycle != "quarantined":
                raise AcceptedMemoryError(f"quarantined unknown cannot carry a plane: {logical_path}")
            if row.get("boot_eligible"):
                raise AcceptedMemoryError(f"quarantined unknown cannot boot: {logical_path}")
        else:
            raise AcceptedMemoryError(f"invalid authority state: {logical_path}")
        if not isinstance(row.get("boot_eligible"), bool):
            raise AcceptedMemoryError(f"boot_eligible must be boolean: {logical_path}")
        if not isinstance(row.get("supersedes", []), list):
            raise AcceptedMemoryError(f"supersedes must be a list: {logical_path}")
        rows[logical_path] = row
    missing = sorted(set(source_entries) - set(rows))
    extra = sorted(set(rows) - set(source_entries))
    if missing or extra:
        raise AcceptedMemoryError(
            f"R4 declaration set is not exact: missing={missing} extra={extra}"
        )
    return rows


def _effective_lifecycles(
    declarations: dict[str, dict[str, Any]]
) -> dict[str, str]:
    superseded_by: dict[str, str] = {}
    graph: dict[str, list[str]] = {}
    for path, row in declarations.items():
        targets = [str(item) for item in row.get("supersedes", [])]
        graph[path] = targets
        for target in targets:
            if target not in declarations:
                raise AcceptedMemoryError(f"supersession target is absent: {path} -> {target}")
            if target == path:
                raise AcceptedMemoryError(f"self-supersession is forbidden: {path}")
            if target in superseded_by:
                raise AcceptedMemoryError(f"multiple current superseders for {target}")
            if declarations[target].get("subject_plane") != row.get("subject_plane"):
                raise AcceptedMemoryError(f"cross-plane supersession is forbidden: {path} -> {target}")
            superseded_by[target] = path

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise AcceptedMemoryError("supersession graph contains a cycle")
        if node in visited:
            return
        visiting.add(node)
        for target in graph[node]:
            visit(target)
        visiting.remove(node)
        visited.add(node)

    for path in graph:
        visit(path)
    return {
        path: "superseded" if path in superseded_by else str(row["lifecycle"])
        for path, row in declarations.items()
    }


def _previous_history(
    previous_generation: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, bytes]]:
    if previous_generation is None:
        return [], {}, {}
    verify_generation(previous_generation)
    manifest = _json(previous_generation / "manifest.json")
    if manifest.get("schema") != "coordharness.accepted-memory-generation.r4":
        raise AcceptedMemoryError("previous generation schema mismatch")
    generation_id = str(manifest.get("generation_id") or "")
    if previous_generation.name != generation_id:
        raise AcceptedMemoryError("previous generation directory/id mismatch")
    versions = [json.loads(line) for line in stable_read(previous_generation / "note_versions.jsonl").splitlines()]
    heads = _json(previous_generation / "note_heads.json").get("heads")
    if not isinstance(heads, dict):
        raise AcceptedMemoryError("previous generation heads are invalid")
    objects = {
        path.stem: stable_read(path)
        for path in sorted((previous_generation / "objects").glob("*.md"))
    }
    return versions, heads, objects


_SEMANTIC_FIELDS = (
    "logical_path",
    "source_sha256",
    "source_bytes",
    "subject_plane",
    "authority_state",
    "lifecycle",
    "boot_eligible",
    "supersedes",
    "shared_basis",
    "declaration_actor",
    "declaration_basis",
)


def verify_generation(generation: Path) -> dict[str, Any]:
    generation = generation.resolve(strict=True)
    if generation.is_symlink() or not generation.is_dir():
        raise AcceptedMemoryError("generation must be a non-symlink directory")
    manifest_raw = stable_read(generation / "manifest.json")
    manifest = json.loads(manifest_raw)
    if manifest.get("schema") != "coordharness.accepted-memory-generation.r4":
        raise AcceptedMemoryError("R4 generation schema mismatch")
    generation_id = str(manifest.get("generation_id") or "")
    if generation.name != generation_id:
        raise AcceptedMemoryError("R4 generation directory/id mismatch")
    core = dict(manifest)
    core.pop("generation_id", None)
    expected_id = "accepted-memory-r4-sha256-" + sha256_bytes(canonical_bytes(core))
    if generation_id != expected_id:
        raise AcceptedMemoryError("R4 generation identifier is not reproducible")
    for name, digest in manifest.get("file_sha256", {}).items():
        if sha256_bytes(stable_read(generation / name)) != digest:
            raise AcceptedMemoryError(f"R4 generation file hash mismatch: {name}")
    declaration_raw = stable_read(generation / "authority" / "R4_DECLARATIONS.json")
    declaration = json.loads(declaration_raw)
    g1_raw = stable_read(generation / "authority" / "G1_ACCEPTANCE.json")
    if sha256_bytes(declaration_raw) != manifest.get("declaration_sha256"):
        raise AcceptedMemoryError("embedded R4 declaration authority mismatch")
    if sha256_bytes(g1_raw) != manifest.get("g1_acceptance_sha256"):
        raise AcceptedMemoryError("embedded G1 acceptance authority mismatch")
    kernel_source_raw = stable_read(generation / "authority" / "kernel_source.md")
    rebuilt_kernel, rebuilt_proof = _compile_kernel(
        kernel_source_raw, declaration.get("kernel_declaration", {})
    )
    if rebuilt_kernel != stable_read(generation / "kernel.md"):
        raise AcceptedMemoryError("kernel is not reproducible from embedded exact authority")
    if rebuilt_proof != manifest.get("kernel_proof"):
        raise AcceptedMemoryError("kernel proof is not reproducible from embedded exact authority")

    versions = [
        json.loads(line)
        for line in stable_read(generation / "note_versions.jsonl").splitlines()
    ]
    by_version: dict[str, dict[str, Any]] = {}
    for version in versions:
        version_id = str(version.get("version_id") or "")
        if version_id in by_version:
            raise AcceptedMemoryError(f"duplicate note version: {version_id}")
        semantic = {field: version.get(field) for field in _SEMANTIC_FIELDS}
        semantic_sha = sha256_bytes(canonical_bytes(semantic))
        if semantic_sha != version.get("semantic_sha256"):
            raise AcceptedMemoryError(f"note semantic hash mismatch: {version_id}")
        expected_version = "memory-note-sha256-" + sha256_bytes(
            canonical_bytes(
                {
                    "semantic": semantic,
                    "previous_version_id": version.get("previous_version_id"),
                }
            )
        )
        if version_id != expected_version:
            raise AcceptedMemoryError(f"note version identifier mismatch: {version_id}")
        by_version[version_id] = version
    for version in versions:
        previous = version.get("previous_version_id")
        if previous is not None and previous not in by_version:
            raise AcceptedMemoryError(f"note version predecessor missing: {previous}")

    heads = _json(generation / "note_heads.json").get("heads")
    if not isinstance(heads, dict):
        raise AcceptedMemoryError("R4 note heads are invalid")
    if len(set(heads.values())) != len(heads):
        raise AcceptedMemoryError("one note version cannot head multiple logical notes")
    for logical_path, version_id in heads.items():
        version = by_version.get(version_id)
        if version is None or version.get("logical_path") != logical_path:
            raise AcceptedMemoryError(f"note head/version mismatch: {logical_path}")

    objects: dict[str, bytes] = {}
    for path in sorted((generation / "objects").glob("*.md")):
        raw = stable_read(path)
        digest = sha256_bytes(raw)
        if path.stem != digest:
            raise AcceptedMemoryError(f"content object filename/hash mismatch: {path.name}")
        objects[digest] = raw
    for version in versions:
        digest = version["source_sha256"]
        raw = objects.get(digest)
        if raw is None or len(raw) != version["source_bytes"]:
            raise AcceptedMemoryError(f"note version source object missing: {version['version_id']}")

    atoms = [json.loads(line) for line in stable_read(generation / "atoms.jsonl").splitlines()]
    atoms_by_path: dict[str, list[dict[str, Any]]] = {}
    atom_ids: set[str] = set()
    for atom in atoms:
        if atom["atom_id"] in atom_ids:
            raise AcceptedMemoryError(f"duplicate memory atom: {atom['atom_id']}")
        atom_ids.add(atom["atom_id"])
        expected_atom = "memory-atom-sha256-" + sha256_bytes(
            canonical_bytes(
                {
                    "logical_path": atom["logical_path"],
                    "note_version_id": atom["note_version_id"],
                    "byte_start": atom["byte_start"],
                    "byte_end": atom["byte_end"],
                    "span_sha256": atom["span_sha256"],
                }
            )
        )
        if atom["atom_id"] != expected_atom:
            raise AcceptedMemoryError(f"memory atom identifier mismatch: {atom['atom_id']}")
        atoms_by_path.setdefault(atom["logical_path"], []).append(atom)
    if set(atoms_by_path) != set(heads):
        raise AcceptedMemoryError("atomized logical-note set differs from note heads")
    for logical_path, version_id in heads.items():
        version = by_version[version_id]
        raw = objects[version["source_sha256"]]
        cursor = 0
        for atom in sorted(atoms_by_path[logical_path], key=lambda row: row["ordinal"]):
            if atom["note_version_id"] != version_id or atom["byte_start"] != cursor:
                raise AcceptedMemoryError(f"non-contiguous atom coverage: {logical_path}")
            span = raw[atom["byte_start"] : atom["byte_end"]]
            if sha256_bytes(span) != atom["span_sha256"]:
                raise AcceptedMemoryError(f"memory atom content mismatch: {atom['atom_id']}")
            if atom["subject_plane"] != version["subject_plane"]:
                raise AcceptedMemoryError(f"memory atom plane mismatch: {atom['atom_id']}")
            if atom["lifecycle"] != version["lifecycle"]:
                raise AcceptedMemoryError(f"memory atom lifecycle mismatch: {atom['atom_id']}")
            cursor = atom["byte_end"]
        if cursor != len(raw):
            raise AcceptedMemoryError(f"atom coverage is incomplete: {logical_path}")

    kernel = stable_read(generation / "kernel.md")
    proof = manifest.get("kernel_proof", {})
    if len(kernel) != proof.get("kernel_bytes") or len(kernel) > KERNEL_MAX_BYTES:
        raise AcceptedMemoryError("kernel byte proof mismatch")
    if not (
        len(kernel) * KERNEL_MAX_INPUT_FRACTION_DENOMINATOR
        < int(proof.get("selected_input_bytes") or 0)
        * KERNEL_MAX_INPUT_FRACTION_NUMERATOR
    ):
        raise AcceptedMemoryError("kernel input-fraction proof mismatch")
    return {
        "status": "PASS",
        "generation_id": generation_id,
        "manifest_sha256": sha256_bytes(manifest_raw),
        "note_versions": len(versions),
        "note_heads": len(heads),
        "atoms": len(atoms),
        "objects": len(objects),
        "kernel_bytes": len(kernel),
    }


KERNEL_BLOCK_RE = re.compile(br"(?m)^(?:- \*\*(K\d{2})\b|### (K\d{2})\b)")
KERNEL_CITATION_RE = re.compile(r"\s*⟦.*?⟧", flags=re.S)


def _compile_kernel(
    kernel_source_raw: bytes, kernel_declaration: dict[str, Any]
) -> tuple[bytes, dict[str, Any]]:
    if kernel_declaration.get("authority_state") != "exact":
        raise AcceptedMemoryError("kernel source authority is not exact")
    if kernel_declaration.get("source_sha256") != sha256_bytes(kernel_source_raw):
        raise AcceptedMemoryError("kernel source hash differs from exact declaration")
    marker = b"## KERNEL vNEXT"
    start = kernel_source_raw.find(marker)
    if start < 0:
        raise AcceptedMemoryError("kernel source lacks KERNEL vNEXT region")
    end = kernel_source_raw.find(b"\n---\n", start)
    if end < 0:
        raise AcceptedMemoryError("kernel source lacks exact region terminator")
    selected_raw = kernel_source_raw[start:end]
    block_ids = [
        (match.group(1) or match.group(2)).decode("ascii")
        for match in KERNEL_BLOCK_RE.finditer(selected_raw)
    ]
    expected_blocks = [f"K{index:02d}" for index in range(20)]
    if block_ids != expected_blocks:
        raise AcceptedMemoryError(
            f"kernel block coverage/order mismatch: expected={expected_blocks} actual={block_ids}"
        )
    declared_blocks = kernel_declaration.get("selected_blocks")
    if declared_blocks != expected_blocks:
        raise AcceptedMemoryError("kernel declaration does not explicitly select K00-K19")
    selected_text = selected_raw.decode("utf-8")
    compiled = KERNEL_CITATION_RE.sub("", selected_text)
    if "⟦" in compiled or "⟧" in compiled:
        raise AcceptedMemoryError("kernel citation compaction left an unmatched marker")
    raw = (compiled.rstrip() + "\n").encode("utf-8")
    selected_input_bytes = len(selected_raw)
    proof = {
        "kernel_bytes": len(raw),
        "max_bytes": KERNEL_MAX_BYTES,
        "selected_kernel_blocks": len(block_ids),
        "selected_block_ids": block_ids,
        "selected_input_bytes": selected_input_bytes,
        "selected_input_sha256": sha256_bytes(selected_raw),
        "kernel_source_sha256": sha256_bytes(kernel_source_raw),
        "transform": "retain exact K00-K19 semantic text; remove provenance citations compacted into manifest",
        "fraction_numerator": len(raw),
        "fraction_denominator": selected_input_bytes,
        "strictly_below_75_percent": (
            len(raw) * KERNEL_MAX_INPUT_FRACTION_DENOMINATOR
            < selected_input_bytes * KERNEL_MAX_INPUT_FRACTION_NUMERATOR
        ),
        "within_15_kib": len(raw) <= KERNEL_MAX_BYTES,
    }
    if not proof["within_15_kib"] or not proof["strictly_below_75_percent"]:
        raise AcceptedMemoryError(f"boot kernel budget failed closed: {proof}")
    return raw, proof


@dataclass(frozen=True)
class BuildResult:
    generation_id: str
    generation_path: Path
    manifest_sha256: str
    verification: dict[str, Any]


@dataclass(frozen=True)
class CurrentGeneration:

    generation_id: str
    generation_path: Path
    manifest_sha256: str
    pointer_sha256: str
    verification: dict[str, Any]


def build_generation(
    *,
    source_generation: Path,
    declaration_path: Path,
    g1_acceptance_path: Path,
    store_root: Path,
    kernel_source_path: Path,
    previous_generation: Path | None = None,
) -> BuildResult:
    source_generation = source_generation.resolve(strict=True)
    source_manifest_raw = stable_read(source_generation / "manifest.json")
    source_manifest = json.loads(source_manifest_raw)
    if source_manifest.get("schema") != "coordharness.accepted-memory-generation.v4":
        raise AcceptedMemoryError("source accepted-memory generation schema mismatch")
    if source_generation.name != "gen_" + str(source_manifest.get("generation_id"))[:20]:
        raise AcceptedMemoryError("source accepted-memory generation directory/id mismatch")
    source_entries = {
        str(row["path"]): dict(row) for row in source_manifest.get("accepted_entries", [])
    }
    if len(source_entries) != source_manifest.get("counts", {}).get("accepted"):
        raise AcceptedMemoryError("source accepted-memory entry count mismatch")
    g1, g1_sha = _validate_g1(g1_acceptance_path)
    declaration_raw = stable_read(declaration_path)
    declaration = json.loads(declaration_raw)
    if declaration.get("g1_generation_id") != g1["generation_id"]:
        raise AcceptedMemoryError("R4 declarations are not bound to active G1 generation")
    if declaration.get("g1_acceptance_sha256") != g1_sha:
        raise AcceptedMemoryError("R4 declarations are not bound to G1 acceptance bytes")
    declarations = _declarations_by_path(declaration, source_entries)
    lifecycles = _effective_lifecycles(declarations)
    kernel_declaration = declaration.get("kernel_declaration")
    if not isinstance(kernel_declaration, dict):
        raise AcceptedMemoryError("R4 declarations require kernel_declaration")
    kernel_source_raw = stable_read(kernel_source_path.resolve(strict=True))
    g1_raw = stable_read(g1_acceptance_path)
    previous_versions, previous_heads, previous_objects = _previous_history(
        previous_generation
    )
    previous_by_id = {row["version_id"]: row for row in previous_versions}

    objects: dict[str, bytes] = dict(previous_objects)
    new_versions: list[dict[str, Any]] = []
    heads: dict[str, str] = {}
    atoms: list[dict[str, Any]] = []
    note_rows: list[dict[str, Any]] = []
    for logical_path in sorted(source_entries):
        source = source_entries[logical_path]
        row = declarations[logical_path]
        object_path = source_generation / str(source["object_path"])
        raw = stable_read(object_path)
        digest = sha256_bytes(raw)
        if digest != source["source_sha256"] or object_path.name != digest + ".md":
            raise AcceptedMemoryError(f"source object hash mismatch: {logical_path}")
        objects[digest] = raw
        effective_lifecycle = lifecycles[logical_path]
        semantic_core = {
            "logical_path": logical_path,
            "source_sha256": digest,
            "source_bytes": len(raw),
            "subject_plane": row.get("subject_plane"),
            "authority_state": row["authority_state"],
            "lifecycle": effective_lifecycle,
            "boot_eligible": row["boot_eligible"],
            "supersedes": sorted(row.get("supersedes", [])),
            "shared_basis": row.get("shared_basis"),
            "declaration_actor": row.get("declaration_actor"),
            "declaration_basis": row.get("declaration_basis"),
        }
        semantic_sha = sha256_bytes(canonical_bytes(semantic_core))
        previous_id = previous_heads.get(logical_path)
        previous = previous_by_id.get(previous_id)
        if previous and previous.get("semantic_sha256") == semantic_sha:
            version = previous
        else:
            version_id = "memory-note-sha256-" + sha256_bytes(
                canonical_bytes(
                    {
                        "semantic": semantic_core,
                        "previous_version_id": previous_id,
                    }
                )
            )
            version = {
                **semantic_core,
                "semantic_sha256": semantic_sha,
                "version_id": version_id,
                "previous_version_id": previous_id,
            }
            new_versions.append(version)
        heads[logical_path] = version["version_id"]
        note_rows.append(version)
        note_atoms = atomize_markdown(
            raw,
            logical_path=logical_path,
            note_version_id=version["version_id"],
            plane=row.get("subject_plane"),
            lifecycle=effective_lifecycle,
        )
        atoms.extend(note_atoms)

    history_by_id = {row["version_id"]: row for row in previous_versions}
    history_by_id.update({row["version_id"]: row for row in new_versions})
    version_history = sorted(history_by_id.values(), key=lambda row: row["version_id"])
    kernel, kernel_proof = _compile_kernel(kernel_source_raw, kernel_declaration)
    atoms_bytes = b"".join(canonical_bytes(row) + b"\n" for row in atoms)
    versions_bytes = b"".join(canonical_bytes(row) + b"\n" for row in version_history)
    heads_payload = {"schema": "coordharness.accepted-memory-note-heads.r4", "heads": heads}
    heads_bytes = json.dumps(heads_payload, indent=2, sort_keys=True).encode() + b"\n"
    core = {
        "schema": "coordharness.accepted-memory-generation.r4",
        "compiler_sha256": sha256_bytes(stable_read(Path(__file__).resolve())),
        "source_generation_id": source_manifest["generation_id"],
        "source_manifest_sha256": sha256_bytes(source_manifest_raw),
        "g1_generation_id": g1["generation_id"],
        "g1_acceptance_sha256": g1_sha,
        "declaration_sha256": sha256_bytes(declaration_raw),
        "previous_generation_id": previous_generation.name if previous_generation else None,
        "counts": {
            "notes": len(note_rows),
            "exact_notes": sum(r["authority_state"] == "exact" for r in declarations.values()),
            "quarantined_notes": sum(
                r["authority_state"] == "quarantined_unknown" for r in declarations.values()
            ),
            "note_versions_total": len(version_history),
            "note_versions_appended": len(new_versions),
            "atoms": len(atoms),
            "objects": len(objects),
        },
        "plane_counts": {
            plane: sum(r.get("subject_plane") == plane for r in declarations.values())
            for plane in sorted(PLANES)
        },
        "file_sha256": {
            "authority/G1_ACCEPTANCE.json": sha256_bytes(g1_raw),
            "authority/R4_DECLARATIONS.json": sha256_bytes(declaration_raw),
            "authority/kernel_source.md": sha256_bytes(kernel_source_raw),
            "atoms.jsonl": sha256_bytes(atoms_bytes),
            "kernel.md": sha256_bytes(kernel),
            "note_heads.json": sha256_bytes(heads_bytes),
            "note_versions.jsonl": sha256_bytes(versions_bytes),
        },
        "kernel_proof": kernel_proof,
        "publication_state": "dark_unpublished",
    }
    generation_id = "accepted-memory-r4-sha256-" + sha256_bytes(canonical_bytes(core))
    manifest = {**core, "generation_id": generation_id}
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    verification = {
        "schema": "coordharness.accepted-memory-generation-verification.r4",
        "status": "PASS_DARK_READY_FOR_POINTER_REVIEW",
        "generation_id": generation_id,
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "source_set_exact": set(declarations) == set(source_entries),
        "compiler_sha256": core["compiler_sha256"],
        "unknown_inferred_shared": 0,
        "all_source_objects_hash_bound": True,
        "all_source_bytes_atomized": True,
        "append_only_note_versions": True,
        "supersession_graph_acyclic": True,
        "kernel_proof": kernel_proof,
        "live_pointer_changed": False,
    }
    verification_bytes = json.dumps(verification, indent=2, sort_keys=True).encode() + b"\n"

    store_root.mkdir(parents=True, exist_ok=True)
    generations = store_root / "generations"
    generations.mkdir(exist_ok=True)
    final = generations / generation_id
    if os.path.lexists(final):
        raise FileExistsError(f"refusing to overwrite R4 generation: {final}")
    temporary = Path(tempfile.mkdtemp(prefix=".accepted-memory-r4.", dir=generations))
    try:
        for digest, raw in sorted(objects.items()):
            _write_new(temporary / "objects" / f"{digest}.md", raw)
        _write_new(temporary / "authority" / "G1_ACCEPTANCE.json", g1_raw)
        _write_new(temporary / "authority" / "R4_DECLARATIONS.json", declaration_raw)
        _write_new(temporary / "authority" / "kernel_source.md", kernel_source_raw)
        _write_new(temporary / "atoms.jsonl", atoms_bytes)
        _write_new(temporary / "kernel.md", kernel)
        _write_new(temporary / "note_heads.json", heads_bytes)
        _write_new(temporary / "note_versions.jsonl", versions_bytes)
        _write_new(temporary / "manifest.json", manifest_bytes)
        _write_new(temporary / "verification.json", verification_bytes)
        for directory in sorted(
            (path for path in temporary.rglob("*") if path.is_dir()), reverse=True
        ):
            _fsync_dir(directory)
        _fsync_dir(temporary)
        os.rename(temporary, final)
        _fsync_dir(generations)
        for path in final.rglob("*"):
            if path.is_file():
                path.chmod(0o444)
        for path in sorted((p for p in final.rglob("*") if p.is_dir()), reverse=True):
            path.chmod(0o555)
        final.chmod(0o555)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    postwrite = verify_generation(final)
    if postwrite["manifest_sha256"] != sha256_bytes(manifest_bytes):
        raise AcceptedMemoryError("post-write manifest verification drifted")
    return BuildResult(generation_id, final, sha256_bytes(manifest_bytes), verification)


def _read_current(store_root: Path) -> tuple[str | None, bytes | None]:
    current = store_root / "CURRENT"
    if not current.exists():
        return None, None
    raw = stable_read(current)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AcceptedMemoryError("CURRENT is not canonical JSON") from exc
    generation_id = str(value.get("generation_id") or "")
    manifest_path = store_root / "generations" / generation_id / "manifest.json"
    manifest_raw = stable_read(manifest_path)
    if value.get("manifest_sha256") != sha256_bytes(manifest_raw):
        raise AcceptedMemoryError("CURRENT manifest fence failed")
    return generation_id, raw


def open_current_generation(store_root: Path) -> CurrentGeneration:

    store_root = store_root.resolve(strict=True)
    if store_root.is_symlink() or not store_root.is_dir():
        raise AcceptedMemoryError("accepted-memory store must be a non-symlink directory")
    generation_id, pointer_raw = _read_current(store_root)
    if generation_id is None or pointer_raw is None:
        raise AcceptedMemoryError("accepted-memory CURRENT is absent")
    try:
        pointer = json.loads(pointer_raw)
    except json.JSONDecodeError as exc:
        raise AcceptedMemoryError("CURRENT is not canonical JSON") from exc
    if pointer.get("schema") != "coordharness.accepted-memory-current.r4":
        raise AcceptedMemoryError("accepted-memory CURRENT schema mismatch")
    generation = store_root / "generations" / generation_id
    verified = verify_generation(generation)
    manifest_sha256 = str(pointer.get("manifest_sha256") or "")
    if verified.get("manifest_sha256") != manifest_sha256:
        raise AcceptedMemoryError("CURRENT verified manifest fence failed")
    return CurrentGeneration(
        generation_id=generation_id,
        generation_path=generation.resolve(strict=True),
        manifest_sha256=manifest_sha256,
        pointer_sha256=sha256_bytes(pointer_raw),
        verification=verified,
    )


def publish_current(
    *,
    store_root: Path,
    generation_id: str,
    expected_current: str | None,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    generation = store_root / "generations" / generation_id
    verified = verify_generation(generation)
    manifest_raw = stable_read(generation / "manifest.json")
    manifest = json.loads(manifest_raw)
    if manifest.get("generation_id") != generation_id:
        raise AcceptedMemoryError("publication target directory/id mismatch")
    verification = _json(generation / "verification.json")
    if verification.get("status") != "PASS_DARK_READY_FOR_POINTER_REVIEW":
        raise AcceptedMemoryError("publication target is not verified")
    if verified["manifest_sha256"] != sha256_bytes(manifest_raw):
        raise AcceptedMemoryError("publication target post-write verification drifted")
    if fcntl is None:
        # The CURRENT pointer swap below is a read-check-write
        # compare-and-swap; the exclusive lock is what makes two
        # concurrent publishers agree on which one's "expected_current"
        # was actually current. Without it, both could read the same old
        # pointer, both pass their CAS check, and the second write would
        # silently clobber the first's -- a correctness trap, not a
        # missing convenience. Refuse rather than publish unlocked.
        raise AcceptedMemoryError(
            "operating-system file locks are unavailable on this platform; "
            "publish_current() requires the pointer lock for a correct "
            "compare-and-swap and cannot run safely without it"
        )
    store_root.mkdir(parents=True, exist_ok=True)
    lock_path = store_root / ".pointer.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        old_id, old_raw = _read_current(store_root)
        if old_id != expected_current:
            raise AcceptedMemoryError(
                f"CURRENT compare-and-swap failed: expected={expected_current} actual={old_id}"
            )
        pointer = {
            "schema": "coordharness.accepted-memory-current.r4",
            "generation_id": generation_id,
            "manifest_sha256": sha256_bytes(manifest_raw),
        }
        pointer_raw = json.dumps(pointer, indent=2, sort_keys=True).encode() + b"\n"
        temporary = store_root / f".CURRENT.{os.getpid()}.{time.time_ns()}.tmp"
        _write_new(temporary, pointer_raw)
        os.replace(temporary, store_root / "CURRENT")
        _fsync_dir(store_root)
        receipt = {
            "schema": "coordharness.accepted-memory-publication-receipt.r4",
            "actor": actor,
            "reason": reason,
            "old_generation_id": old_id,
            "new_generation_id": generation_id,
            "preimage_sha256": sha256_bytes(old_raw) if old_raw is not None else None,
            "postimage_sha256": sha256_bytes(pointer_raw),
            "manifest_sha256": sha256_bytes(manifest_raw),
            "cas_expected_current": expected_current,
        }
        receipt_id = sha256_bytes(canonical_bytes(receipt))
        receipt_path = store_root / "receipts" / f"{time.time_ns()}_{receipt_id}.json"
        _write_json_new(receipt_path, {**receipt, "receipt_id": receipt_id})
        _fsync_dir(receipt_path.parent)
        return {**receipt, "receipt_id": receipt_id, "receipt_path": str(receipt_path)}


def rollback_current(
    *,
    store_root: Path,
    restore_generation_id: str,
    expected_current: str,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    return publish_current(
        store_root=store_root,
        generation_id=restore_generation_id,
        expected_current=expected_current,
        actor=actor,
        reason="rollback: " + reason,
    )


def client_kernel_bytes(store_root: Path, client: str) -> bytes:
    if client not in CLIENTS:
        raise AcceptedMemoryError(f"unrecognized client identity: {client}")
    generation_id, pointer_raw = _read_current(store_root)
    if generation_id is None or pointer_raw is None:
        raise AcceptedMemoryError("accepted-memory CURRENT is absent")
    generation = store_root / "generations" / generation_id
    manifest = _json(generation / "manifest.json")
    kernel = stable_read(generation / "kernel.md")
    if manifest.get("file_sha256", {}).get("kernel.md") != sha256_bytes(kernel):
        raise AcceptedMemoryError("kernel content fence failed")
    proof = manifest.get("kernel_proof", {})
    if len(kernel) > KERNEL_MAX_BYTES or not proof.get("strictly_below_75_percent"):
        raise AcceptedMemoryError("kernel budget proof failed at client read")
    return kernel


def dual_client_canary(store_root: Path) -> dict[str, Any]:
    claude = client_kernel_bytes(store_root, "claude")
    codex = client_kernel_bytes(store_root, "codex")
    return {
        "schema": "coordharness.accepted-memory-dual-client-canary.r4",
        "status": "PASS" if claude == codex else "FAIL",
        "byte_identical": claude == codex,
        "claude_bytes": len(claude),
        "codex_bytes": len(codex),
        "claude_sha256": sha256_bytes(claude),
        "codex_sha256": sha256_bytes(codex),
    }
