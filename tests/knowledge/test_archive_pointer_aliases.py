from __future__ import annotations

import hashlib
import json
from pathlib import Path

from coordharness.knowledge import context_federator as cf


def _fixture(monkeypatch, tmp_path: Path, *, fenced_hash: str | None = None) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    project = repo
    target = project / "docs" / "archive" / "MOVED.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Moved\n\nPreserved archive body.\n", encoding="utf-8")
    digest = fenced_hash or hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = target.parent / "_PATH_ALIASES.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": cf.ARCHIVE_POINTER_ALIAS_SCHEMA,
                "mappings": [
                    {
                        "mapping_id": "test-exact-move",
                        "source": "docs/ops/MOVED.md",
                        "target": "docs/archive/MOVED.md",
                        "target_sha256": digest,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cf, "REPO", repo)
    monkeypatch.setattr(cf, "COORDHARNESS", project)
    monkeypatch.setattr(cf, "DEFAULT_ARCHIVE_POINTER_ALIAS_MANIFEST", manifest)
    monkeypatch.setattr(cf.kfts, "_REPO_ROOT", repo)
    return repo, target


def test_missing_memory_path_resolves_only_through_exact_hash_fenced_manifest(
    monkeypatch, tmp_path: Path
) -> None:
    _fixture(monkeypatch, tmp_path)
    original = "memory://docs/ops/MOVED.md"

    info = cf._pointer_info(original)
    read = cf.read_context_pointer(original)

    assert info["pointer_health"] == "archived_alias"
    assert info["canonical_pointer"] == "memory://docs/archive/MOVED.md"
    provenance = info["archive_alias_resolution"]
    assert provenance["resolution_kind"] == "archive_manifest_exact_path"
    assert provenance["original_pointer"] == original
    assert provenance["resolved_path"] == "docs/archive/MOVED.md"
    assert provenance["mapping_id"] == "test-exact-move"
    assert read["exists"] is True
    assert read["requested_pointer"] == original
    assert read["resolved_pointer"] == "memory://docs/archive/MOVED.md"
    assert "Preserved archive body" in read["content"]
    assert cf._pointer_info("kfts://docs/ops/MOVED.md")["pointer_health"] == (
        "archived_alias"
    )


def test_plain_coord_context_path_resolves_and_fragment_is_preserved(
    monkeypatch, tmp_path: Path
) -> None:
    _fixture(monkeypatch, tmp_path)
    original = "docs/ops/MOVED.md#moved"

    alias = cf.resolve_archive_pointer_alias(original)
    info = cf._pointer_info(original)

    assert alias["canonical_pointer"] == "docs/archive/MOVED.md#moved"
    assert info["pointer_health"] == "archived_alias"
    assert info["pointer_expandable"] is True


def test_archive_resolver_never_guesses_by_basename(monkeypatch, tmp_path: Path) -> None:
    _fixture(monkeypatch, tmp_path)
    wrong_old_home = "memory://docs/_review/MOVED.md"

    alias = cf.resolve_archive_pointer_alias(wrong_old_home)
    info = cf._pointer_info(wrong_old_home)

    assert alias["status"] == "not_alias"
    assert info["pointer_health"] == "missing"
    assert info["pointer_expandable"] is False


def test_archive_resolver_fails_closed_on_target_hash_mismatch(monkeypatch, tmp_path: Path) -> None:
    _fixture(monkeypatch, tmp_path, fenced_hash="0" * 64)
    original = "memory://docs/ops/MOVED.md"

    alias = cf.resolve_archive_pointer_alias(original)
    info = cf._pointer_info(original)
    read = cf.read_context_pointer(original)

    assert alias["status"] == "target_invalid"
    assert info["pointer_health"] == "unresolved"
    assert info["pointer_expandable"] is False
    assert read["exists"] is False
    assert read["metadata"]["archive_alias_resolution"]["reason"] == (
        "target_missing_or_hash_mismatch"
    )
