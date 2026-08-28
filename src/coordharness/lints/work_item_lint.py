#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from coordharness import config as _harness_config

_DB = str(_harness_config.coord_db_path())
_OPEN = ("planned", "queued", "running", "blocked")

_REQUIRED = [
    ("title", "no real title (defaults to the work_id)"),
    ("module", "no module (which subsystem does this belong to?)"),
    ("display", "no operator-facing display title"),
    ("assignee", "unassigned (claude|codex|local-gpu|operator?)"),
    ("parent_id", "not mapped to an initiative (parent_id)"),
    ("done_signal", "no done_signal artifact (what proves it's done?)"),
    ("acceptance_json", "empty acceptance rubric (DONE = artifact + rubric pass)"),
]


@dataclass(frozen=True)
class Row:
    work_id: str
    parent_id: str
    surface: str
    module: str
    sublane: str
    title: str
    display: str
    assignee: str
    intent_state: str
    done_signal: str
    acceptance_json: str


@dataclass
class Finding:
    work_id: str
    surface: str
    assignee: str
    missing: list[str] = field(default_factory=list)


def _norm(s: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in (s or "")).split())


def _load(db: str) -> list[Row]:
    uri = f"file:{db}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        con.row_factory = sqlite3.Row
        cols = "work_id,parent_id,surface,module,sublane,title,display,assignee,intent_state,done_signal,acceptance_json"
        q = f"SELECT {cols} FROM work_items WHERE intent_state IN ({','.join('?' * len(_OPEN))}) AND archived_at IS NULL"
        return [
            Row(
                work_id=r["work_id"] or "",
                parent_id=(r["parent_id"] or "").strip(),
                surface=(r["surface"] or "job").strip(),
                module=(r["module"] or "").strip(),
                sublane=(r["sublane"] or "").strip(),
                title=(r["title"] or "").strip(),
                display=(r["display"] or "").strip(),
                assignee=(r["assignee"] or "").strip(),
                intent_state=(r["intent_state"] or "").strip(),
                done_signal=(r["done_signal"] or "").strip(),
                acceptance_json=(r["acceptance_json"] or "[]").strip(),
            )
            for r in con.execute(q, _OPEN)
        ]
    finally:
        con.close()


def _field_ok(row: Row, fieldname: str) -> bool:
    if fieldname == "title":
        return bool(row.title) and row.title != row.work_id
    if fieldname == "acceptance_json":
        try:
            return bool(json.loads(row.acceptance_json or "[]"))
        except Exception:
            return False
    if fieldname in ("parent_id", "done_signal") and row.surface == "epic":
        return True
    return bool(getattr(row, fieldname, "") or "")


def lint(rows: list[Row]) -> list[Finding]:
    out: list[Finding] = []
    for row in rows:
        missing = [reason for f, reason in _REQUIRED if not _field_ok(row, f)]
        if missing:
            out.append(Finding(row.work_id, row.surface, row.assignee or "—", missing))
    return out


def dups(rows: list[Row], threshold: float = 0.86) -> list[tuple[str, str, float]]:
    pairs: list[tuple[str, str, float]] = []
    by_mod: dict[str, list[Row]] = {}
    for r in rows:
        by_mod.setdefault(r.module, []).append(r)
    for mod, group in by_mod.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                ta, tb = _norm(a.title or a.display), _norm(b.title or b.display)
                if not ta or not tb:
                    continue
                ratio = SequenceMatcher(None, ta, tb).ratio()
                if ratio >= threshold:
                    pairs.append((a.work_id, b.work_id, round(ratio, 2)))
    return sorted(pairs, key=lambda p: -p[2])


def main() -> int:
    ap = argparse.ArgumentParser(description="Lint board work items for completeness + duplicates.")
    ap.add_argument("--db", default=_DB)
    ap.add_argument("--strict", action="store_true", help="exit 1 if any open item is malformed")
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()

    rows = _load(args.db)
    findings = lint(rows)
    duplicates = dups(rows)

    print(f"═══ WORK-ITEM LINT ═══  ({len(rows)} open items)")
    print(f"  malformed: {len(findings)}   ·   well-formed: {len(rows) - len(findings)}   ·   possible dups: {len(duplicates)}")

    if findings:
        print("\n── UNDER-SPECIFIED (missing required metadata) ──")
        for f in findings[: args.limit]:
            print(f"  ✗ {f.work_id}  [{f.surface}/{f.assignee}]")
            for m in f.missing:
                print(f"      – {m}")
        if len(findings) > args.limit:
            print(f"  … +{len(findings) - args.limit} more")

    if duplicates:
        print("\n── POSSIBLE DUPLICATE WORK (same module, near-identical title) ──")
        for a, b, ratio in duplicates[: args.limit]:
            print(f"  ~{ratio}  {a}  ≈  {b}")

    if args.strict and findings:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
