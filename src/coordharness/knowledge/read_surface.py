"""Read-only surface over the knowledge/memory subsystem.

This module is the single implementation behind the MCP knowledge read tools
(``facts_query``, ``knowledge_index_status``, ``memory_proposals_list``,
``memory_proposals_get``). It lives here rather than in
``coord/mcp_coord_server.py`` so a future CLI wrapper can call exactly the same
functions and return exactly the same payloads -- MCP and CLI stay in parity by
construction instead of by convention.

Every function is read-only against the knowledge store and returns a
structured payload carrying its own provenance (which store file, which table,
which row key) so a caller never has to guess where a value came from.

Read-only is a measured property here, not an assumption:

* ``facts.query_facts`` / ``facts.stats`` open the store via ``facts._read_conn``
  (``mode=ro`` URI) and return ``[]`` / zeroes when the file is absent.
* ``kfts.index_stats`` opens via ``kfts._conn_ro`` (``mode=ro`` + ``PRAGMA
  query_only=ON``) and tolerates an absent file.
* ``memory_proposals.list_proposals`` / ``get_proposal`` do **not** have a
  read-only path: both go through ``memory_proposals._conn``, which does
  ``mkdir(parents=True)`` and ``executescript(SCHEMA_SQL)``. That writes in two
  distinct situations, not one: against a missing path it CREATES the file, and
  against an EXISTING store that has no ``memory_proposals`` table it CREATES
  THE TABLE (measured: page_count 17 -> 23 on a facts-only store). A knowledge
  store built by ``facts.init_db`` alone is exactly that shape, so an
  existence-only guard is not enough. The proposal readers below therefore
  require the table itself to already be present and report
  ``source.table_present = False`` otherwise. Removing that guard reintroduces
  a write side effect on a read path.

Failure is closed rather than loud: an absent, unreadable (not a sqlite file)
or schema-less store yields an empty result whose ``source`` block says which
of those it was, instead of letting a raw ``sqlite3`` error escape to the
caller as a stack trace.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from coordharness.knowledge import facts, kfts, memory_proposals

SCHEMA_VERSION = 1

DEFAULT_FACTS_LIMIT = 50
MAX_FACTS_LIMIT = 200
DEFAULT_PROPOSALS_LIMIT = 25
MAX_PROPOSALS_LIMIT = 100

_KNOWLEDGE_STORE = "knowledge_db"


def _resolve_store(db_path: str | Path | None) -> Path:
    """Resolve the knowledge store path (facts, kfts and proposals share one file)."""
    target = Path(db_path) if db_path is not None else Path(kfts.DEFAULT_INDEX_DB)
    return target.resolve()


def _probe_store(path: Path, table: str) -> tuple[bool, bool, bool]:
    """Return ``(file_present, readable, table_present)`` without writing anything.

    Opened ``mode=ro`` + ``PRAGMA query_only=ON`` so the probe itself cannot be
    the thing that mutates the store. A file that is not a sqlite database
    reports ``readable=False`` rather than raising, which is what lets the
    callers below fail closed instead of leaking a ``sqlite3.DatabaseError``.
    """
    if not path.is_file():
        return False, False, False
    uri = f"file:{quote(path.as_posix())}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return True, False, False
    try:
        conn.execute("PRAGMA query_only=ON")
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=? LIMIT 1",
            (table,),
        ).fetchone()
    except sqlite3.DatabaseError:
        return True, False, False
    finally:
        conn.close()
    return True, True, row is not None


def _store_descriptor(
    path: Path, table: str, *, present: bool, readable: bool, table_present: bool
) -> dict[str, Any]:
    return {
        "store": _KNOWLEDGE_STORE,
        "path": str(path),
        "table": table,
        "present": present,
        "readable": readable,
        "table_present": table_present,
    }


def _bounded_limit(limit: Any, *, default: int, maximum: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError, OverflowError):
        # OverflowError is float('inf'); it is neither a valid limit nor a
        # reason to hand the caller a traceback.
        return default
    return max(1, min(value, maximum))


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _validated_choice(value: str | None, allowed: tuple[str, ...], field: str) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    normalized = text.lower()
    if normalized not in allowed:
        raise ValueError(f"bad {field} {text!r}; allowed: {', '.join(allowed)}")
    return normalized


def _envelope(mode: str, store: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "read_only": True,
        "actions_enabled": False,
        "source": store,
    }


def _fact_row(fact: facts.Fact) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": fact.id,
        "statement": fact.statement,
        "value": fact.value,
        "unit": fact.unit,
        "status": fact.status,
        "module": fact.module,
        "evidence_pointer": fact.evidence_pointer,
        "supersedes": fact.supersedes,
        "superseded_by": fact.superseded_by,
        "owner_lane": fact.owner_lane,
        "updated_at": fact.updated_at,
        "notes": fact.notes,
        "valid_from": fact.valid_from,
        "valid_to": fact.valid_to,
    }
    lifecycle = facts.fact_lifecycle(fact)
    if lifecycle is not None:
        row["fact_lifecycle"] = lifecycle
        row["retirement_marking"] = lifecycle["retirement_marking"]
    return row


def _proposal_row(proposal: memory_proposals.MemoryProposal) -> dict[str, Any]:
    return {
        "id": proposal.id,
        "content_hash": proposal.content_hash,
        "kind": proposal.kind,
        "statement": proposal.statement,
        "value": proposal.value,
        "confidence": proposal.confidence,
        "scope": proposal.scope,
        "status": proposal.status,
        "evidence_pointer": proposal.evidence_pointer,
        "provenance": proposal.provenance,
        "tags": list(proposal.tags),
        "source_actor": proposal.source_actor,
        "source_thread_id": proposal.source_thread_id,
        "source_work_id": proposal.source_work_id,
        "seen_count": proposal.seen_count,
        "created_at": proposal.created_at,
        "updated_at": proposal.updated_at,
        "reviewed_by": proposal.reviewed_by,
        "reviewed_at": proposal.reviewed_at,
        "review_note": proposal.review_note,
    }


def facts_query(
    module: str | None = None,
    status: str | None = None,
    text: str | None = None,
    limit: int = DEFAULT_FACTS_LIMIT,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Structured filter over the fact ledger.

    Complements the fuzzy ``facts_lookup`` tool: ``module`` and ``status`` are
    exact filters. Passing ``text`` delegates to the ranked search path inside
    ``facts.query_facts``, so the reply reports ``match_mode`` to make clear
    whether rows came back by exact filter or by ranked text match.
    """
    store_path = _resolve_store(db_path)
    present, readable, table_present = _probe_store(store_path, "facts")
    bounded = _bounded_limit(limit, default=DEFAULT_FACTS_LIMIT, maximum=MAX_FACTS_LIMIT)
    module_filter = _clean_text(module)
    status_filter = _validated_choice(status, facts.STATUSES, "status")
    text_filter = _clean_text(text)

    # A text query is tokenised upstream before it reaches SQL. Terms that
    # tokenise to nothing (punctuation, a lone LIKE wildcard, a stopword) make
    # facts.search_facts fall back to "no term filter", which returns the whole
    # ledger. Reporting those rows as text matches would assert a match the
    # data does not carry, so the empty-term case is named and returns nothing.
    text_terms = tuple(facts._search_terms(text_filter)) if text_filter else ()
    text_unsearchable = bool(text_filter) and not text_terms

    if text_unsearchable:
        match_mode = "text_no_searchable_terms"
    elif text_filter:
        match_mode = "text_search"
    else:
        match_mode = "structured_filter"

    rows: list[facts.Fact] = []
    truncated = False
    if table_present and not text_unsearchable:
        # Ask for one row past the cap so "truncated" reports whether more rows
        # exist, rather than guessing from count == limit (which calls a store
        # holding exactly `limit` rows truncated when it is complete).
        probe = facts.query_facts(
            module=module_filter,
            status=status_filter,
            text=text_filter,
            db_path=store_path,
            limit=bounded + 1,
        )
        truncated = len(probe) > bounded
        rows = probe[:bounded]

    payload = _envelope(
        "facts_query",
        _store_descriptor(
            store_path, "facts", present=present, readable=readable, table_present=table_present
        ),
    )
    payload.update(
        {
            "filters": {
                "module": module_filter,
                "status": status_filter,
                "text": text_filter,
                "text_terms": list(text_terms),
            },
            "match_mode": match_mode,
            "limit": bounded,
            "count": len(rows),
            "truncated": truncated,
            "facts": [_fact_row(fact) for fact in rows],
            "store_totals": (
                facts.stats(db_path=store_path) if table_present else {"total": 0, "by_status": {}}
            ),
        }
    )
    return payload


