
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Mapping, Protocol

from coordharness import config as _harness_config
from coordharness.coord import board_context
from coordharness.knowledge import accepted_memory_r4, facts, kfts


DEFAULT_PROVIDER_LIMIT = 6
DEFAULT_MAX_HITS = 20
DEFAULT_MAX_PACKET_BYTES = 32_000
DEFAULT_POINTER_READ_BYTES = 12_000
MAX_POINTER_READ_BYTES = 40_000
ARTIFACT_LIVE_HASH_MAX_BYTES = 2_000_000

REPO = _harness_config.project_root()
COORDHARNESS = REPO
DEFAULT_ACCEPTED_MEMORY_STORE = _harness_config.state_dir() / "accepted_memory_r4"
DEFAULT_ARCHIVE_POINTER_ALIAS_MANIFEST = REPO / "docs" / "archive" / "_PATH_ALIASES.json"
DEFAULT_COORD_DB = _harness_config.coord_db_path()
ARCHIVE_POINTER_ALIAS_SCHEMA = "coordharness.archive-pointer-aliases.v1"
ACTIVE_POINTER_RECENT_DONE_DAYS = 7
_CONTAINED_REPO_ROOTS = frozenset({"docs", "src", "tests", ".agents", ".claude"})
_CONTAINED_ROOT_FILES = frozenset({"AGENTS.md", "CLAUDE.md"})
_COORD_EVENT_POINTER_RE = re.compile(r"^coord(?:://event/|:event:)([0-9]+)$")
_LEGACY_COORD_EVENT_POINTER_RE = re.compile(r"^memory://coord-event-([0-9]+)$")


@dataclass(frozen=True)
class ContextQueryProfile:
    id: str
    purpose: str
    max_hits: int
    per_provider_limit: int
    max_packet_bytes: int
    provider_policy: str = "default providers only"
    include_board_history: bool = False
    manual_only: bool = False
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextHit:
    source: str
    kind: str
    title: str
    pointer: str | None = None
    snippet: str | None = None
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderResult:
    source: str
    hits: list[ContextHit]
    elapsed_s: float | None = None
    truncated: bool = False
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    returned_before_compaction: int | None = None

    def returned_count(self) -> int:
        if self.returned_before_compaction is not None:
            return int(self.returned_before_compaction)
        return len(self.hits)


@dataclass(frozen=True)
class ContextPacket:
    query: str
    work_id: str | None
    hits: list[ContextHit]
    provider_results: list[ProviderResult]
    errors: list[dict[str, str]]
    truncated: bool
    expansion: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "query": self.query,
            "work_id": self.work_id,
            "hits": [asdict(hit) for hit in self.hits],
            "provider_results": [
                {
                    "source": result.source,
                    "returned": result.returned_count(),
                    "elapsed_s": result.elapsed_s,
                    "truncated": result.truncated,
                    "error": result.error,
                    **({"metadata": result.metadata} if result.metadata else {}),
                }
                for result in self.provider_results
            ],
            "errors": self.errors,
            "truncated": self.truncated,
            "expansion": self.expansion,
        }


class ContextProvider(Protocol):
    source: str

    def search(self, query: str, *, work_id: str | None = None, limit: int = DEFAULT_PROVIDER_LIMIT) -> ProviderResult:
        ...


@dataclass(frozen=True)
class ContextFederatorConfig:
    max_hits: int = DEFAULT_MAX_HITS
    per_provider_limit: int = DEFAULT_PROVIDER_LIMIT
    max_packet_bytes: int = DEFAULT_MAX_PACKET_BYTES
    include_board_history: bool = False
    compact_metadata: bool = False


class ContextFederator:
    def __init__(
        self,
        providers: list[ContextProvider] | None = None,
        *,
        config: ContextFederatorConfig | None = None,
    ) -> None:
        self.config = config or ContextFederatorConfig()
        self.providers = providers if providers is not None else default_context_providers(
            include_board_history=self.config.include_board_history
        )

    def search(self, query: str, *, work_id: str | None = None) -> ContextPacket:
        query = " ".join(str(query or "").split())
        provider_limit = max(1, int(self.config.per_provider_limit))
        results: list[ProviderResult] = []
        hits: list[ContextHit] = []
        errors: list[dict[str, str]] = []
        for provider in self.providers:
            try:
                result = provider.search(query, work_id=work_id, limit=provider_limit)
            except Exception as exc:
                result = ProviderResult(source=getattr(provider, "source", provider.__class__.__name__), hits=[], error=str(exc))
            if self.config.compact_metadata:
                result = _compact_search_result(result)
            results.append(result)
            if result.error:
                errors.append({"source": result.source, "error": result.error})
            hits.extend(result.hits)

        deduped = _order_hits(_dedupe_hits(hits))
        max_hits = max(0, int(self.config.max_hits))
        truncated = len(deduped) > max_hits or any(r.truncated for r in results)
        selected = _select_diverse_hits(deduped, max_hits)
        if self.config.compact_metadata:
            selected = [_compact_context_hit(hit) for hit in selected]
        selected, byte_truncated = _bound_hits(selected, self.config.max_packet_bytes)
        packet = ContextPacket(
            query=query,
            work_id=work_id,
            hits=selected,
            provider_results=results,
            errors=errors,
            truncated=truncated or byte_truncated,
            expansion=_expansion(query, work_id, selected),
        )
        return _bound_packet(packet, self.config.max_packet_bytes)


class BoardContextProvider:
    source = "board"

    def __init__(self, *, db_path: str | Path | None = None, rows: list[dict[str, Any]] | None = None) -> None:
        self.db_path = db_path
        self.rows = rows

    def search(self, query: str, *, work_id: str | None = None, limit: int = DEFAULT_PROVIDER_LIMIT) -> ProviderResult:
        rows = self.rows if self.rows is not None else board_context.load_rows(self.db_path)
        by_id = {str(row.get("work_id") or row.get("id") or ""): row for row in rows}
        if work_id:
            focus = board_context.build_focus(rows, work_id, query=query or None, related_limit=limit)
            raw_rows = [focus["row"], *(focus.get("related_open") or []), *(focus.get("related_done") or [])]
            hits = [_hit_from_board_row(_merge_board_row(row, by_id), reason=row.get("why_included")) for row in raw_rows if row]
            return ProviderResult(
                source=self.source,
                hits=hits[:limit],
                truncated=focus.get("truncated", False),
                metadata={"work_id": work_id, "work_id_scope": "board_focus"},
            )
        search = board_context.search_rows(rows, query, limit=limit)
        hits = [_hit_from_board_row(_merge_board_row(row, by_id), reason=row.get("why_included")) for row in search.get("results", [])]
        return ProviderResult(
            source=self.source,
            hits=hits,
            truncated=search.get("truncated", False),
            metadata={"work_id": work_id, "work_id_scope": "query_only_not_filtered"},
        )


class BoardHistoryProvider:
    source = "board_history"

    def __init__(self, *, db_path: str | Path | None = None, rows: list[dict[str, Any]] | None = None) -> None:
        self.db_path = db_path
        self.rows = rows

    def search(self, query: str, *, work_id: str | None = None, limit: int = DEFAULT_PROVIDER_LIMIT) -> ProviderResult:
        rows = self.rows if self.rows is not None else board_context.load_rows(self.db_path)
        done_cards = board_context.build_done_cards(rows, query, limit=limit)
        hits = [
            _hit_from_board_row(
                row,
                reason=row.get("why_included"),
                source=self.source,
                kind="done_card",
            )
            for row in done_cards.get("cards", [])
        ]
        return ProviderResult(
            source=self.source,
            hits=hits,
            truncated=done_cards.get("truncated", False),
            metadata={"work_id": work_id, "work_id_scope": "query_only_not_filtered"},
        )


KFTS_V2_RUNTIME_POINTER: Path | None = None


def _contained_read_path(raw: str | Path, *, base: Path | None = None) -> Path | None:
    path = Path(raw).expanduser()
    candidate = path if path.is_absolute() else (base or REPO) / path
    resolved = candidate.resolve(strict=False)
    allowed = (REPO.resolve(), _harness_config.state_dir().resolve(strict=False))
    if not any(resolved == root or root in resolved.parents for root in allowed):
        return None
    return resolved


def configured_kfts_runtime_pointer(
    *, env: Mapping[str, str] | None = None
) -> Path | None:
    source = os.environ if env is None else env
    raw = str(source.get("COORD_KFTS_RUNTIME_POINTER") or "").strip()
    if not raw:
        return KFTS_V2_RUNTIME_POINTER
    pointer = _contained_read_path(raw)
    if pointer is None:
        raise ValueError("COORD_KFTS_RUNTIME_POINTER escapes project and state roots")
    return pointer


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _activated_kfts_v2_db() -> Path | None:
    pointer = configured_kfts_runtime_pointer()
    if pointer is None or not pointer.is_file() or pointer.is_symlink():
        return None
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        if payload.get("schema") != "coordharness.kfts-v2-runtime-pointer.v1":
            return None
        db = _contained_read_path(str(payload["database_path"]), base=pointer.parent)
        manifest = _contained_read_path(str(payload["manifest_path"]), base=pointer.parent)
        if db is None or manifest is None or not db.is_file() or not manifest.is_file():
            return None
        if _file_sha256(db) != payload["database_sha256"]:
            return None
        if _file_sha256(manifest) != payload["manifest_sha256"]:
            return None
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        if manifest_payload.get("generation_id") != payload["generation_id"]:
            return None
        return db
    except (OSError, KeyError, TypeError, ValueError):
        return None


class KftsProvider:
    source = "kfts"

    def __init__(self, *, db_path: str | Path | None = None, include_freshness: bool = False) -> None:
        self.db_path = db_path if db_path is not None else _activated_kfts_v2_db()
        self.include_freshness = include_freshness

    def search(self, query: str, *, work_id: str | None = None, limit: int = DEFAULT_PROVIDER_LIMIT) -> ProviderResult:
        rows = kfts.search(query, db_path=self.db_path, limit=limit)
        hits = [
            ContextHit(
                source=self.source,
                kind="knowledge_section" if str(row.get("card_kind") or "") == "section" else "knowledge_pointer",
                title=str(row.get("title") or row.get("pointer") or "knowledge pointer"),
                pointer=str(row.get("pointer")) if row.get("pointer") else None,
                snippet=str(row.get("snippet")) if row.get("snippet") else None,
                metadata={
                    key: value
                    for key, value in {
                        "card_kind": row.get("card_kind"),
                        "doc_pointer": row.get("doc_pointer"),
                        "source_path": row.get("source_path"),
                        "heading": row.get("heading"),
                        "heading_path": row.get("heading_path"),
                        "heading_slug": row.get("heading_slug"),
                        "heading_level": row.get("heading_level"),
                        "section_index": row.get("section_index"),
                        "line_start": row.get("line_start"),
                        "line_end": row.get("line_end"),
                        "line_count": row.get("line_count"),
                        "matched_terms": row.get("matched_terms"),
                        "term_coverage": row.get("term_coverage"),
                        "source_tier": row.get("source_tier"),
                        "stale_source": row.get("stale_source"),
                        "freshness_basis": row.get("freshness_basis"),
                    }.items()
                    if value not in (None, "")
                },
            )
            for row in rows
        ]
        metadata = {"work_id": work_id, "work_id_scope": "query_only_not_filtered"}
        metadata.update(self._freshness_metadata())
        return ProviderResult(source=self.source, hits=hits, metadata=metadata)

    def _freshness_metadata(self) -> dict[str, Any]:
        if not self.include_freshness:
            return {}
        refresh = {
            "automatic": False,
            "python_api": kfts.REBUILD_INDEX_API,
        }
        try:
            stats = kfts.index_stats(db_path=self.db_path, use_manifest=True, scan_fallback=False)
        except Exception as exc:
            return {"index_stats_error": str(exc), "index_refresh": refresh}
        freshness_basis = stats.get("freshness_basis")
        manifest_age_seconds: float | None = None
        manifest_updated_at = kfts._manifest_updated_at(self.db_path) if freshness_basis == "source_manifest" else None
        if manifest_updated_at:
            manifest_age_seconds = max(0.0, time.time() - manifest_updated_at)
        return {
            "index_stats": {
                "documents": int(stats.get("documents") or 0),
                "source_file_count": int(stats.get("source_file_count") or 0),
                "indexed_source_path_count": int(stats.get("indexed_source_path_count") or 0),
                "stale": bool(stats.get("stale")),
                "stale_reasons": list(stats.get("stale_reasons") or [])[:3],
                "freshness_basis": freshness_basis,
                "freshness_label": (
                    "as-of manifest snapshot, not live scan"
                    if freshness_basis == "source_manifest"
                    else "live source scan"
                ),
                "manifest_updated_at": manifest_updated_at,
                "manifest_age_seconds": manifest_age_seconds,
                "verify_api": "coordharness.knowledge.kfts.index_stats",
            },
            "index_refresh": refresh,
        }


