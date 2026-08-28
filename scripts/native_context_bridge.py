#!/usr/bin/env python3
"""Answer the native cockpit's context palette from this repository's own stores.

The Swift cockpit ships a context palette -- a search field over knowledge,
facts, work and the memory queue -- that shells out to this script, one JSON
request per line on stdin, one JSON response on stdout. The palette was ported
without it, so opening the palette in this repository produced a "bridge script
not found" error and the whole surface read as broken.

This is a **read-only** bridge. It opens the coordination and knowledge stores
through the same helpers the MCP read tools use, and it never writes. Two
consequences worth stating rather than discovering:

  * A store that does not exist yet is not an error. A fresh checkout has no
    knowledge database, and the honest answer is an empty result that names the
    store it looked in -- not a stack trace, and not a silent zero that reads
    like "nothing matched".
  * Snippets come from titles and steps, never from event bodies. The palette is
    a local surface, but the redaction line drawn for the board holds here too:
    an event's occurrence and kind are safe to show, its prose is not.

The response shape is pinned by the Swift decoder in
`NativeContextPaletteModels.swift`. `ok`, `mode`, `query`, `groups` and
`sourceCounts` are required; everything else has a default, so a partial answer
degrades to fewer groups rather than a decode failure.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from coordharness import config  # noqa: E402
from coordharness.coord import config as coord_config  # noqa: E402
from coordharness.coord import coord_db  # noqa: E402

MAX_LIMIT = 50
MAX_SNIPPET = 240

# Which groups each palette mode is allowed to answer with. "all" is not a
# synonym for every group: files are a separate concern the bridge does not
# index, so claiming them under "all" would promise a search that never runs.
MODE_GROUPS: dict[str, tuple[str, ...]] = {
    "all": ("work", "facts", "knowledge", "memory"),
    "knowledge": ("knowledge",),
    "facts": ("facts",),
    "work": ("work",),
    "memory": ("memory",),
    "files": (),
    "done": ("work",),
}

GROUP_LABELS = {
    "work": ("Work", "board"),
    "facts": ("Facts", "knowledge_db"),
    "knowledge": ("Knowledge", "knowledge_db"),
    "memory": ("Memory proposals", "knowledge_db"),
}

GROUP_ACCENTS = {"work": "blue", "facts": "green", "knowledge": "violet", "memory": "amber"}


def _clip(text: Any, limit: int = MAX_SNIPPET) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _hit(
    *,
    hit_id: str,
    group: str,
    kind: str,
    title: str,
    pointer: str | None = None,
    snippet: str = "",
    metadata: dict[str, Any] | None = None,
    badges: Iterable[str] = (),
    action: str = "inspect",
) -> dict[str, Any]:
    label, source = GROUP_LABELS.get(group, (group.title(), group))
    return {
        "id": hit_id,
        "source": source,
        "sourceLabel": label,
        "group": group,
        "groupLabel": label,
        "kind": kind,
        "title": _clip(title, 160),
        "pointer": pointer,
        "snippet": _clip(snippet),
        "metadata": metadata or {},
        "accent": GROUP_ACCENTS.get(group, "muted"),
        "primaryAction": action,
        "previewLoaded": False,
        "badges": list(badges),
    }


def _search_work(query: str, limit: int, done_only: bool) -> list[dict[str, Any]]:
    """Match board rows on id, title and current step."""
    db = config.coord_db_path()
    if not db.exists():
        return []
    needle = query.lower()
    hits: list[dict[str, Any]] = []
    conn = coord_config.connect_ro(db)
    try:
        for row in coord_db.board_rows(conn):
            row = dict(row)
            work_id = str(row.get("work_id") or "")
            title = str(row.get("title") or row.get("display") or work_id)
            step = str(row.get("claim_step") or "")
            status = str(row.get("status") or "").lower()
            if done_only and status != "done":
                continue
            haystack = f"{work_id} {title} {step}".lower()
            if needle not in haystack:
                continue
            hits.append(
                _hit(
                    hit_id=work_id,
                    group="work",
                    kind=status or "row",
                    title=f"{work_id}  {title}",
                    pointer=str(row.get("done_signal") or "") or None,
                    snippet=step,
                    metadata={
                        "status": status,
                        "owner": str(row.get("assignee") or ""),
                        "module": str(row.get("module") or ""),
                    },
                    badges=[b for b in (status, str(row.get("assignee") or "")) if b],
                    action="revealWork",
                )
            )
            if len(hits) >= limit:
                break
    finally:
        conn.close()
    return hits


def _knowledge_hits(query: str, limit: int, groups: tuple[str, ...]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Facts, index cards and memory proposals, through the shared read surface.

    Returns (hits, errors). A missing knowledge store yields no hits and one
    stated error rather than an exception: the palette should say which store
    was empty, not fail to open.
    """
    hits: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    db = config.knowledge_db_path()
    if not db.exists():
        errors.append(
            {
                "source": "knowledge_db",
                "message": f"No knowledge store at {db.name}; facts, index and memory groups are empty.",
            }
        )
        return hits, errors

    try:
        from coordharness.knowledge import read_surface
    except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
        errors.append({"source": "knowledge_db", "message": f"read surface unavailable: {exc}"})
        return hits, errors

    if "facts" in groups:
        try:
            payload = read_surface.facts_query(text=query, limit=limit, db_path=db)
            for fact in payload.get("facts", []):
                hits.append(
                    _hit(
                        hit_id=f"fact:{fact.get('fact_id') or fact.get('key')}",
                        group="facts",
                        kind=str(fact.get("status") or "fact"),
                        title=str(fact.get("key") or fact.get("statement") or "fact"),
                        snippet=str(fact.get("value") or fact.get("statement") or ""),
                        metadata={k: v for k, v in fact.items() if isinstance(v, (str, int, float, bool))},
                        badges=[b for b in (str(fact.get("status") or ""), str(fact.get("module") or "")) if b],
                    )
                )
        except Exception as exc:  # noqa: BLE001
            errors.append({"source": "facts", "message": str(exc)})

    if "memory" in groups:
        try:
            payload = read_surface.memory_proposals_list(limit=limit, db_path=db)
            for proposal in payload.get("proposals", []):
                hits.append(
                    _hit(
                        hit_id=f"proposal:{proposal.get('proposal_id')}",
                        group="memory",
                        kind=str(proposal.get("status") or "proposed"),
                        title=_clip(proposal.get("statement") or "memory proposal", 160),
                        snippet=str(proposal.get("rationale") or ""),
                        metadata={k: v for k, v in proposal.items() if isinstance(v, (str, int, float, bool))},
                        badges=[b for b in (str(proposal.get("status") or ""), str(proposal.get("scope") or "")) if b],
                    )
                )
        except Exception as exc:  # noqa: BLE001
            errors.append({"source": "memory_proposals", "message": str(exc)})

    return hits, errors