def knowledge_index_status(
    use_manifest: bool = False,
    scan_fallback: bool = True,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Freshness/health of the KFTS document index.

    Cheap and query-free: lets a caller decide whether to trust
    ``knowledge_search`` results before spending a search on them.
    """
    store_path = _resolve_store(db_path)
    present, readable, table_present = _probe_store(store_path, "knowledge_fts")
    index = kfts.index_stats(
        db_path=store_path,
        use_manifest=bool(use_manifest),
        scan_fallback=bool(scan_fallback),
    )

    payload = _envelope(
        "knowledge_index_status",
        _store_descriptor(
            store_path,
            "knowledge_fts",
            present=present,
            readable=readable,
            table_present=table_present,
        ),
    )
    payload.update(
        {
            "requested": {
                "use_manifest": bool(use_manifest),
                "scan_fallback": bool(scan_fallback),
            },
            "index": index,
            "stale": bool(index.get("stale")),
            "stale_reasons": list(index.get("stale_reasons") or []),
            "rebuild_entry_point": kfts.REBUILD_INDEX_API,
        }
    )
    return payload


def memory_proposals_list(
    status: str | None = None,
    kind: str | None = None,
    limit: int = DEFAULT_PROPOSALS_LIMIT,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """List queued memory proposals (read-only view of the human-review queue).

    Refuses to touch a store whose ``memory_proposals`` table does not exist
    yet: the underlying accessors would CREATE it (see module docstring), both
    when the file is missing and when the file exists without that table.
    """
    store_path = _resolve_store(db_path)
    present, readable, table_present = _probe_store(store_path, "memory_proposals")
    bounded = _bounded_limit(limit, default=DEFAULT_PROPOSALS_LIMIT, maximum=MAX_PROPOSALS_LIMIT)
    status_filter = _validated_choice(status, memory_proposals.STATUSES, "status")
    kind_filter = _validated_choice(kind, memory_proposals.KINDS, "kind")

    rows: list[memory_proposals.MemoryProposal] = []
    truncated = False
    if table_present:
        probe = memory_proposals.list_proposals(
            status=status_filter,
            kind=kind_filter,
            limit=bounded + 1,
            db_path=store_path,
        )
        truncated = len(probe) > bounded
        rows = probe[:bounded]

    payload = _envelope(
        "memory_proposals_list",
        _store_descriptor(
            store_path,
            "memory_proposals",
            present=present,
            readable=readable,
            table_present=table_present,
        ),
    )
    payload.update(
        {
            "filters": {"status": status_filter, "kind": kind_filter},
            "limit": bounded,
            "count": len(rows),
            "truncated": truncated,
            "proposals": [_proposal_row(row) for row in rows],
        }
    )
    return payload


def memory_proposals_get(
    id: str,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Fetch one memory proposal by id.

    Same schema-presence guard as :func:`memory_proposals_list`.
    """
    proposal_id = _clean_text(id)
    if proposal_id is None:
        raise ValueError("id is required")
    store_path = _resolve_store(db_path)
    present, readable, table_present = _probe_store(store_path, "memory_proposals")

    proposal: memory_proposals.MemoryProposal | None = None
    if table_present:
        proposal = memory_proposals.get_proposal(proposal_id, db_path=store_path)

    payload = _envelope(
        "memory_proposals_get",
        _store_descriptor(
            store_path,
            "memory_proposals",
            present=present,
            readable=readable,
            table_present=table_present,
        ),
    )
    payload.update(
        {
            "id": proposal_id,
            "exists": proposal is not None,
            "proposal": _proposal_row(proposal) if proposal is not None else None,
        }
    )
    return payload