class FactsProvider:
    source = "facts"

    def __init__(self, *, db_path: str | Path | None = None) -> None:
        self.db_path = db_path

    def search(self, query: str, *, work_id: str | None = None, limit: int = DEFAULT_PROVIDER_LIMIT) -> ProviderResult:
        rows = facts.search_facts(query, db_path=self.db_path, limit=limit)
        hits = [
            _hit_from_fact_search_hit(row)
            for row in rows
        ]
        return ProviderResult(
            source=self.source,
            hits=hits,
            metadata={"work_id": work_id, "work_id_scope": "query_only_not_filtered"},
        )


class ArtifactManifestProvider:
    source = "artifact_manifest"

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        live_hash_max_bytes: int = ARTIFACT_LIVE_HASH_MAX_BYTES,
    ) -> None:
        self.db_path = db_path
        self.live_hash_max_bytes = max(0, int(live_hash_max_bytes))

    def search(self, query: str, *, work_id: str | None = None, limit: int = DEFAULT_PROVIDER_LIMIT) -> ProviderResult:
        hits: list[ContextHit] = []
        seen: set[Path] = set()
        for raw in _artifact_path_candidates(query):
            path = _path_from_local_pointer(raw)
            if path is None or not path.exists() or not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            hits.append(_artifact_manifest_hit(path, raw_pointer=raw))
            if len(hits) >= max(1, int(limit)):
                break

        catalog_rows_scanned = 0
        catalog_candidates: list[tuple[float, float, dict[str, Any]]] = []
        catalog_error: str | None = None
        try:
            conn = board_context._connect(self.db_path)
            try:
                rows = conn.execute(
                    "SELECT a.artifact_id, a.work_id, a.run_id, a.path, a.kind,"
                    " a.sha256, a.validation_json, a.created_at,"
                    " w.title AS work_title, w.display AS work_display,"
                    " w.module AS work_module, w.sublane AS work_sublane"
                    " FROM artifacts a LEFT JOIN work_items w ON w.work_id=a.work_id"
                ).fetchall()
            finally:
                conn.close()
            catalog_rows_scanned = len(rows)
            query_text = " ".join(str(query or "").split())
            query_lower = query_text.lower()
            query_terms = board_context.tokenize(query_text)
            for raw_row in rows:
                row = dict(raw_row)
                row_work_id = str(row.get("work_id") or "")
                if work_id and row_work_id != str(work_id):
                    continue
                path_text = str(row.get("path") or "")
                title_text = str(row.get("work_display") or row.get("work_title") or "")
                kind_text = str(row.get("kind") or "")
                terms = board_context.tokenize(" ".join((row_work_id, path_text, title_text, kind_text)))
                score = 0.0
                strong_match = False
                if work_id and row_work_id == str(work_id):
                    score += 100.0
                    strong_match = True
                if query_lower:
                    if query_lower == row_work_id.lower():
                        score += 80.0
                        strong_match = True
                    if query_lower == path_text.lower():
                        score += 60.0
                        strong_match = True
                    elif query_lower in path_text.lower():
                        score += 20.0
                        strong_match = True
                    if query_lower in row_work_id.lower():
                        score += 30.0
                        strong_match = True
                    if query_lower in title_text.lower():
                        score += 15.0
                        strong_match = True
                    score += 4.0 * len(query_terms & terms)
                if score > 0 and strong_match:
                    catalog_candidates.append((score, float(row.get("created_at") or 0.0), row))
        except Exception as exc:
            catalog_error = f"{type(exc).__name__}: {exc}"

        catalog_candidates.sort(
            key=lambda item: (-item[0], -item[1], str(item[2].get("artifact_id") or ""))
        )
        candidate_count = len(catalog_candidates)
        for score, _created_at, row in catalog_candidates:
            path = _artifact_catalog_path(str(row.get("path") or ""))
            if path is not None:
                resolved = path.resolve(strict=False)
                if resolved in seen:
                    continue
                seen.add(resolved)
            hits.append(
                _artifact_catalog_hit(
                    row,
                    score=score,
                    path=path,
                    live_hash_max_bytes=self.live_hash_max_bytes,
                )
            )
            if len(hits) >= max(1, int(limit)):
                break
        metadata: dict[str, Any] = {
            "work_id": work_id,
            "work_id_scope": "exact_filter" if work_id else "query_only_not_filtered",
            "catalog_rows_scanned": catalog_rows_scanned,
            "catalog_candidate_count": candidate_count,
            "live_hash_max_bytes": self.live_hash_max_bytes,
            "catalog_writes": 0,
        }
        if catalog_error:
            metadata["catalog_error"] = catalog_error
        return ProviderResult(
            source=self.source,
            hits=hits,
            truncated=candidate_count > len(hits),
            metadata=metadata,
        )


def _accepted_memory_filter(
    label: str,
    values: set[str] | tuple[str, ...] | list[str] | None,
    allowed: set[str] | frozenset[str],
) -> frozenset[str] | None:
    if values is None:
        return None
    normalized = frozenset(str(value).strip() for value in values if str(value).strip())
    unknown = sorted(normalized - set(allowed))
    if unknown:
        raise ValueError(f"unknown accepted-memory {label}(s): {unknown}")
    return normalized


def _accepted_memory_version_allowed(
    version: dict[str, Any],
    *,
    subject_planes: frozenset[str] | None,
    lifecycles: frozenset[str] | None,
    authority_states: frozenset[str] | None,
) -> bool:
    return not (
        (subject_planes is not None and version.get("subject_plane") not in subject_planes)
        or (lifecycles is not None and version.get("lifecycle") not in lifecycles)
        or (
            authority_states is not None
            and version.get("authority_state") not in authority_states
        )
    )


def _accepted_memory_snippet(body: str, query_terms: set[str], *, limit: int = 320) -> str:
    compact = " ".join(str(body or "").split())
    if not compact:
        return ""
    lower = compact.lower()
    positions = [lower.find(term.lower()) for term in query_terms]
    positions = [position for position in positions if position >= 0]
    start = max(0, min(positions) - 80) if positions else 0
    snippet = compact[start : start + max(1, int(limit))]
    if start:
        snippet = "… " + snippet
    if start + limit < len(compact):
        snippet = snippet.rstrip() + " …"
    return snippet


def _accepted_memory_title(hook: str, logical_path: str, body: str) -> str:
    if hook and hook != "---":
        return hook
    if body.startswith("---"):
        match = re.search(r"(?m)^name:\s*[\"']?([^\n\"']+)", body)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return Path(logical_path).stem.replace("-", " ")


class AcceptedMemoryProvider:
    source = "accepted_memory"

    def __init__(
        self,
        *,
        store_root: str | Path | None = None,
        subject_planes: set[str] | tuple[str, ...] | list[str] | None = None,
        lifecycles: set[str] | tuple[str, ...] | list[str] | None = None,
        authority_states: set[str] | tuple[str, ...] | list[str] | None = ("exact",),
    ) -> None:
        self.store_root = Path(store_root) if store_root is not None else DEFAULT_ACCEPTED_MEMORY_STORE
        self.subject_planes = _accepted_memory_filter(
            "subject plane", subject_planes, accepted_memory_r4.PLANES
        )
        self.lifecycles = _accepted_memory_filter(
            "lifecycle", lifecycles, accepted_memory_r4.LIFECYCLES
        )
        self.authority_states = _accepted_memory_filter(
            "authority state", authority_states, {"exact", "quarantined_unknown"}
        )

    def search(self, query: str, *, work_id: str | None = None, limit: int = DEFAULT_PROVIDER_LIMIT) -> ProviderResult:
        current_path = self.store_root / "CURRENT"
        if not current_path.is_file():
            return ProviderResult(
                source=self.source,
                hits=[],
                metadata={
                    "available": False,
                    "store_root": str(self.store_root),
                    "work_id": work_id,
                    "work_id_scope": "query_only_not_filtered",
                },
            )
        current = accepted_memory_r4.open_current_generation(self.store_root)
        generation = current.generation_path
        heads_payload = json.loads(accepted_memory_r4.stable_read(generation / "note_heads.json"))
        heads = heads_payload.get("heads")
        if not isinstance(heads, dict):
            raise accepted_memory_r4.AcceptedMemoryError("accepted-memory note heads are invalid")
        versions = {
            str(row["version_id"]): row
            for row in (
                json.loads(line)
                for line in accepted_memory_r4.stable_read(
                    generation / "note_versions.jsonl"
                ).splitlines()
            )
        }
        atoms = [
            json.loads(line)
            for line in accepted_memory_r4.stable_read(generation / "atoms.jsonl").splitlines()
        ]
        object_cache: dict[str, bytes] = {}
        candidates: list[tuple[float, str, ContextHit]] = []
        filtered_notes = 0
        query_text = " ".join(str(query or "").split())
        query_lower = query_text.lower()
        query_terms = board_context.tokenize(query_text)
        for atom in atoms:
            logical_path = str(atom.get("logical_path") or "")
            version_id = str(heads.get(logical_path) or "")
            if not version_id or atom.get("note_version_id") != version_id:
                continue
            version = versions.get(version_id)
            if version is None:
                raise accepted_memory_r4.AcceptedMemoryError(
                    f"accepted-memory atom head version is missing: {logical_path}"
                )
            if not _accepted_memory_version_allowed(
                version,
                subject_planes=self.subject_planes,
                lifecycles=self.lifecycles,
                authority_states=self.authority_states,
            ):
                filtered_notes += 1
                continue
            source_sha256 = str(version.get("source_sha256") or "")
            raw = object_cache.get(source_sha256)
            if raw is None:
                raw = accepted_memory_r4.stable_read(
                    generation / "objects" / f"{source_sha256}.md"
                )
                object_cache[source_sha256] = raw
            start = int(atom.get("byte_start") or 0)
            end = int(atom.get("byte_end") or 0)
            span = raw[start:end]
            if accepted_memory_r4.sha256_bytes(span) != atom.get("span_sha256"):
                raise accepted_memory_r4.AcceptedMemoryError(
                    f"accepted-memory atom span fence failed: {atom.get('atom_id')}"
                )
            hook = str(atom.get("hook") or Path(logical_path).name)
            body = span.decode("utf-8", errors="replace")
            title = _accepted_memory_title(hook, logical_path, body)
            searchable = " ".join((logical_path, title, body))
            searchable_lower = searchable.lower()
            searchable_terms = board_context.tokenize(searchable)
            matched_terms = sorted(query_terms & searchable_terms)
            if query_text and not matched_terms and query_lower not in searchable_lower:
                continue
            coverage = len(matched_terms) / max(1, len(query_terms)) if query_terms else 1.0
            lifecycle = str(version.get("lifecycle") or "")
            score = 100.0 * coverage
            if query_lower and query_lower in title.lower():
                score += 40.0
            if query_lower and query_lower in logical_path.lower():
                score += 30.0
            if lifecycle == "current":
                score += 8.0
            if version.get("boot_eligible") is True:
                score += 2.0
            snippet = _accepted_memory_snippet(body, query_terms)
            pointer = (
                f"accepted-memory://{current.generation_id}/{logical_path}"
                f"#atom={atom.get('atom_id')}"
            )
            candidates.append(
                (
                    score,
                    str(atom.get("atom_id") or ""),
                    ContextHit(
                        source=self.source,
                        kind="accepted_memory_atom",
                        title=title,
                        pointer=pointer,
                        snippet=snippet,
                        score=score,
                        metadata={
                            "generation_id": current.generation_id,
                            "manifest_sha256": current.manifest_sha256,
                            "pointer_sha256": current.pointer_sha256,
                            "logical_path": logical_path,
                            "note_version_id": version_id,
                            "atom_id": atom.get("atom_id"),
                            "span_sha256": atom.get("span_sha256"),
                            "source_sha256": source_sha256,
                            "byte_start": start,
                            "byte_end": end,
                            "subject_plane": version.get("subject_plane"),
                            "lifecycle": lifecycle,
                            "authority_state": version.get("authority_state"),
                            "boot_eligible": bool(version.get("boot_eligible")),
                            "matched_terms": matched_terms,
                            "term_coverage": round(coverage, 3),
                        },
                    ),
                )
            )
        candidates.sort(key=lambda item: (-item[0], item[1]))
        bounded_limit = max(1, int(limit))
        hits = [item[2] for item in candidates[:bounded_limit]]
        return ProviderResult(
            source=self.source,
            hits=hits,
            truncated=len(candidates) > len(hits),
            metadata={
                "available": True,
                "generation_id": current.generation_id,
                "manifest_sha256": current.manifest_sha256,
                "pointer_sha256": current.pointer_sha256,
                "verified_notes": current.verification.get("note_heads"),
                "verified_atoms": current.verification.get("atoms"),
                "candidate_count": len(candidates),
                "filtered_atom_count": filtered_notes,
                "subject_planes": sorted(self.subject_planes) if self.subject_planes else None,
                "lifecycles": sorted(self.lifecycles) if self.lifecycles else None,
                "authority_states": sorted(self.authority_states) if self.authority_states else None,
                "work_id": work_id,
                "work_id_scope": "query_only_not_filtered",
            },
        )