def _index_stats() -> dict[str, Any] | None:
    db = config.knowledge_db_path()
    if not db.exists():
        return None
    try:
        from coordharness.knowledge import read_surface

        return read_surface.knowledge_index_status(db_path=db)
    except Exception:  # noqa: BLE001 - stats are decoration, never a failure path
        return None


def handle_search(request: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    query = str(request.get("query") or "").strip()
    mode = str(request.get("mode") or "all")
    if mode not in MODE_GROUPS:
        mode = "all"
    limit = max(1, min(int(request.get("limit") or 18), MAX_LIMIT))
    groups_wanted = MODE_GROUPS[mode]

    hits: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    if query:
        if "work" in groups_wanted:
            hits.extend(_search_work(query, limit, done_only=(mode == "done")))
        knowledge_hits, knowledge_errors = _knowledge_hits(query, limit, groups_wanted)
        hits.extend(knowledge_hits)
        errors.extend(knowledge_errors)

    if mode == "files":
        errors.append(
            {
                "source": "files",
                "message": "This bridge does not index files; use the knowledge or work modes.",
            }
        )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for hit in hits:
        grouped.setdefault(hit["group"], []).append(hit)

    groups = []
    for key in groups_wanted:
        items = grouped.get(key, [])
        if not items:
            continue
        label, _ = GROUP_LABELS.get(key, (key.title(), key))
        groups.append(
            {
                "id": key,
                "label": label,
                "accent": GROUP_ACCENTS.get(key, "muted"),
                "summary": f"{len(items)} match{'' if len(items) == 1 else 'es'}",
                "count": len(items),
                "items": items,
            }
        )

    return {
        "ok": True,
        "id": request.get("id"),
        "command": "search",
        "mode": mode,
        "query": query,
        "profile": request.get("profile"),
        "hits": hits,
        "groups": groups,
        "sourceCounts": {key: len(items) for key, items in grouped.items()},
        "errors": errors or None,
        "truncated": any(len(items) >= limit for items in grouped.values()),
        "suggestions": [],
        "intentCards": [],
        "facets": [],
        "answerCards": [],
        "explorerSummary": {},
        "index": _index_stats(),
        "elapsedMs": round((time.monotonic() - started) * 1000, 2),
    }


def handle_read(request: dict[str, Any]) -> dict[str, Any]:
    """Read a pointer, confined to the project root.

    Confinement is the whole security surface here: the palette hands back
    whatever pointer a hit carried, and a pointer that escapes the project is
    refused rather than resolved.
    """
    pointer = str(request.get("pointer") or "")
    max_bytes = max(1, min(int(request.get("maxBytes") or 12_000), 200_000))
    root = config.project_root()
    detail: dict[str, Any] = {"title": pointer, "body": "", "kind": "file"}
    read: dict[str, Any] = {"pointer": pointer}

    try:
        target = (root / pointer).resolve()
        target.relative_to(root.resolve())
        if not target.is_file():
            raise FileNotFoundError(pointer)
        data = target.read_text(encoding="utf-8", errors="replace")
        read["bytes"] = len(data.encode("utf-8"))
        read["truncated"] = len(data) > max_bytes
        detail["body"] = data[:max_bytes]
        detail["title"] = pointer
    except ValueError:
        detail["body"] = "Refused: that pointer resolves outside the project root."
    except FileNotFoundError:
        detail["body"] = "That pointer names nothing in this project."
    except OSError as exc:
        detail["body"] = f"Could not read that pointer: {exc}"

    return {
        "ok": True,
        "id": request.get("id"),
        "command": "read",
        "pointer": pointer,
        "read": read,
        "detailCard": detail,
    }


def handle_stats(request: dict[str, Any]) -> dict[str, Any]:
    response = handle_search({**request, "command": "search", "query": "", "mode": "all"})
    response["command"] = "stats"
    return response


HANDLERS = {"search": handle_search, "read": handle_read, "stats": handle_stats}


def main() -> int:
    line = sys.stdin.readline()
    if not line.strip():
        print(json.dumps({"ok": False, "mode": "all", "query": "", "groups": [], "sourceCounts": {},
                          "errors": [{"source": "bridge", "message": "empty request"}]}))
        return 1
    try:
        request = json.loads(line)
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "mode": "all", "query": "", "groups": [], "sourceCounts": {},
                          "errors": [{"source": "bridge", "message": f"invalid request: {exc}"}]}))
        return 1

    handler = HANDLERS.get(str(request.get("command") or "search"))
    if handler is None:
        print(json.dumps({"ok": False, "mode": "all", "query": "", "groups": [], "sourceCounts": {},
                          "errors": [{"source": "bridge", "message": "unknown command"}]}))
        return 1

    print(json.dumps(handler(request), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
