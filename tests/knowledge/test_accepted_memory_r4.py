from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from coordharness.knowledge import context_federator as cf
from coordharness.knowledge.accepted_memory_r4 import (
    AcceptedMemoryError,
    MAX_ATOM_BYTES,
    atomize_markdown,
    build_generation,
    client_kernel_bytes,
    dual_client_canary,
    publish_current,
    open_current_generation,
    rollback_current,
    sha256_bytes,
)


def _json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _fixtures(tmp_path: Path, *, small_boot: bool = False):
    source_id = hashlib.sha256(b"accepted-v4-source").hexdigest()
    source = tmp_path / f"gen_{source_id[:20]}"
    source.mkdir()
    bodies = {
        "a.md": (b"# Guard A\n\n" + (b"a" * (40 if small_boot else 4096)) + b"\n"),
        "b.md": b"# Procedure B\n\n" + (b"b" * 4096) + b"\n",
    }
    entries = []
    for basename, raw in bodies.items():
        digest = sha256_bytes(raw)
        object_path = source / "objects" / f"{digest}.md"
        object_path.parent.mkdir(exist_ok=True)
        object_path.write_bytes(raw)
        entries.append(
            {
                "path": f"claude-project-memory/{basename}",
                "source_basename": basename,
                "source_sha256": digest,
                "source_bytes": len(raw),
                "object_path": f"objects/{digest}.md",
            }
        )
    _json(
        source / "manifest.json",
        {
            "schema": "coordharness.accepted-memory-generation.v4",
            "generation_id": source_id,
            "counts": {"accepted": 2},
            "accepted_entries": entries,
        },
    )
    g1 = tmp_path / "G1_ACCEPTANCE.json"
    _json(
        g1,
        {
            "status": "PASS_FROZEN_LIVE",
            "generation_id": "coord-authority-sha256-" + hashlib.sha256(b"g1").hexdigest(),
        },
    )
    g1_value = json.loads(g1.read_text())
    kernel = tmp_path / "kernel.candidate.md"
    kernel_lines = ["proposal metadata", "", "## KERNEL vNEXT — exact test region", ""]
    citation = "" if small_boot else " ⟦" + ("provenance " * 20) + "⟧"
    for index in range(20):
        kernel_lines.append(f"- **K{index:02d} — rule.** binding rule {index}.{citation}")
    kernel_lines.extend(["", "---", "", "non-kernel appendix"])
    kernel.write_text("\n".join(kernel_lines) + "\n", encoding="utf-8")
    declaration = tmp_path / "declaration.json"
    _json(
        declaration,
        {
            "schema": "coordharness.accepted-memory-r4-declarations.v1",
            "g1_generation_id": g1_value["generation_id"],
            "g1_acceptance_sha256": sha256_bytes(g1.read_bytes()),
            "kernel_declaration": {
                "authority_state": "exact",
                "source_sha256": sha256_bytes(kernel.read_bytes()),
                "selected_blocks": [f"K{index:02d}" for index in range(20)],
            },
            "rows": [
                {
                    "logical_path": "claude-project-memory/a.md",
                    "source_sha256": entries[0]["source_sha256"],
                    "authority_state": "exact",
                    "subject_plane": "shared",
                    "shared_basis": "explicit repository safety invariant",
                    "lifecycle": "current",
                    "boot_eligible": True,
                    "supersedes": [],
                    "declaration_actor": "test",
                    "declaration_basis": "fixture",
                },
                {
                    "logical_path": "claude-project-memory/b.md",
                    "source_sha256": entries[1]["source_sha256"],
                    "authority_state": "exact",
                    "subject_plane": "harness",
                    "lifecycle": "current",
                    "boot_eligible": False,
                    "supersedes": [],
                    "declaration_actor": "test",
                    "declaration_basis": "fixture",
                },
            ],
        },
    )
    return source, g1, declaration, kernel, bodies


def _published_fixture_store(tmp_path: Path) -> tuple[Path, str]:
    source, g1, declaration, kernel, _ = _fixtures(tmp_path)
    value = json.loads(declaration.read_text())
    value["rows"][1]["lifecycle"] = "historical"
    _json(declaration, value)
    store = tmp_path / "store"
    result = build_generation(
        source_generation=source,
        declaration_path=declaration,
        g1_acceptance_path=g1,
        store_root=store,
        kernel_source_path=kernel,
    )
    publish_current(
        store_root=store,
        generation_id=result.generation_id,
        expected_current=None,
        actor="test",
        reason="provider fixture",
    )
    return store, result.generation_id