class MemoryProposalProvider:
    source = "memory_proposals"

    def __init__(self, *, db_path: str | Path | None = None) -> None:
        self.db_path = db_path

    def search(self, query: str, *, work_id: str | None = None, limit: int = DEFAULT_PROVIDER_LIMIT) -> ProviderResult:
        rows = _memory_proposal_rows(status="proposed", query=query, work_id=work_id, limit=limit, db_path=self.db_path)
        return ProviderResult(
            source=self.source,
            hits=[_hit_from_memory_proposal_row(row, source=self.source, kind="memory_proposal") for row in rows],
            metadata={"work_id": work_id, "work_id_scope": "query_or_work_filter"},
        )


def _artifact_path_candidates(query: str) -> list[str]:
    raw = str(query or "").strip()
    if not raw:
        return []
    candidates: list[str] = [raw]
    candidates.extend(part.strip("'\"`()[]{}<>") for part in re.split(r"[\s,;]+", raw))
    out: list[str] = []
    for candidate in candidates:
        if not candidate or candidate in out:
            continue
        if _path_from_local_pointer(candidate) is not None:
            out.append(candidate)
    return out[:12]


def _repo_rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO.resolve()).as_posix()
    except ValueError:
        return str(path)


def _artifact_manifest_hit(path: Path, *, raw_pointer: str) -> ContextHit:
    stat = path.stat()
    rel = _repo_rel(path)
    metadata = {
        "path": rel,
        "raw_pointer": raw_pointer,
        "size_bytes": int(stat.st_size),
        "mtime": stat.st_mtime,
        "suffix": path.suffix,
        "is_file": True,
        "manifest_only": True,
    }
    return ContextHit(
        source=ArtifactManifestProvider.source,
        kind="artifact_manifest",
        title=f"Artifact manifest: {rel}",
        pointer=rel,
        snippet=f"{rel} ({stat.st_size} bytes)",
        metadata=metadata,
    )


def _artifact_catalog_path(pointer: str) -> Path | None:
    return _path_from_local_pointer(pointer)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_catalog_hit(
    row: dict[str, Any],
    *,
    score: float,
    path: Path | None,
    live_hash_max_bytes: int,
) -> ContextHit:
    raw_path = str(row.get("path") or "")
    work_id = str(row.get("work_id") or "")
    kind = str(row.get("kind") or "artifact")
    exists = bool(path is not None and path.is_file())
    stored_sha256 = str(row.get("sha256") or "").strip().lower()
    stored_sha256_valid = bool(re.fullmatch(r"[a-f0-9]{64}", stored_sha256))
    metadata: dict[str, Any] = {
        "artifact_id": row.get("artifact_id"),
        "work_id": work_id or None,
        "run_id": row.get("run_id"),
        "kind": kind,
        "path": raw_path,
        "created_at": row.get("created_at"),
        "work_module": row.get("work_module"),
        "work_sublane": row.get("work_sublane"),
        "catalog_backed": True,
        "catalog_sha256_present": bool(stored_sha256),
        "catalog_sha256_valid": stored_sha256_valid,
        "pointer_path_exists": exists,
        "manifest_only": True,
    }
    if stored_sha256:
        metadata["catalog_sha256"] = stored_sha256
    if exists and path is not None:
        stat = path.stat()
        metadata["size_bytes"] = int(stat.st_size)
        metadata["mtime_ns"] = int(stat.st_mtime_ns)
        if stat.st_size <= live_hash_max_bytes:
            metadata["live_sha256"] = _sha256_file(path)
            metadata["live_sha256_basis"] = "bounded_on_demand_file_bytes"
        else:
            metadata["live_sha256_deferred"] = "size_over_live_hash_cap"
    label = str(row.get("work_display") or row.get("work_title") or work_id or raw_path)
    basename = Path(raw_path).name or raw_path
    return ContextHit(
        source=ArtifactManifestProvider.source,
        kind="artifact_catalog",
        title=f"{label}: {kind} {basename}",
        pointer=raw_path or None,
        snippet=f"catalog artifact for {work_id or 'unscoped work'} ({kind})",
        score=score,
        metadata={key: value for key, value in metadata.items() if value not in (None, "")},
    )


def _memory_proposal_db_path(db_path: str | Path | None) -> Path:
    from coordharness.knowledge import memory_proposals

    return memory_proposals._boundary_checked_path(db_path)


def _memory_proposal_rows(
    *,
    status: str,
    query: str,
    work_id: str | None,
    limit: int,
    db_path: str | Path | None,
) -> list[dict[str, Any]]:
    path = _memory_proposal_db_path(db_path)
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_proposals'"
        ).fetchone()
        if exists is None:
            return []
        rows = conn.execute(
            """
            SELECT id, kind, statement, value, confidence, scope, status, evidence_pointer,
                   provenance_json, tags_json, source_actor, source_thread_id, source_work_id,
                   seen_count, created_at, updated_at, reviewed_by, reviewed_at, review_note
              FROM memory_proposals
             WHERE status=?
             ORDER BY updated_at DESC, id DESC
             LIMIT ?
            """,
            (status, max(1, min(100, int(limit) * 8))),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            conn.close()
        except UnboundLocalError:
            pass
    query_terms = _query_terms(query)
    filtered: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if work_id and str(item.get("source_work_id") or "") != str(work_id):
            continue
        if query_terms and not (query_terms & _query_terms(_memory_proposal_search_blob(item))):
            continue
        filtered.append(item)
        if len(filtered) >= max(1, int(limit)):
            break
    return filtered


def _memory_proposal_search_blob(row: dict[str, Any]) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in ("id", "kind", "statement", "value", "scope", "evidence_pointer", "tags_json", "source_work_id")
    )