def test_atomizer_is_byte_exact_and_deterministic() -> None:
    raw = b"preamble\n\n# A\nbody\n## B\nmore\n"
    kwargs = {
        "logical_path": "claude-project-memory/a.md",
        "note_version_id": "v1",
        "plane": "harness",
        "lifecycle": "current",
    }
    atoms = atomize_markdown(raw, **kwargs)
    assert b"".join(raw[a["byte_start"] : a["byte_end"]] for a in atoms) == raw
    assert atoms == atomize_markdown(raw, **kwargs)
    assert len(atoms) == 3


def test_atomizer_bounds_oversized_heading_spans_without_byte_loss() -> None:
    raw = ("# Large\n" + ("evidence row\n" * 2_000)).encode("utf-8")
    kwargs = {
        "logical_path": "claude-project-memory/large.md",
        "note_version_id": "v1",
        "plane": "harness",
        "lifecycle": "current",
    }

    atoms = atomize_markdown(raw, **kwargs)

    assert len(atoms) > 1
    assert max(atom["byte_end"] - atom["byte_start"] for atom in atoms) <= MAX_ATOM_BYTES
    assert b"".join(raw[atom["byte_start"] : atom["byte_end"]] for atom in atoms) == raw
    assert atoms == atomize_markdown(raw, **kwargs)


def test_atomizer_hard_split_is_utf8_safe() -> None:
    raw = ("# Unicode\n" + ("é" * 6_000)).encode("utf-8")
    atoms = atomize_markdown(
        raw,
        logical_path="claude-project-memory/unicode.md",
        note_version_id="v1",
        plane="product",
        lifecycle="current",
    )

    assert max(atom["byte_end"] - atom["byte_start"] for atom in atoms) <= MAX_ATOM_BYTES
    for atom in atoms:
        raw[atom["byte_start"] : atom["byte_end"]].decode("utf-8")
    assert b"".join(raw[atom["byte_start"] : atom["byte_end"]] for atom in atoms) == raw


def test_federator_provider_reads_hash_fenced_r4_atoms_with_semantic_metadata(
    tmp_path: Path,
) -> None:
    store, generation_id = _published_fixture_store(tmp_path)
    provider = cf.AcceptedMemoryProvider(
        store_root=store,
        subject_planes={"harness"},
        lifecycles={"historical"},
    )

    result = provider.search("Procedure B", limit=5)

    assert result.error is None
    assert result.metadata["generation_id"] == generation_id
    assert result.metadata["verified_notes"] == 2
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.source == "accepted_memory"
    assert hit.kind == "accepted_memory_atom"
    assert hit.pointer.startswith(f"accepted-memory://{generation_id}/")
    assert hit.metadata["subject_plane"] == "harness"
    assert hit.metadata["lifecycle"] == "historical"
    assert hit.metadata["authority_state"] == "exact"
    assert hit.metadata["generation_id"] == generation_id
    assert len(hit.metadata["manifest_sha256"]) == 64
    assert len(hit.metadata["span_sha256"]) == 64
    pointer_read = cf.read_context_pointer(
        hit.pointer or "",
        max_bytes=64,
        accepted_memory_store=store,
    )
    assert pointer_read["exists"] is True
    assert pointer_read["truncated"] is True
    assert pointer_read["metadata"]["generation_id"] == generation_id
    assert pointer_read["metadata"]["subject_plane"] == "harness"
    assert len((pointer_read["content"] or "").encode()) <= 64


def test_federator_filters_accepted_memory_before_ranking_and_preserves_compact_metadata(
    tmp_path: Path,
) -> None:
    store, _ = _published_fixture_store(tmp_path)
    provider = cf.AcceptedMemoryProvider(
        store_root=store,
        subject_planes={"shared"},
        lifecycles={"current"},
    )
    packet = cf.ContextFederator(
        [provider],
        config=cf.ContextFederatorConfig(
            max_hits=5,
            per_provider_limit=5,
            max_packet_bytes=6_000,
            compact_metadata=True,
        ),
    ).search("Guard A")

    assert len(packet.hits) == 1
    assert packet.hits[0].metadata["subject_plane"] == "shared"
    assert packet.hits[0].metadata["lifecycle"] == "current"
    assert packet.hits[0].metadata["authority_state"] == "exact"
    assert cf.AcceptedMemoryProvider(
        store_root=store,
        subject_planes={"shared"},
        lifecycles={"current"},
    ).search("Procedure B", limit=5).hits == []


def test_federator_accepted_memory_fails_closed_on_current_hash_drift(tmp_path: Path) -> None:
    store, _ = _published_fixture_store(tmp_path)
    current = json.loads((store / "CURRENT").read_text())
    current["manifest_sha256"] = "0" * 64
    _json(store / "CURRENT", current)

    with pytest.raises(AcceptedMemoryError, match="manifest fence"):
        cf.AcceptedMemoryProvider(store_root=store).search("Guard", limit=5)


def test_open_current_generation_returns_verified_read_only_binding(tmp_path: Path) -> None:
    store, generation_id = _published_fixture_store(tmp_path)
    current = open_current_generation(store)

    assert current.generation_id == generation_id
    assert current.generation_path.name == generation_id
    assert current.verification["status"] == "PASS"
    assert len(current.pointer_sha256) == 64


def test_build_is_exact_versioned_bounded_and_unpublished(tmp_path: Path) -> None:
    source, g1, declaration, kernel, _ = _fixtures(tmp_path)
    store = tmp_path / "store"
    first = build_generation(
        source_generation=source,
        declaration_path=declaration,
        g1_acceptance_path=g1,
        store_root=store,
        kernel_source_path=kernel,
    )
    assert not (store / "CURRENT").exists()
    assert first.verification["kernel_proof"]["within_15_kib"] is True
    assert first.verification["kernel_proof"]["strictly_below_75_percent"] is True
    successor = build_generation(
        source_generation=source,
        declaration_path=declaration,
        g1_acceptance_path=g1,
        store_root=store,
        kernel_source_path=kernel,
        previous_generation=first.generation_path,
    )
    manifest = json.loads((successor.generation_path / "manifest.json").read_text())
    assert manifest["previous_generation_id"] == first.generation_id
    assert manifest["counts"]["note_versions_total"] == 2
    assert manifest["counts"]["note_versions_appended"] == 0
    assert set(json.loads((successor.generation_path / "note_heads.json").read_text())["heads"]) == {
        "claude-project-memory/a.md",
        "claude-project-memory/b.md",
    }


def test_semantic_change_appends_one_note_version(tmp_path: Path) -> None:
    source, g1, declaration, kernel, _ = _fixtures(tmp_path)
    store = tmp_path / "store"
    first = build_generation(
        source_generation=source,
        declaration_path=declaration,
        g1_acceptance_path=g1,
        store_root=store,
        kernel_source_path=kernel,
    )
    changed = json.loads(declaration.read_text())
    changed["rows"][1]["boot_eligible"] = True
    changed_path = tmp_path / "changed.json"
    _json(changed_path, changed)
    successor = build_generation(
        source_generation=source,
        declaration_path=changed_path,
        g1_acceptance_path=g1,
        store_root=store,
        kernel_source_path=kernel,
        previous_generation=first.generation_path,
    )
    manifest = json.loads((successor.generation_path / "manifest.json").read_text())
    assert manifest["counts"]["note_versions_total"] == 3
    assert manifest["counts"]["note_versions_appended"] == 1