def _query_terms(value: str) -> set[str]:
    return {term for term in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", str(value or "").lower())}


def _json_object_safe(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list_safe(value: str | None) -> list[Any]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _hit_from_memory_proposal_row(row: dict[str, Any], *, source: str, kind: str) -> ContextHit:
    value, value_truncated = _clip_text_with_flag(row.get("value"), 220)
    metadata = {
        "id": row.get("id"),
        "proposal_kind": row.get("kind"),
        "status": row.get("status"),
        "scope": row.get("scope"),
        "confidence": row.get("confidence"),
        "source_actor": row.get("source_actor"),
        "source_work_id": row.get("source_work_id"),
        "seen_count": row.get("seen_count"),
        "updated_at": row.get("updated_at"),
        "reviewed_by": row.get("reviewed_by"),
        "reviewed_at": row.get("reviewed_at"),
        "value_truncated": value_truncated,
        "tags": _json_list_safe(row.get("tags_json")),
        "provenance": _json_object_safe(row.get("provenance_json")),
    }
    return ContextHit(
        source=source,
        kind=kind,
        title=str(row.get("statement") or row.get("id") or "memory proposal"),
        pointer=str(row.get("evidence_pointer") or "") or None,
        snippet=value,
        metadata={key: val for key, val in metadata.items() if val not in (None, "", [])},
    )


def _hit_from_fact_search_hit(row: Any) -> ContextHit:
    fact = row.fact
    evidence_pointer = fact.evidence_pointer
    evidence_candidates = _evidence_pointer_candidates(evidence_pointer)
    primary_pointer = _first_expandable_evidence_pointer(evidence_candidates)
    value, value_truncated = _clip_text_with_flag(fact.value, 160)
    notes, notes_truncated = _clip_text_with_flag(fact.notes, 240)
    return ContextHit(
        source=FactsProvider.source,
        kind="fact",
        title=str(fact.statement or "fact"),
        pointer=primary_pointer,
        snippet=notes,
        score=getattr(row, "score", None),
        metadata={
            "id": fact.id,
            "module": fact.module,
            "status": fact.status,
            "unit": fact.unit,
            "value": value,
            "value_truncated": value_truncated,
            "notes_truncated": notes_truncated,
            "updated_at": fact.updated_at,
            "evidence_pointer": evidence_pointer,
            "evidence_pointer_candidates": evidence_candidates,
            "primary_pointer_kind": _pointer_info(primary_pointer).get("pointer_kind") if primary_pointer else "none",
            "matched_terms": list(getattr(row, "matched_terms", ()) or ()),
            "rank_reasons": list(getattr(row, "rank_reasons", ()) or ()),
        },
    )


def _evidence_pointer_candidates(value: Any, *, limit: int = 4) -> list[str]:
    raw = str(value or "")
    if not raw.strip():
        return []
    candidates: list[str] = []
    for chunk in re.split(r"[;\n,]+", raw):
        text = chunk.strip().strip("'\"`[]{}<>")
        if not text or any(ch.isspace() for ch in text):
            continue
        if text.startswith(("memory://", "kfts://", "http://", "https://")) or _path_from_local_pointer(text) is not None:
            candidates.append(text)
        if len(candidates) >= limit:
            break
    return candidates


def _first_expandable_evidence_pointer(candidates: list[str]) -> str | None:
    for pointer in candidates:
        if _pointer_info(pointer).get("pointer_expandable"):
            return pointer
    return None


def default_context_providers(
    *,
    db_path: str | Path | None = None,
    knowledge_db: str | Path | None = None,
    context_db: str | Path | None = None,
    accepted_memory_store: str | Path | None = None,
    accepted_memory_subject_planes: set[str] | tuple[str, ...] | list[str] | None = None,
    accepted_memory_lifecycles: set[str] | tuple[str, ...] | list[str] | None = None,
    include_board_history: bool = False,
    include_kfts_freshness: bool = False,
    board_rows: list[dict[str, Any]] | None = None,
) -> list[ContextProvider]:
    resolved_context_db = context_db if context_db is not None else knowledge_db
    providers: list[ContextProvider] = [
        BoardContextProvider(db_path=db_path, rows=board_rows),
        FactsProvider(db_path=knowledge_db),
        KftsProvider(db_path=resolved_context_db, include_freshness=include_kfts_freshness),
        ArtifactManifestProvider(db_path=db_path),
        AcceptedMemoryProvider(
            store_root=accepted_memory_store,
            subject_planes=accepted_memory_subject_planes,
            lifecycles=accepted_memory_lifecycles,
        ),
        MemoryProposalProvider(db_path=knowledge_db),
    ]
    if include_board_history:
        providers.append(BoardHistoryProvider(db_path=db_path, rows=board_rows))
    return providers


def default_federator() -> ContextFederator:
    return ContextFederator()


def compile_context_pack(
    query: str,
    *,
    work_id: str | None = None,
    profile: str = "work",
    max_bytes: int | None = None,
    providers: list[ContextProvider] | None = None,
    allow_manual: bool = False,
) -> dict[str, Any]:
    config = config_for_context_profile(profile, allow_manual=allow_manual)
    if max_bytes is not None:
        config = ContextFederatorConfig(
            max_hits=config.max_hits,
            per_provider_limit=config.per_provider_limit,
            max_packet_bytes=min(max(1, int(max_bytes)), config.max_packet_bytes),
            include_board_history=config.include_board_history,
            compact_metadata=config.compact_metadata,
        )
    federator = ContextFederator(providers, config=config)
    packet = federator.search(query, work_id=work_id)
    payload = packet.to_dict()
    sections = _context_pack_sections(payload.get("hits") or [])
    pack = {
        "schema_version": 1,
        "mode": "context_pack",
        "query": payload.get("query"),
        "work_id": payload.get("work_id"),
        "profile": profile,
        "byte_budget": config.max_packet_bytes,
        "truncated": payload.get("truncated"),
        "sections": sections,
        "provider_results": payload.get("provider_results") or [],
        "errors": payload.get("errors") or [],
        "expansion": payload.get("expansion") or {},
    }
    return _bound_context_pack(pack, config.max_packet_bytes)


def _context_pack_sections(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    section_specs = [
        ("current_work", lambda hit: hit.get("source") == "board"),
        ("done_cards", lambda hit: hit.get("source") == "board_history"),
        ("facts", lambda hit: hit.get("source") == "facts"),
        ("knowledge", lambda hit: hit.get("source") == "kfts"),
        ("artifact_manifests", lambda hit: hit.get("source") == "artifact_manifest"),
        ("accepted_memory", lambda hit: hit.get("source") == "accepted_memory"),
        ("memory_proposals", lambda hit: hit.get("source") == "memory_proposals"),
    ]
    sections: list[dict[str, Any]] = []
    assigned: set[int] = set()
    for name, pred in section_specs:
        items = []
        for idx, hit in enumerate(hits):
            if idx in assigned or not pred(hit):
                continue
            assigned.add(idx)
            items.append(hit)
        sections.append({"id": name, "count": len(items), "items": items})
    leftovers = [hit for idx, hit in enumerate(hits) if idx not in assigned]
    if leftovers:
        sections.append({"id": "other", "count": len(leftovers), "items": leftovers})
    return sections


def _bound_context_pack(pack: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    max_bytes = max(1, int(max_bytes))
    while len(json.dumps(pack, sort_keys=True, default=str).encode("utf-8")) > max_bytes:
        for section in reversed(pack.get("sections") or []):
            items = section.get("items")
            if isinstance(items, list) and items:
                items.pop()
                section["count"] = len(items)
                pack["truncated"] = True
                break
        else:
            pack["provider_results"] = []
            pack["errors"] = []
            pack["expansion"] = {}
            if len(json.dumps(pack, sort_keys=True, default=str).encode("utf-8")) <= max_bytes:
                break
            pack["sections"] = []
            pack["truncated"] = True
            break
    return pack


def context_query_profiles() -> list[ContextQueryProfile]:
    return [
        ContextQueryProfile(
            id="brief",
            purpose="startup or chat-turn orientation",
            max_hits=8,
            per_provider_limit=3,
            max_packet_bytes=6_000,
            notes=(
                "Use in session briefs and quick recall checks.",
                "Designed to avoid full-board or full-doc injection.",
            ),
        ),
        ContextQueryProfile(
            id="orient",
            purpose="very light boot/default orientation",
            max_hits=6,
            per_provider_limit=2,
            max_packet_bytes=5_000,
            notes=(
                "Use for promptless startup and short reorientation.",
                "Keep board recall to compact seeds; expand pointers only on demand.",
            ),
        ),
        ContextQueryProfile(
            id="work",
            purpose="focused current/nearby work retrieval",
            max_hits=18,
            per_provider_limit=6,
            max_packet_bytes=20_000,
            notes=(
                "Default for work-item focus and adjacent open/recent done context.",
                "Prefer this before manually reading broad board/doc dumps.",
            ),
        ),
        ContextQueryProfile(
            id="edit-prep",
            purpose="focused implementation context before editing",
            max_hits=20,
            per_provider_limit=6,
            max_packet_bytes=24_000,
            notes=(
                "Use after a specific file/module/work item is known.",
                "Returns board/docs/facts pointers; expand individual pointers explicitly.",
            ),
        ),
        ContextQueryProfile(
            id="impact",
            purpose="bounded impact analysis for code or policy changes",
            max_hits=22,
            per_provider_limit=6,
            max_packet_bytes=26_000,
            notes=(
                "Use for blast-radius checks before broad edits.",
                "Searches board/docs/facts for the implicated surfaces; expand pointers explicitly.",
            ),
        ),
        ContextQueryProfile(
            id="docs",
            purpose="documentation/spec/recommendation retrieval",
            max_hits=24,
            per_provider_limit=8,
            max_packet_bytes=28_000,
            include_board_history=True,
            notes=(
                "Use for spec reconciliation and prior recommendation lookup.",
                "Includes terminal board history so stale spec claims can be cross-checked.",
                "Still returns pointers/snippets only; expand one doc explicitly.",
            ),
        ),
        ContextQueryProfile(
            id="deep",
            purpose="operator-approved broad cross-domain recall",
            max_hits=28,
            per_provider_limit=8,
            max_packet_bytes=32_000,
            include_board_history=True,
            notes=(
                "Use when the operator asks for comprehensive cross-module analysis.",
                "Includes older terminal board work; not a session-start default.",
                "Still bounded below the old broad-packet ceiling; expand pointers explicitly.",
            ),
        ),
        ContextQueryProfile(
            id="code",
            purpose="code/symbol/impact-oriented context",
            max_hits=16,
            per_provider_limit=5,
            max_packet_bytes=24_000,
            notes=(
                "Searches board/docs/facts for code-oriented pointers and prior decisions.",
                "Expand individual file/doc pointers explicitly after the search narrows the surface.",
            ),
        ),
        ContextQueryProfile(
            id="forensic",
            purpose="manual broad forensic recall with bounded snippets",
            max_hits=40,
            per_provider_limit=10,
            max_packet_bytes=40_000,
            include_board_history=True,
            manual_only=True,
            provider_policy="default providers plus terminal board history",
            notes=(
                "Manual opt-in only; never a SessionStart or default work profile.",
                "Use for adversarial audits and roadmap/spec reconciliation before expanding individual pointers.",
                "Still returns snippets/pointers, not full board or full document bodies.",
            ),
        ),
    ]


def config_for_context_profile(profile_id: str, *, allow_manual: bool = False) -> ContextFederatorConfig:
    profiles = {profile.id: profile for profile in context_query_profiles()}
    try:
        profile = profiles[str(profile_id)]
    except KeyError as exc:
        allowed = ", ".join(sorted(profiles))
        raise ValueError(f"unknown context profile {profile_id!r}; expected one of: {allowed}") from exc
    if profile.manual_only and not allow_manual:
        raise ValueError(f"context profile {profile.id!r} requires explicit manual opt-in")
    return ContextFederatorConfig(
        max_hits=profile.max_hits,
        per_provider_limit=profile.per_provider_limit,
        max_packet_bytes=profile.max_packet_bytes,
        include_board_history=profile.include_board_history,
        compact_metadata=True,
    )


def context_query_profiles_report() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": "context_query_profiles",
        "read_only": True,
        "actions_enabled": False,
        "provider_initialization_enabled": False,
        "service_activation_enabled": False,
        "coord_truth_mutation_enabled": False,
        "default_profile": "work",
        "session_start_profile": "orient",
        "operator_deep_profile": "deep",
        "operator_forensic_profile": "forensic",
        "profiles": [asdict(profile) for profile in context_query_profiles()],
    }


def render_context_query_profiles_markdown(report: dict[str, Any] | None = None) -> str:
    payload = report or context_query_profiles_report()
    lines = [
        "# Context Query Profiles",
        f"Read-only: `{payload.get('read_only')}` | Actions: `{payload.get('actions_enabled')}`",
        f"Session-start default: `{payload.get('session_start_profile')}` | Work default: `{payload.get('default_profile')}`",
        "",
    ]
    for profile in payload.get("profiles") or []:
        lines.append(f"- `{profile.get('id')}` — {profile.get('purpose')}")
        lines.append(
            "  "
            f"max_hits={profile.get('max_hits')} · "
            f"per_provider_limit={profile.get('per_provider_limit')} · "
            f"max_packet_bytes={profile.get('max_packet_bytes')}"
        )
        lines.append(f"  Provider policy: {profile.get('provider_policy')}")
        for note in profile.get("notes") or ():
            lines.append(f"  - {note}")
    return "\n".join(lines) + "\n"


def render_markdown(packet: ContextPacket | dict[str, Any]) -> str:
    payload = packet.to_dict() if isinstance(packet, ContextPacket) else packet
    lines = [f"# Federated Context: {payload.get('query') or '(empty query)'}"]
    if payload.get("work_id"):
        lines.append(f"Work: `{payload['work_id']}`")
    lines.append(f"Hits: {len(payload.get('hits') or [])} | Truncated: {payload.get('truncated')}")
    for hit in payload.get("hits") or []:
        pointer = f" -> `{hit.get('pointer')}`" if hit.get("pointer") else ""
        lines.append(f"- [{hit.get('source')}/{hit.get('kind')}] {hit.get('title')}{pointer}")
        if hit.get("snippet"):
            lines.append(f"  {str(hit['snippet']).replace(chr(10), ' ')[:240]}")
    if payload.get("errors"):
        lines.append("\n## Provider Errors")
        for error in payload["errors"]:
            lines.append(f"- {error.get('source')}: {error.get('error')}")
    expansion = payload.get("expansion") or {}
    if expansion:
        lines.append("\n## Expansion")
        for key, value in expansion.items():
            lines.append(f"- {key}: `{value}`")
    return "\n".join(lines) + "\n"


def read_context_pointer(
    pointer: str,
    *,
    max_bytes: int = DEFAULT_POINTER_READ_BYTES,
    neighbor_radius: int = 1,
    neighbor_snippet_bytes: int = 220,
    context_db: str | Path | None = None,
    accepted_memory_store: str | Path | None = None,
) -> dict[str, Any]:
    requested_pointer = str(pointer or "").strip()
    requested_max_bytes = max(1, int(max_bytes))
    max_bytes = min(requested_max_bytes, MAX_POINTER_READ_BYTES)
    if requested_pointer.startswith("accepted-memory://"):
        return _read_accepted_memory_pointer(
            requested_pointer,
            max_bytes=max_bytes,
            requested_max_bytes=requested_max_bytes,
            store_root=(
                Path(accepted_memory_store)
                if accepted_memory_store is not None
                else DEFAULT_ACCEPTED_MEMORY_STORE
            ),
        )
    source_info = _source_pointer_info(requested_pointer)
    if source_info is not None:
        if not source_info.get("pointer_expandable"):
            return {
                "schema_version": 1,
                "mode": "context_pointer_read",
                "read_only": True,
                "actions_enabled": False,
                "pointer": requested_pointer,
                "max_bytes": max_bytes,
                "requested_max_bytes": requested_max_bytes,
                "max_bytes_cap": MAX_POINTER_READ_BYTES,
                "exists": False,
                "truncated": False,
                "metadata": source_info,
                "content": None,
            }
        requested_source_pointer = requested_pointer
        requested_pointer = str(source_info["canonical_pointer"])
    else:
        requested_source_pointer = None
    coord_event_info = _coord_event_pointer_info(requested_pointer)
    if coord_event_info is not None:
        return {
            "schema_version": 1,
            "mode": "context_pointer_read",
            "read_only": True,
            "actions_enabled": False,
            "pointer": coord_event_info.get("canonical_pointer") or requested_pointer,
            "max_bytes": max_bytes,
            "requested_max_bytes": requested_max_bytes,
            "max_bytes_cap": MAX_POINTER_READ_BYTES,
            "exists": coord_event_info.get("pointer_health") == "ok",
            "truncated": False,
            "metadata": coord_event_info,
            "content": None,
        }
    neighbor_radius = max(0, int(neighbor_radius))
    neighbor_snippet_bytes = max(1, int(neighbor_snippet_bytes))
    archive_alias = resolve_archive_pointer_alias(requested_pointer)
    if archive_alias["status"] in {"manifest_invalid", "target_invalid"}:
        return {
            "schema_version": 1,
            "mode": "context_pointer_read",
            "read_only": True,
            "actions_enabled": False,
            "pointer": requested_pointer,
            "max_bytes": max_bytes,
            "requested_max_bytes": requested_max_bytes,
            "max_bytes_cap": MAX_POINTER_READ_BYTES,
            "exists": False,
            "truncated": False,
            "metadata": {"archive_alias_resolution": archive_alias},
            "content": None,
        }
    pointer = str(archive_alias["canonical_pointer"]) if archive_alias["status"] == "unique" else requested_pointer
    extension_alias = _memory_markdown_extension_alias(pointer)
    if extension_alias["status"] == "unique":
        alias = extension_alias
    else:
        direct_path = _path_from_scheme_pointer(pointer, "memory://") if pointer.startswith("memory://") else None
        alias = {"status": "not_alias", "pointer": pointer} if direct_path is not None else resolve_context_pointer_alias(pointer)
    if alias["status"] in {"ambiguous", "unresolved"}:
        return {
            "schema_version": 1,
            "mode": "context_pointer_read",
            "read_only": True,
            "actions_enabled": False,
            "pointer": pointer,
            "max_bytes": max_bytes,
            "requested_max_bytes": requested_max_bytes,
            "max_bytes_cap": MAX_POINTER_READ_BYTES,
            "exists": False,
            "truncated": False,
            "metadata": {"alias_resolution": alias},
            "content": None,
        }
    if alias["status"] == "unique":
        pointer = str(alias["canonical_pointer"])
    pointer = _as_knowledge_pointer(pointer)
    read_kwargs: dict[str, Any] = {"max_bytes": max_bytes}
    if context_db is not None:
        read_kwargs["db_path"] = context_db
    note = kfts.read_note(pointer, **read_kwargs)
    body = note.get("body")
    metadata = {
        key: value
        for key, value in note.items()
        if key
        not in {
            "body",
            "pointer",
        }
        and value not in (None, "")
    }
    if alias["status"] == "unique":
        metadata["alias_resolution"] = alias
    if requested_source_pointer is not None:
        metadata["source_pointer_resolution"] = source_info
        metadata["requested_pointer"] = requested_source_pointer
    if archive_alias["status"] == "unique":
        metadata["archive_alias_resolution"] = archive_alias
        metadata["requested_pointer"] = requested_pointer
    neighbor_kwargs: dict[str, Any] = {
        "radius": neighbor_radius,
        "max_snippet_bytes": neighbor_snippet_bytes,
    }
    if context_db is not None:
        neighbor_kwargs["db_path"] = context_db
    metadata["neighbors"] = kfts.neighbor_sections(
        str(note.get("pointer") or pointer),
        **neighbor_kwargs,
    )
    result = {
        "schema_version": 1,
        "mode": "context_pointer_read",
        "read_only": True,
        "actions_enabled": False,
        "pointer": note.get("pointer") or pointer,
        "max_bytes": max_bytes,
        "requested_max_bytes": requested_max_bytes,
        "max_bytes_cap": MAX_POINTER_READ_BYTES,
        "exists": bool(note.get("exists")),
        "truncated": bool(note.get("truncated")),
        "metadata": metadata,
        "content": body if isinstance(body, str) else None,
    }
    if archive_alias["status"] == "unique":
        result["requested_pointer"] = requested_pointer
        result["resolved_pointer"] = note.get("pointer") or pointer
    if requested_source_pointer is not None:
        result["requested_pointer"] = requested_source_pointer
        result["resolved_pointer"] = note.get("pointer") or pointer
    return result


def _accepted_memory_pointer_parts(pointer: str) -> tuple[str, str, str] | None:
    raw = str(pointer or "").strip()
    prefix = "accepted-memory://"
    if not raw.startswith(prefix):
        return None
    body = raw[len(prefix) :]
    path_part, separator, fragment = body.partition("#atom=")
    if not separator or "/" not in path_part:
        return None
    generation_id, logical_path = path_part.split("/", 1)
    if not generation_id.startswith("accepted-memory-r4-sha256-"):
        return None
    if not logical_path.startswith("claude-project-memory/"):
        return None
    basename = logical_path[len("claude-project-memory/") :]
    if Path(basename).name != basename or not basename.endswith(".md"):
        return None
    if not fragment.startswith("memory-atom-sha256-"):
        return None
    return generation_id, logical_path, fragment


def _read_accepted_memory_pointer(
    pointer: str,
    *,
    max_bytes: int,
    requested_max_bytes: int,
    store_root: Path,
) -> dict[str, Any]:
    parts = _accepted_memory_pointer_parts(pointer)
    if parts is None:
        raise ValueError("invalid accepted-memory pointer")
    generation_id, logical_path, atom_id = parts
    current = accepted_memory_r4.open_current_generation(store_root)
    if current.generation_id != generation_id:
        raise accepted_memory_r4.AcceptedMemoryError(
            "accepted-memory pointer does not name CURRENT generation"
        )
    generation = current.generation_path
    heads = json.loads(
        accepted_memory_r4.stable_read(generation / "note_heads.json")
    ).get("heads")
    if not isinstance(heads, dict) or logical_path not in heads:
        raise accepted_memory_r4.AcceptedMemoryError("accepted-memory pointer note is absent")
    versions = {
        str(row["version_id"]): row
        for row in (
            json.loads(line)
            for line in accepted_memory_r4.stable_read(
                generation / "note_versions.jsonl"
            ).splitlines()
        )
    }
    version = versions.get(str(heads[logical_path]))
    if version is None:
        raise accepted_memory_r4.AcceptedMemoryError(
            "accepted-memory pointer head version is absent"
        )
    atom = next(
        (
            row
            for row in (
                json.loads(line)
                for line in accepted_memory_r4.stable_read(
                    generation / "atoms.jsonl"
                ).splitlines()
            )
            if row.get("atom_id") == atom_id
        ),
        None,
    )
    if atom is None or atom.get("logical_path") != logical_path:
        raise accepted_memory_r4.AcceptedMemoryError("accepted-memory pointer atom is absent")
    if atom.get("note_version_id") != version.get("version_id"):
        raise accepted_memory_r4.AcceptedMemoryError("accepted-memory pointer atom is not current")
    raw = accepted_memory_r4.stable_read(
        generation / "objects" / f"{version['source_sha256']}.md"
    )
    start, end = int(atom["byte_start"]), int(atom["byte_end"])
    span = raw[start:end]
    if accepted_memory_r4.sha256_bytes(span) != atom.get("span_sha256"):
        raise accepted_memory_r4.AcceptedMemoryError("accepted-memory pointer span fence failed")
    selected = span[:max_bytes]
    content = selected.decode("utf-8", errors="ignore")
    return {
        "schema_version": 1,
        "mode": "context_pointer_read",
        "read_only": True,
        "actions_enabled": False,
        "pointer": pointer,
        "max_bytes": max_bytes,
        "requested_max_bytes": requested_max_bytes,
        "max_bytes_cap": MAX_POINTER_READ_BYTES,
        "exists": True,
        "truncated": len(span) > len(selected),
        "metadata": {
            "pointer_kind": "accepted_memory",
            "generation_id": current.generation_id,
            "manifest_sha256": current.manifest_sha256,
            "pointer_sha256": current.pointer_sha256,
            "logical_path": logical_path,
            "note_version_id": version.get("version_id"),
            "atom_id": atom_id,
            "span_sha256": atom.get("span_sha256"),
            "subject_plane": version.get("subject_plane"),
            "lifecycle": version.get("lifecycle"),
            "authority_state": version.get("authority_state"),
            "boot_eligible": bool(version.get("boot_eligible")),
            "byte_start": start,
            "byte_end": end,
            "neighbors": [],
        },
        "content": content,
    }


def render_pointer_read_markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"# Context Pointer: {payload.get('pointer')}",
        f"Read-only: `{payload.get('read_only')}` | Exists: `{payload.get('exists')}` | Truncated: `{payload.get('truncated')}`",
        "",
    ]
    content = payload.get("content")
    if content:
        lines.extend(["```markdown", str(content), "```"])
    return "\n".join(lines) + "\n"


def _hit_from_board_row(
    row: dict[str, Any],
    *,
    reason: str | None = None,
    source: str = "board",
    kind: str = "work_item",
) -> ContextHit:
    title = str(row.get("label") or row.get("title") or row.get("display") or row.get("id") or "board row")
    primary_pointer = row.get("primary_pointer") if isinstance(row.get("primary_pointer"), dict) else None
    if primary_pointer is None:
        primary_pointer = board_context._primary_board_pointer(row)
    pointer = str(primary_pointer.get("value") if primary_pointer else "") or None
    metadata = {
        k: v
        for k, v in row.items()
        if k
        in {
            "id",
            "status",
            "proof_state",
            "assignee",
            "module",
            "sublane",
            "has_done_signal",
            "has_context_pack_ref",
            "card_kind",
            "terminal_status",
            "pointer_health",
            "primary_pointer_exists",
        }
    }
    if primary_pointer:
        metadata["primary_pointer_kind"] = primary_pointer.get("kind")
        metadata["primary_pointer_field"] = primary_pointer.get("field")
    return ContextHit(
        source=source,
        kind=kind,
        title=title,
        pointer=pointer,
        snippet=reason or row.get("note") or row.get("claim_step"),
        score=row.get("score") if isinstance(row.get("score"), (int, float)) else None,
        metadata=metadata,
    )


def _merge_board_row(compact: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    row_id = str(compact.get("id") or compact.get("work_id") or "")
    full = by_id.get(row_id)
    if not full:
        return compact
    merged = dict(full)
    merged.update(compact)
    return merged


def _dedupe_hits(hits: list[ContextHit]) -> list[ContextHit]:
    seen: set[tuple[str, str, str | None]] = set()
    out: list[ContextHit] = []
    for hit in hits:
        work_id = hit.metadata.get("id") if hit.source in {"board", "board_history"} else None
        key = ("board_work", str(work_id), None) if work_id else (hit.source, hit.title, hit.pointer)
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return out


_COMPACT_HIT_METADATA_KEYS = {
    "atom_id",
    "authority_state",
    "boot_eligible",
    "byte_end",
    "byte_start",
    "id",
    "generation_id",
    "lifecycle",
    "logical_path",
    "manifest_sha256",
    "module",
    "note_version_id",
    "status",
    "unit",
    "value",
    "value_truncated",
    "notes_truncated",
    "updated_at",
    "confidence",
    "scope",
    "conflict_count",
    "evidence_pointer",
    "matched_terms",
    "term_coverage",
    "source_tier",
    "freshness_basis",
    "source_path",
    "source_sha256",
    "span_sha256",
    "subject_plane",
    "heading",
    "line_start",
    "line_end",
    "stale_source",
    "pointer_health",
    "pointer_expandable",
    "canonical_pointer",
    "work_id_scope",
}


def _compact_search_result(result: ProviderResult) -> ProviderResult:
    hits = [_compact_context_hit(hit) for hit in result.hits]
    return ProviderResult(
        source=result.source,
        hits=hits,
        elapsed_s=result.elapsed_s,
        truncated=result.truncated,
        error=result.error,
        metadata={},
        returned_before_compaction=result.returned_count(),
    )


def _compact_context_hit(hit: ContextHit) -> ContextHit:
    return ContextHit(
        source=hit.source,
        kind=hit.kind,
        title=hit.title,
        pointer=hit.pointer,
        snippet=hit.snippet,
        score=hit.score,
        metadata={
            key: _compact_hit_metadata_value(key, value)
            for key, value in hit.metadata.items()
            if key in _COMPACT_HIT_METADATA_KEYS
        },
    )


def _compact_hit_metadata_value(key: str, value: Any) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, str):
        limit = 320 if key in {
            "value",
            "evidence_pointer",
            "canonical_pointer",
            "source_path",
        } else 160
        return _bounded_text_bytes(value, limit)
    if isinstance(value, (list, tuple, set)):
        item_limit = 120 if key == "rank_reasons" else 80
        return [_bounded_text_bytes(item, item_limit) for item in list(value)[:8]]
    if isinstance(value, dict):
        return _compact_metadata_dict(value)
    return _bounded_text_bytes(_text_for_json(value), 240)


def _order_hits(hits: list[ContextHit]) -> list[ContextHit]:
    if not hits:
        return []
    hits = [_with_pointer_metadata(hit) for hit in hits]
    source_order = {
        "board": 0,
        "accepted_memory": 1,
        "facts": 2,
        "kfts": 3,
        "board_history": 4,
    }
    ordered = sorted(
        enumerate(hits),
        key=lambda item: (
            source_order.get(item[1].source, 50),
            -float(item[1].score or 0.0) if isinstance(item[1].score, (int, float)) else 0.0,
            item[0],
        ),
    )
    out: list[ContextHit] = []
    for rank, (original_index, hit) in enumerate(ordered, start=1):
        metadata = dict(hit.metadata)
        metadata.setdefault(
            "context_order",
            {
                "rank": rank,
                "original_index": original_index,
                "source_order": source_order.get(hit.source, 50),
                "policy": "provider_order_then_provider_score",
            },
        )
        out.append(
            ContextHit(
                source=hit.source,
                kind=hit.kind,
                title=hit.title,
                pointer=hit.pointer,
                snippet=hit.snippet,
                score=hit.score,
                metadata=metadata,
            )
        )
    return out


def _select_diverse_hits(hits: list[ContextHit], max_hits: int) -> list[ContextHit]:
    max_hits = max(0, int(max_hits))
    if max_hits == 0 or len(hits) <= max_hits:
        return hits[:max_hits]

    selected: list[ContextHit] = []
    selected_ids: set[int] = set()
    seen_sources: set[str] = set()
    for hit in hits:
        if hit.source in seen_sources:
            continue
        selected.append(hit)
        selected_ids.add(id(hit))
        seen_sources.add(hit.source)
        if len(selected) >= max_hits:
            return _renumber_hits(selected)

    for hit in hits:
        if id(hit) in selected_ids:
            continue
        selected.append(hit)
        if len(selected) >= max_hits:
            break
    return _renumber_hits(selected)


def _renumber_hits(hits: list[ContextHit]) -> list[ContextHit]:
    out: list[ContextHit] = []
    for rank, hit in enumerate(hits, start=1):
        metadata = dict(hit.metadata)
        order = dict(metadata.get("context_order") or {})
        order["rank"] = rank
        metadata["context_order"] = order
        out.append(
            ContextHit(
                source=hit.source,
                kind=hit.kind,
                title=hit.title,
                pointer=hit.pointer,
                snippet=hit.snippet,
                score=hit.score,
                metadata=metadata,
            )
        )
    return out


def _bound_hits(hits: list[ContextHit], max_bytes: int) -> tuple[list[ContextHit], bool]:
    out: list[ContextHit] = []
    truncated = False
    for hit in hits:
        probe = [asdict(h) for h in [*out, hit]]
        size = len(json.dumps(probe, sort_keys=True, default=str).encode("utf-8"))
        if size > max_bytes:
            truncated = True
            break
        out.append(hit)
    return out, truncated


def _packet_size(packet: ContextPacket) -> int:
    return len(json.dumps(packet.to_dict(), sort_keys=True, default=str).encode("utf-8"))


def _bound_packet(packet: ContextPacket, max_bytes: int) -> ContextPacket:
    max_bytes = max(1, int(max_bytes))
    if _packet_size(packet) <= max_bytes:
        return packet

    provider_results = _compact_provider_results(packet.provider_results)
    errors = _compact_errors(packet.errors)
    query = _bounded_text_bytes(packet.query, 512)
    work_id = _bounded_text_bytes(packet.work_id, 160) if packet.work_id else None

    hits = list(packet.hits)
    while hits:
        hits.pop()
        candidate = ContextPacket(
            query=query,
            work_id=work_id,
            hits=hits,
            provider_results=provider_results,
            errors=errors,
            truncated=True,
            expansion=_expansion(query, work_id, hits),
        )
        if _packet_size(candidate) <= max_bytes:
            return candidate

    for candidate in (
        ContextPacket(
            query=query,
            work_id=work_id,
            hits=[],
            provider_results=provider_results,
            errors=errors,
            truncated=True,
            expansion={},
        ),
        ContextPacket(
            query=_bounded_text_bytes(query, 240),
            work_id=work_id,
            hits=[],
            provider_results=provider_results,
            errors=[],
            truncated=True,
            expansion={},
        ),
        ContextPacket(
            query=_bounded_text_bytes(query, 120),
            work_id=work_id,
            hits=[],
            provider_results=[],
            errors=[],
            truncated=True,
            expansion={},
        ),
        ContextPacket(
            query="",
            work_id=None,
            hits=[],
            provider_results=[],
            errors=[],
            truncated=True,
            expansion={},
        ),
    ):
        if _packet_size(candidate) <= max_bytes:
            return candidate
    return candidate


def _compact_provider_results(results: list[ProviderResult]) -> list[ProviderResult]:
    compact: list[ProviderResult] = []
    for result in results[:8]:
        compact.append(
            ProviderResult(
                source=_bounded_text_bytes(result.source, 80),
                hits=[],
                elapsed_s=result.elapsed_s,
                truncated=True,
                error=_bounded_text_bytes(result.error, 240) if result.error else None,
                metadata=_compact_metadata(result.metadata),
                returned_before_compaction=result.returned_count(),
            )
        )
    return compact


def _compact_errors(errors: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for error in errors[:8]:
        out.append(
            {
                "source": _bounded_text_bytes(error.get("source"), 80),
                "error": _bounded_text_bytes(error.get("error"), 240),
            }
        )
    return out


def _compact_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in list((metadata or {}).items())[:6]:
        key_text = _bounded_text_bytes(key, 80)
        if isinstance(value, (int, float, bool)) or value is None:
            out[key_text] = value
        elif key in {"index_stats", "index_refresh"} and isinstance(value, dict):
            out[key_text] = _compact_metadata_dict(value)
        else:
            out[key_text] = _bounded_text_bytes(_text_for_json(value), 240)
    return out


def _compact_metadata_dict(value: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, nested in list(value.items())[:8]:
        key_text = _bounded_text_bytes(key, 80)
        if isinstance(nested, (int, float, bool)) or nested is None:
            out[key_text] = nested
        elif isinstance(nested, str):
            out[key_text] = _bounded_text_bytes(nested, 240)
        elif isinstance(nested, list):
            out[key_text] = [_bounded_text_bytes(item, 80) for item in nested[:6]]
        else:
            out[key_text] = _bounded_text_bytes(_text_for_json(nested), 240)
    return out


def _text_for_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, default=str, ensure_ascii=True)
    except TypeError:
        return str(value)


def _bounded_text_bytes(value: Any, max_bytes: int) -> str:
    text = "" if value is None else str(value)
    max_bytes = max(1, int(max_bytes))
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _clip_text_with_flag(value: Any, max_chars: int) -> tuple[str | None, bool]:
    if value in (None, ""):
        return None, False
    text = " ".join(str(value).split())
    max_chars = max(4, int(max_chars))
    if len(text) <= max_chars:
        return text, False
    return text[: max_chars - 3].rstrip() + "...", True


def _expansion(query: str, work_id: str | None, hits: list[ContextHit] | None = None) -> dict[str, str]:
    expansion = {
        "board_search_api": "coordharness.coord.board_context.search_rows",
        "knowledge_search_api": "coordharness.knowledge.context_federator.compile_context_pack",
        "query": query,
    }
    if work_id:
        expansion["work_id"] = work_id
    pointer = _first_readable_pointer(hits or [])
    if pointer:
        expansion["read_pointer_api"] = "coordharness.knowledge.context_federator.read_context_pointer"
        expansion["read_pointer"] = pointer
        expansion["read_max_bytes"] = str(DEFAULT_POINTER_READ_BYTES)
    return expansion


def _first_readable_pointer(hits: list[ContextHit]) -> str | None:
    for hit in hits:
        pointer = str(hit.pointer or "")
        if pointer.startswith(("accepted-memory://", "memory://", "kfts://")) and _pointer_info(pointer).get("pointer_expandable"):
            return pointer
    return None


def _with_pointer_metadata(hit: ContextHit) -> ContextHit:
    if not hit.pointer:
        return hit
    metadata = dict(hit.metadata)
    info = _pointer_info(hit.pointer)
    for key, value in info.items():
        metadata.setdefault(key, value)
    pointer = str(info.get("canonical_pointer") or hit.pointer)
    return ContextHit(
        source=hit.source,
        kind=hit.kind,
        title=hit.title,
        pointer=pointer,
        snippet=hit.snippet,
        score=hit.score,
        metadata=metadata,
    )


def _source_pointer_info(pointer: str) -> dict[str, Any] | None:

    raw = str(pointer or "").strip()
    if not raw.startswith("src:"):
        return None
    body = raw[len("src:") :]
    path_part, separator, fragment = body.partition("#")
    path_part = path_part.strip()
    path = Path(path_part)
    if (
        not path_part
        or path.is_absolute()
        or ".." in path.parts
        or "://" in path_part
        or not path.suffix
        or not path.parts
        or path.parts[0] not in _CONTAINED_REPO_ROOTS
    ):
        return {
            "pointer_kind": "source",
            "pointer_health": "unresolved",
            "pointer_expandable": False,
        }
    candidate = REPO / path
    try:
        candidate.resolve(strict=False).relative_to(REPO.resolve())
    except ValueError:
        return {
            "pointer_kind": "source",
            "pointer_health": "unresolved",
            "pointer_expandable": False,
        }
    if candidate.exists() and not candidate.is_file():
        return {
            "pointer_kind": "source",
            "pointer_health": "unresolved",
            "pointer_expandable": False,
        }
    exists = candidate.is_file()
    canonical = f"memory://{path.as_posix()}"
    if separator:
        canonical = f"{canonical}#{fragment}"
    return {
        "pointer_kind": "source",
        "pointer_health": "ok" if exists else "missing",
        "pointer_expandable": exists,
        "pointer_path_exists": exists,
        "canonical_pointer": canonical,
        "original_pointer": raw,
    }


def _coord_event_pointer_info(pointer: str) -> dict[str, Any] | None:

    raw = str(pointer or "").strip()
    match = _COORD_EVENT_POINTER_RE.fullmatch(raw)
    legacy = False
    if match is None:
        match = _LEGACY_COORD_EVENT_POINTER_RE.fullmatch(raw)
        legacy = match is not None
    if match is None:
        if raw.startswith(("coord://event/", "coord:event:", "memory://coord-event-")):
            return {
                "pointer_kind": "coord_event",
                "pointer_health": "unresolved",
                "pointer_expandable": False,
            }
        return None
    event_id = int(match.group(1))
    canonical = f"coord://event/{event_id}"
    try:
        conn = sqlite3.connect(f"file:{Path(DEFAULT_COORD_DB)}?mode=ro", uri=True)
        try:
            exists = conn.execute(
                "SELECT 1 FROM events WHERE event_id=? LIMIT 1", (event_id,)
            ).fetchone() is not None
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return {
            "pointer_kind": "coord_event",
            "pointer_health": "unresolved",
            "pointer_expandable": False,
            "canonical_pointer": canonical,
            "original_pointer": raw,
        }
    return {
        "pointer_kind": "coord_event",
        "pointer_health": "ok" if exists else "missing",
        "pointer_expandable": False,
        "pointer_path_exists": exists,
        "canonical_pointer": canonical,
        "original_pointer": raw,
        "alias_resolution": "legacy_coord_event" if legacy else "canonical_coord_event",
    }


def _pointer_info(pointer: str) -> dict[str, Any]:
    raw = str(pointer or "").strip()
    if not raw:
        return {"pointer_health": "none", "pointer_expandable": False}
    source_info = _source_pointer_info(raw)
    if source_info is not None:
        return source_info
    coord_event_info = _coord_event_pointer_info(raw)
    if coord_event_info is not None:
        return coord_event_info
    if raw.startswith("accepted-memory://"):
        valid = _accepted_memory_pointer_parts(raw) is not None
        return {
            "pointer_kind": "accepted_memory",
            "pointer_health": "hash_fenced" if valid else "unresolved",
            "pointer_expandable": valid,
        }
    if raw.startswith("kfts://"):
        path = _path_from_scheme_pointer(raw, "kfts://")
        if path is None:
            return {"pointer_kind": "kfts", "pointer_health": "unresolved", "pointer_expandable": False}
        archive_alias = resolve_archive_pointer_alias(raw)
        if archive_alias["status"] == "unique":
            return {
                "pointer_kind": "kfts",
                "pointer_health": "archived_alias",
                "pointer_expandable": True,
                "pointer_path_exists": True,
                "canonical_pointer": archive_alias["canonical_pointer"],
                "archive_alias_resolution": archive_alias,
                "original_pointer": raw,
            }
        if archive_alias["status"] in {"manifest_invalid", "target_invalid"}:
            return {
                "pointer_kind": "kfts",
                "pointer_health": "unresolved",
                "pointer_expandable": False,
                "archive_alias_resolution": archive_alias,
            }
        exists = path.exists()
        return {
            "pointer_kind": "kfts",
            "pointer_health": "ok" if exists else "missing",
            "pointer_expandable": exists,
            "pointer_path_exists": exists,
        }
    if raw.startswith("memory://"):
        direct_path = _path_from_scheme_pointer(raw, "memory://")
        extension_alias = _memory_markdown_extension_alias(raw)
        if extension_alias["status"] == "unique":
            canonical = str(extension_alias["canonical_pointer"])
            path = _path_from_scheme_pointer(canonical, "memory://")
            return {
                "pointer_kind": "memory",
                "pointer_health": "ok",
                "pointer_expandable": True,
                "pointer_path_exists": True,
                "canonical_pointer": canonical,
                "alias_resolution": extension_alias,
                "original_pointer": raw,
            }
        archive_alias = resolve_archive_pointer_alias(raw)
        if archive_alias["status"] == "unique":
            return {
                "pointer_kind": "memory",
                "pointer_health": "archived_alias",
                "pointer_expandable": True,
                "pointer_path_exists": True,
                "canonical_pointer": archive_alias["canonical_pointer"],
                "archive_alias_resolution": archive_alias,
                "original_pointer": raw,
            }
        if archive_alias["status"] in {"manifest_invalid", "target_invalid"}:
            return {
                "pointer_kind": "memory",
                "pointer_health": "unresolved",
                "pointer_expandable": False,
                "archive_alias_resolution": archive_alias,
            }
        alias = {"status": "not_alias", "pointer": raw} if direct_path is not None else resolve_context_pointer_alias(raw)
        if alias["status"] == "ambiguous":
            return {
                "pointer_kind": "memory",
                "pointer_health": "ambiguous",
                "pointer_expandable": False,
                "alias_resolution": alias,
                "alias_candidates": alias.get("candidates", []),
            }
        if alias["status"] == "unresolved":
            return {
                "pointer_kind": "memory",
                "pointer_health": "unresolved",
                "pointer_expandable": False,
                "alias_resolution": alias,
            }
        if alias["status"] == "unique":
            raw = str(alias["canonical_pointer"])
        path = _path_from_scheme_pointer(raw, "memory://")
        if path is None:
            return {"pointer_kind": "memory", "pointer_health": "unresolved", "pointer_expandable": False}
        exists = path.exists()
        out = {
            "pointer_kind": "memory",
            "pointer_health": "ok" if exists else "missing",
            "pointer_expandable": exists,
            "pointer_path_exists": exists,
        }
        if alias["status"] == "unique":
            out["alias_resolution"] = alias
            out["canonical_pointer"] = alias["canonical_pointer"]
        return out
    for scheme, kind in (("file://", "file"), ("artifact://", "artifact")):
        if raw.startswith(scheme):
            path = _path_from_repo_relative_scheme_pointer(raw, scheme)
            if path is None:
                return {
                    "pointer_kind": kind,
                    "pointer_health": "unresolved",
                    "pointer_expandable": False,
                }
            exists = path.exists()
            return {
                "pointer_kind": kind,
                "pointer_health": "ok" if exists else "missing",
                "pointer_expandable": exists,
                "pointer_path_exists": exists,
            }
    if raw.startswith(("http://", "https://")):
        return {"pointer_kind": "url", "pointer_health": "external", "pointer_expandable": False}
    path = _path_from_local_pointer(raw)
    if path is not None:
        exists = path.exists()
        if not exists:
            archive_alias = resolve_archive_pointer_alias(raw)
            if archive_alias["status"] == "unique":
                return {
                    "pointer_kind": "path",
                    "pointer_health": "archived_alias",
                    "pointer_expandable": True,
                    "pointer_path_exists": True,
                    "canonical_pointer": archive_alias["canonical_pointer"],
                    "archive_alias_resolution": archive_alias,
                    "original_pointer": raw,
                }
            if archive_alias["status"] in {"manifest_invalid", "target_invalid"}:
                return {
                    "pointer_kind": "path",
                    "pointer_health": "unresolved",
                    "pointer_expandable": False,
                    "archive_alias_resolution": archive_alias,
                }
        return {
            "pointer_kind": "path",
            "pointer_health": "ok" if exists else "missing",
            "pointer_expandable": exists,
            "pointer_path_exists": exists,
        }
    return {"pointer_kind": "unknown", "pointer_health": "unresolved", "pointer_expandable": False}


def _path_from_memory_pointer(pointer: str) -> Path | None:
    path = _path_from_scheme_pointer(pointer, "memory://")
    if path is not None and path.exists():
        return path
    extension_alias = _memory_markdown_extension_alias(pointer)
    if extension_alias["status"] == "unique":
        return _path_from_scheme_pointer(str(extension_alias["canonical_pointer"]), "memory://")
    archive_alias = resolve_archive_pointer_alias(pointer)
    if archive_alias["status"] == "unique":
        return _path_from_scheme_pointer(str(archive_alias["canonical_pointer"]), "memory://")
    alias = resolve_context_pointer_alias(pointer)
    if alias["status"] == "unique":
        pointer = str(alias["canonical_pointer"])
    elif alias["status"] in {"ambiguous", "unresolved"}:
        return None
    return _path_from_scheme_pointer(pointer, "memory://")


def _path_from_scheme_pointer(pointer: str, scheme: str) -> Path | None:
    raw = pointer[len(scheme) :].split("#", 1)[0].strip()
    return _path_from_local_pointer(raw)


def _path_from_repo_relative_scheme_pointer(pointer: str, scheme: str) -> Path | None:
    raw = (
        pointer[len(scheme) :]
        .split("#", 1)[0]
        .strip()
        .strip("'\"`[]{}<>")
        .rstrip(".,;:")
    )
    raw = _strip_local_pointer_line_suffix(raw)
    if not raw:
        return None
    path = Path(raw)
    first = path.parts[0] if path.parts else ""
    if (
        path.is_absolute()
        or ".." in path.parts
        or (first not in _CONTAINED_REPO_ROOTS and raw not in _CONTAINED_ROOT_FILES)
    ):
        return None
    return _contained_read_path(path)


def _path_from_local_pointer(pointer: str) -> Path | None:
    raw = pointer.split("#", 1)[0].strip().strip("'\"`[]{}<>").rstrip(".,;:")
    if not raw or "://" in raw:
        return None
    raw = _strip_local_pointer_line_suffix(raw)
    path = Path(raw).expanduser()
    if path.is_absolute():
        return _contained_read_path(path)
    first = path.parts[0] if path.parts else ""
    if first not in _CONTAINED_REPO_ROOTS and raw not in _CONTAINED_ROOT_FILES:
        return None
    return _contained_read_path(path)


def _as_knowledge_pointer(pointer: str) -> str:
    raw = str(pointer or "").strip()
    if raw.startswith(("memory://", "kfts://")):
        return raw
    path_part, separator, fragment = raw.partition("#")
    path = _path_from_local_pointer(path_part)
    if path is None:
        return raw
    try:
        rel = path.resolve(strict=False).relative_to(REPO.resolve())
    except ValueError:
        return raw
    out = f"memory://{rel.as_posix()}"
    return f"{out}#{fragment}" if separator else out


def _archive_pointer_parts(pointer: str) -> tuple[str, str, str | None] | None:
    raw = str(pointer or "").strip()
    scheme = ""
    for candidate in ("memory://", "kfts://"):
        if raw.startswith(candidate):
            scheme = candidate
            raw = raw[len(candidate) :]
            break
    if "://" in raw:
        return None
    path_part, separator, fragment = raw.partition("#")
    path = _path_from_local_pointer(path_part)
    if path is None:
        return None
    try:
        rel = path.resolve(strict=False).relative_to(REPO.resolve()).as_posix()
    except ValueError:
        return None
    if not rel.startswith("docs/"):
        return None
    return scheme, rel, fragment if separator else None


def _archive_alias_manifest_path() -> Path:
    configured = Path(DEFAULT_ARCHIVE_POINTER_ALIAS_MANIFEST)
    resolved = _contained_read_path(configured)
    return resolved if resolved is not None else REPO / "docs" / "archive" / "_PATH_ALIASES.json"


def _load_archive_pointer_aliases() -> tuple[dict[str, dict[str, str]] | None, str | None, Path]:
    manifest_path = _archive_alias_manifest_path()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}", manifest_path
    if payload.get("schema") != ARCHIVE_POINTER_ALIAS_SCHEMA or not isinstance(payload.get("mappings"), list):
        return None, "schema_or_mappings_invalid", manifest_path
    mappings: dict[str, dict[str, str]] = {}
    for raw in payload["mappings"]:
        if not isinstance(raw, dict):
            return None, "mapping_not_object", manifest_path
        source = str(raw.get("source") or "")
        target = str(raw.get("target") or "")
        target_sha256 = str(raw.get("target_sha256") or "").lower()
        mapping_id = str(raw.get("mapping_id") or "")
        for value in (source, target):
            parts = Path(value).parts
            if (
                not value.startswith("docs/")
                or value != Path(value).as_posix()
                or not parts
                or any(part in {"", ".", ".."} for part in parts)
                or "#" in value
                or "://" in value
            ):
                return None, "mapping_path_invalid", manifest_path
        if not target.startswith("docs/archive/") or source.startswith("docs/archive/"):
            return None, "mapping_scope_invalid", manifest_path
        if not re.fullmatch(r"[a-f0-9]{64}", target_sha256) or not mapping_id:
            return None, "mapping_fence_invalid", manifest_path
        if source in mappings:
            return None, "duplicate_source", manifest_path
        mappings[source] = {
            "source": source,
            "target": target,
            "target_sha256": target_sha256,
            "mapping_id": mapping_id,
        }
    return mappings, None, manifest_path


def resolve_archive_pointer_alias(pointer: str) -> dict[str, Any]:
    parts = _archive_pointer_parts(pointer)
    if parts is None:
        return {"status": "not_alias", "pointer": pointer}
    scheme, source_rel, fragment = parts
    source_path = REPO / source_rel
    if source_path.is_file():
        return {"status": "not_alias", "pointer": pointer, "reason": "source_still_exists"}
    mappings, error, manifest_path = _load_archive_pointer_aliases()
    if mappings is None:
        if str(error or "").startswith("FileNotFoundError:"):
            return {
                "status": "not_alias",
                "pointer": pointer,
                "source_path": source_rel,
                "manifest_unavailable": str(manifest_path),
            }
        return {
            "status": "manifest_invalid",
            "pointer": pointer,
            "source_path": source_rel,
            "manifest_path": str(manifest_path),
            "reason": error,
        }
    mapping = mappings.get(source_rel)
    if mapping is None:
        return {"status": "not_alias", "pointer": pointer, "source_path": source_rel}
    target_path = REPO / mapping["target"]
    if not target_path.is_file() or _sha256_file(target_path) != mapping["target_sha256"]:
        return {
            "status": "target_invalid",
            "pointer": pointer,
            "source_path": source_rel,
            "resolved_path": mapping["target"],
            "manifest_path": str(manifest_path),
            "mapping_id": mapping["mapping_id"],
            "reason": "target_missing_or_hash_mismatch",
        }
    canonical = f"{scheme}{mapping['target']}" if scheme else mapping["target"]
    if fragment is not None:
        canonical = f"{canonical}#{fragment}"
    return {
        "status": "unique",
        "resolution_kind": "archive_manifest_exact_path",
        "pointer": pointer,
        "original_pointer": pointer,
        "canonical_pointer": canonical,
        "source_path": source_rel,
        "resolved_path": mapping["target"],
        "target_sha256": mapping["target_sha256"],
        "manifest_path": str(manifest_path),
        "manifest_schema": ARCHIVE_POINTER_ALIAS_SCHEMA,
        "mapping_id": mapping["mapping_id"],
    }


def _strip_local_pointer_line_suffix(raw: str) -> str:
    line_match = re.match(r"^(.+\.[A-Za-z0-9]+):\d+(?:-\d+)?(?:\([^)]*\))?$", raw)
    if line_match:
        return line_match.group(1)
    paren_match = re.match(r"^(.+\.[A-Za-z0-9]+)\([^)]*\)$", raw)
    if paren_match:
        return paren_match.group(1)
    return raw


def _memory_alias_parts(pointer: str) -> tuple[str, str | None] | None:
    raw = str(pointer or "").strip()
    if not raw.startswith("memory://"):
        return None
    body = raw[len("memory://") :].strip()
    if not body:
        return None
    slug, fragment = body.split("#", 1) if "#" in body else (body, None)
    slug = slug.strip().strip("'\"`[]{}<>.,;:")
    if not slug or "/" in slug or "\\" in slug:
        return None
    if Path(slug).suffix and Path(slug).suffix.lower() != ".md":
        return None
    if slug.lower().endswith(".md"):
        slug = slug[:-3]
    return slug, fragment or None


def _memory_alias_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _pointer_for_memory_mirror_file(path: Path, *, fragment: str | None = None) -> str:
    try:
        rel = path.resolve().relative_to(REPO.resolve())
    except ValueError:
        rel = path
    pointer = f"memory://{rel.as_posix()}"
    return f"{pointer}#{fragment}" if fragment else pointer


def _memory_alias_candidates(slug: str, *, fragment: str | None = None) -> list[str]:
    mirror = REPO / kfts.MEMORY_MIRROR_REL
    if not mirror.is_dir():
        return []
    key = _memory_alias_key(slug)
    if not key:
        return []
    candidates: list[str] = []
    for path in sorted(mirror.glob("*.md")):
        aliases = {
            _memory_alias_key(path.stem),
            _memory_alias_key(path.name.removesuffix(".md")),
        }
        if key in aliases:
            candidates.append(_pointer_for_memory_mirror_file(path, fragment=fragment))
    return sorted(set(candidates))


def _repo_markdown_alias_candidates(slug: str, *, fragment: str | None = None) -> list[str]:

    key = _memory_alias_key(slug)
    if not key:
        return []
    candidates: list[str] = []
    for root in (REPO / "docs",):
        if not root.is_dir():
            continue
        for path in root.rglob("*.md"):
            if _memory_alias_key(path.stem) == key:
                candidates.append(_pointer_for_memory_mirror_file(path, fragment=fragment))
    return sorted(set(candidates))


def _memory_markdown_extension_alias(pointer: str) -> dict[str, Any]:

    raw = str(pointer or "").strip()
    if not raw.startswith("memory://"):
        return {"status": "not_alias", "pointer": pointer}
    body = raw[len("memory://") :]
    path_part, separator, fragment = body.partition("#")
    if not path_part or Path(path_part).suffix:
        return {"status": "not_alias", "pointer": pointer}
    path = _path_from_local_pointer(path_part)
    if path is None or path.exists():
        return {"status": "not_alias", "pointer": pointer}
    markdown = path.with_suffix(".md")
    try:
        rel = markdown.resolve(strict=False).relative_to(REPO.resolve())
    except ValueError:
        return {"status": "not_alias", "pointer": pointer}
    if not markdown.is_file() or not rel.as_posix().startswith(("docs/", "docs/")):
        return {"status": "unresolved", "pointer": pointer}
    canonical = f"memory://{rel.as_posix()}"
    if separator:
        canonical = f"{canonical}#{fragment}"
    return {
        "status": "unique",
        "pointer": pointer,
        "canonical_pointer": canonical,
        "resolution_kind": "exact_repo_markdown_extension",
    }


def resolve_context_pointer_alias(pointer: str) -> dict[str, Any]:
    parts = _memory_alias_parts(pointer)
    if parts is None:
        return {"status": "not_alias", "pointer": pointer}
    slug, fragment = parts
    candidates = sorted(
        set(
            _memory_alias_candidates(slug, fragment=fragment)
            + _repo_markdown_alias_candidates(slug, fragment=fragment)
        )
    )
    if len(candidates) == 1:
        return {
            "status": "unique",
            "pointer": pointer,
            "alias": slug,
            "canonical_pointer": candidates[0],
        }
    if len(candidates) > 1:
        return {
            "status": "ambiguous",
            "pointer": pointer,
            "alias": slug,
            "candidates": candidates[:8],
        }
    return {"status": "unresolved", "pointer": pointer, "alias": slug, "candidates": []}


def _row_is_active_or_recent(row: dict[str, Any], *, now: float | None = None, recent_done_days: int = ACTIVE_POINTER_RECENT_DONE_DAYS) -> bool:
    status = str(row.get("status") or row.get("intent_state") or "").strip().lower()
    if not status:
        return False
    if status not in {"done", "failed", "archived", "superseded", "cancelled", "canceled", "closed", "skipped"}:
        return True
    recent_window_s = max(0, int(recent_done_days)) * 86400
    if recent_window_s <= 0:
        return False
    now = now or time.time()
    for key in ("completed_at", "finished_at", "closed_at", "done_at"):
        raw = row.get(key)
        if raw in (None, ""):
            continue
        try:
            if isinstance(raw, (int, float)):
                ts = float(raw)
            else:
                from datetime import datetime

                ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            continue
        if now - ts <= recent_window_s:
            return True
    return False


def active_board_pointer_resolution_warnings(
    rows: list[dict[str, Any]] | None = None,
    *,
    recent_done_days: int = ACTIVE_POINTER_RECENT_DONE_DAYS,
) -> list[str]:
    rows = rows if rows is not None else board_context.load_rows()
    found: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not _row_is_active_or_recent(row, recent_done_days=recent_done_days):
            continue
        rid = str(row.get("work_id") or row.get("id") or row.get("roadmap_id") or "?").strip() or "?"
        for candidate in board_context._board_pointer_candidates(row):
            pointer = str(candidate.get("value") or "").strip()
            field = str(candidate.get("field") or "pointer")
            if field != "context_pack_ref":
                continue
            info = _pointer_info(pointer)
            health = str(info.get("pointer_health") or "")
            if health not in {"missing", "unresolved", "ambiguous"}:
                continue
            detail = f" health={health}"
            if info.get("alias_candidates"):
                detail += f" candidates={info['alias_candidates']}"
            found.append(f"context pointer gate: {rid} {field} {pointer}{detail}")
    return found