def test_atomic_publish_cas_rollback_and_dual_client_equality(tmp_path: Path) -> None:
    source, g1, declaration, kernel, _ = _fixtures(tmp_path)
    store = tmp_path / "store"
    first = build_generation(
        source_generation=source,
        declaration_path=declaration,
        g1_acceptance_path=g1,
        store_root=store,
        kernel_source_path=kernel,
    )
    successor = build_generation(
        source_generation=source,
        declaration_path=declaration,
        g1_acceptance_path=g1,
        store_root=store,
        kernel_source_path=kernel,
        previous_generation=first.generation_path,
    )
    publish_current(
        store_root=store,
        generation_id=first.generation_id,
        expected_current=None,
        actor="test",
        reason="baseline",
    )
    publish_current(
        store_root=store,
        generation_id=successor.generation_id,
        expected_current=first.generation_id,
        actor="test",
        reason="successor",
    )
    with pytest.raises(AcceptedMemoryError, match="compare-and-swap"):
        publish_current(
            store_root=store,
            generation_id=first.generation_id,
            expected_current=first.generation_id,
            actor="test",
            reason="stale writer",
        )
    canary = dual_client_canary(store)
    assert canary["byte_identical"] is True
    assert client_kernel_bytes(store, "claude") == client_kernel_bytes(store, "codex")
    receipt = rollback_current(
        store_root=store,
        restore_generation_id=first.generation_id,
        expected_current=successor.generation_id,
        actor="test",
        reason="restore",
    )
    assert receipt["old_generation_id"] == successor.generation_id
    assert receipt["new_generation_id"] == first.generation_id
    assert len(list((store / "receipts").glob("*.json"))) == 3


def test_missing_declaration_and_implicit_shared_fail_closed(tmp_path: Path) -> None:
    source, g1, declaration, kernel, _ = _fixtures(tmp_path)
    value = json.loads(declaration.read_text())
    value["rows"] = value["rows"][:1]
    missing = tmp_path / "missing.json"
    _json(missing, value)
    with pytest.raises(AcceptedMemoryError, match="declaration set is not exact"):
        build_generation(
            source_generation=source,
            declaration_path=missing,
            g1_acceptance_path=g1,
            store_root=tmp_path / "store-missing",
            kernel_source_path=kernel,
        )
    value = json.loads(declaration.read_text())
    value["rows"][0].pop("shared_basis")
    implicit = tmp_path / "implicit.json"
    _json(implicit, value)
    with pytest.raises(AcceptedMemoryError, match="shared declaration lacks exact basis"):
        build_generation(
            source_generation=source,
            declaration_path=implicit,
            g1_acceptance_path=g1,
            store_root=tmp_path / "store-implicit",
            kernel_source_path=kernel,
        )


def test_kernel_budget_and_supersession_cycles_fail_closed(tmp_path: Path) -> None:
    source, g1, declaration, kernel, _ = _fixtures(tmp_path, small_boot=True)
    with pytest.raises(AcceptedMemoryError, match="kernel budget failed closed"):
        build_generation(
            source_generation=source,
            declaration_path=declaration,
            g1_acceptance_path=g1,
            store_root=tmp_path / "store-small",
            kernel_source_path=kernel,
        )


def test_explicit_supersession_relabels_target_atoms_and_head(tmp_path: Path) -> None:
    source, g1, declaration, kernel, _ = _fixtures(tmp_path)
    value = json.loads(declaration.read_text())
    value["rows"][0]["subject_plane"] = "harness"
    value["rows"][0].pop("shared_basis")
    value["rows"][0]["supersedes"] = ["claude-project-memory/b.md"]
    superseding = tmp_path / "superseding.json"
    _json(superseding, value)
    result = build_generation(
        source_generation=source,
        declaration_path=superseding,
        g1_acceptance_path=g1,
        store_root=tmp_path / "store-superseding",
        kernel_source_path=kernel,
    )
    versions = [
        json.loads(line)
        for line in (result.generation_path / "note_versions.jsonl").read_text().splitlines()
    ]
    target = next(row for row in versions if row["logical_path"].endswith("/b.md"))
    assert target["lifecycle"] == "superseded"
    target_atoms = [
        json.loads(line)
        for line in (result.generation_path / "atoms.jsonl").read_text().splitlines()
        if '"logical_path":"claude-project-memory/b.md"' in line
    ]
    assert target_atoms and {row["lifecycle"] for row in target_atoms} == {"superseded"}
    value = json.loads(declaration.read_text())
    value["rows"][0]["subject_plane"] = "harness"
    value["rows"][0].pop("shared_basis")
    value["rows"][0]["supersedes"] = ["claude-project-memory/b.md"]
    value["rows"][1]["supersedes"] = ["claude-project-memory/a.md"]
    cyclic = tmp_path / "cyclic.json"
    _json(cyclic, value)
    with pytest.raises(AcceptedMemoryError, match="cycle"):
        build_generation(
            source_generation=source,
            declaration_path=cyclic,
            g1_acceptance_path=g1,
            store_root=tmp_path / "store-cycle",
            kernel_source_path=kernel,
        )
