from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from dataclasses import replace as dc_replace
from pathlib import Path

from .kfts import DEFAULT_INDEX_DB
from .query_scoring import field_terms as _shared_field_terms
from .query_scoring import query_tokens as _shared_query_tokens
from .query_scoring import term_matches as _shared_term_matches

MAX_FACT_SEARCH_CANDIDATES = 200

STATUSES = ("live", "superseded", "closed", "dark", "parked", "corrected")

OWNER_LANES = ("claude", "codex", "shared", "operator")

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_RETIREMENT_MARKER_RE = re.compile(
    r"(?:^|;\s*)FACT_LIFECYCLE=RETIRED;\s*"
    r"FACT_LIFECYCLE_POINTER=([^;\n]+)"
)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _slugify(statement: str, module: str | None) -> str:
    base = f"{module}-{statement}" if module else statement
    slug = _SLUG_RE.sub("-", base.lower()).strip("-")
    return slug[:80] or "fact"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS facts (
    id               TEXT PRIMARY KEY,           -- stable slug, e.g. 'checkout-p99-latency'
    statement        TEXT NOT NULL,              -- canonical question, e.g. 'checkout p99 latency'
    value            TEXT,                       -- the answer as text ('0.42', 'NO-GO', 'CLOSED')
    unit             TEXT,                       -- 'AUC' | 'Brier' | 'C-index' | 'USD' | 'decision' | ...
    status           TEXT NOT NULL               -- live|superseded|closed|dark|parked|corrected
        CHECK (status IN ('live','superseded','closed','dark','parked','corrected')),
    module           TEXT,                       -- vertical / subsystem, e.g. 'api','ingest','ui'
    evidence_pointer TEXT,                       -- memory://<path> or file:line provenance
    supersedes       TEXT,                       -- id of the row THIS row replaces (FK -> facts.id)
    superseded_by    TEXT,                       -- id of the row that replaced THIS row (back-pointer)
    owner_lane       TEXT                        -- claude|codex|shared|operator
        CHECK (owner_lane IS NULL OR owner_lane IN ('claude','codex','shared','operator')),
    updated_at       TEXT NOT NULL,              -- ISO8601 UTC -- TRANSACTION time: when WE recorded it
    notes            TEXT,                       -- caveat / framing (e.g. 'fabricated hardcoded string')
    valid_from       TEXT,                       -- ISO8601 UTC -- VALID time: when this became true in
                                                  -- the world (bitemporal; additive, NULL on legacy rows
                                                  -- until first write after migration -- see
                                                  -- _ensure_bitemporal_columns)
    valid_to         TEXT,                       -- ISO8601 UTC -- VALID time: when it stopped being true;
                                                  -- NULL = still true / open-ended
    FOREIGN KEY (supersedes) REFERENCES facts(id)
);
CREATE INDEX IF NOT EXISTS idx_facts_module ON facts(module);
CREATE INDEX IF NOT EXISTS idx_facts_status ON facts(status);
CREATE INDEX IF NOT EXISTS idx_facts_statement ON facts(statement);
"""


def _ensure_bitemporal_columns(c: sqlite3.Connection) -> None:
    cols = {row[1] for row in c.execute("PRAGMA table_info(facts)").fetchall()}
    if "valid_from" not in cols:
        c.execute("ALTER TABLE facts ADD COLUMN valid_from TEXT")
        c.execute("UPDATE facts SET valid_from = updated_at WHERE valid_from IS NULL")
    if "valid_to" not in cols:
        c.execute("ALTER TABLE facts ADD COLUMN valid_to TEXT")


_STALE_STATUSES = frozenset({"corrected", "superseded"})


def _stale_marker(status: str, superseded_by: str | None) -> str:
    tag = "CORRECTED" if status == "corrected" else "SUPERSEDED"
    pointer = f"->{superseded_by}" if superseded_by else "->?"
    return f"[{tag}{pointer}] "


def _mark_stale_value(value: str | None, status: str, superseded_by: str | None) -> str | None:
    if status not in _STALE_STATUSES or value is None:
        return value
    marker = _stale_marker(status, superseded_by)
    if value.startswith("[CORRECTED->") or value.startswith("[SUPERSEDED->"):
        return value
    return marker + value


@dataclass(frozen=True)
class Fact:
    id: str
    statement: str
    value: str | None
    unit: str | None
    status: str
    module: str | None
    evidence_pointer: str | None
    supersedes: str | None
    superseded_by: str | None
    owner_lane: str | None
    updated_at: str
    notes: str | None
    valid_from: str | None = None
    valid_to: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row | tuple) -> "Fact":
        d = dict(row) if not isinstance(row, tuple) else None
        if d is None:
            padded = list(row) + [None] * max(0, 14 - len(row))
            (id_, statement, value, unit, status, module, ev, sup, supby,
             lane, updated, notes, valid_from, valid_to) = padded[:14]
            return cls(id_, statement, _mark_stale_value(value, status, supby), unit, status,
                       module, ev, sup, supby, lane, updated, notes, valid_from, valid_to)
        return cls(
            id=d["id"], statement=d["statement"],
            value=_mark_stale_value(d["value"], d["status"], d["superseded_by"]),
            unit=d["unit"],
            status=d["status"], module=d["module"], evidence_pointer=d["evidence_pointer"],
            supersedes=d["supersedes"], superseded_by=d["superseded_by"],
            owner_lane=d["owner_lane"], updated_at=d["updated_at"], notes=d["notes"],
            valid_from=d.get("valid_from"), valid_to=d.get("valid_to"),
        )


@dataclass(frozen=True)
class FactSearchHit:
    fact: Fact
    score: float
    matched_terms: tuple[str, ...]
    rank_reasons: tuple[str, ...]


def fact_lifecycle(fact: Fact) -> dict[str, str | bool] | None:
    match = _RETIREMENT_MARKER_RE.search(fact.notes or "")
    if not match:
        return None
    return {
        "state": "retired",
        "retired": True,
        "quotable": False,
        "retirement_marking": "RETIRED",
        "pointer": match.group(1).strip(),
    }


def _conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    p = _checked_db_path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p))
    _configure_connection(c)
    c.executescript(SCHEMA_SQL)
    _ensure_bitemporal_columns(c)
    c.commit()
    return c


def _read_conn(db_path: Path | str | None = None) -> sqlite3.Connection | None:
    p = _checked_db_path(db_path)
    if not p.exists():
        return None
    c = sqlite3.connect(f"file:{p.resolve().as_posix()}?mode=ro", uri=True)
    _configure_connection(c, write_pragmas=False)
    return c


def _checked_db_path(db_path: Path | str | None = None) -> Path:
    p = Path(db_path) if db_path else DEFAULT_INDEX_DB
    rp = str(p.resolve())
    from coordharness.coord.config import _WAREHOUSE_MARKERS

    for marker in _WAREHOUSE_MARKERS:
        if f"/{marker}/" in rp:
            raise RuntimeError(f"knowledge index {rp!r} must stay outside the warehouse")
    return p


def _configure_connection(c: sqlite3.Connection, *, write_pragmas: bool = True) -> None:
    c.row_factory = sqlite3.Row
    if write_pragmas:
        c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    c.execute("PRAGMA foreign_keys=ON")


def init_db(db_path: Path | str | None = None) -> dict:
    c = _conn(db_path)
    try:
        c.commit()
        return {"db": str(Path(db_path or DEFAULT_INDEX_DB).resolve())}
    finally:
        c.close()


def upsert_fact(
    statement: str,
    value: str | None = None,
    *,
    unit: str | None = None,
    status: str = "live",
    module: str | None = None,
    evidence_pointer: str | None = None,
    supersedes: str | None = None,
    owner_lane: str | None = None,
    notes: str | None = None,
    id: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    db_path: Path | str | None = None,
) -> Fact:
    if status not in STATUSES:
        raise ValueError(f"bad status {status!r}; allowed: {STATUSES}")
    if owner_lane is not None and owner_lane not in OWNER_LANES:
        raise ValueError(f"bad owner_lane {owner_lane!r}; allowed: {OWNER_LANES}")
    if module:
        module = module.strip().lower()
    fid = id or _slugify(statement, module)
    now = _now()
    c = _conn(db_path)
    try:
        if supersedes is not None and not c.execute(
            "SELECT 1 FROM facts WHERE id=?", (supersedes,)
        ).fetchone():
            raise ValueError(f"supersedes={supersedes!r} references no existing fact")
        existing = c.execute(
            "SELECT valid_from, valid_to FROM facts WHERE id=?", (fid,)
        ).fetchone()
        if existing is None:
            final_valid_from = valid_from if valid_from is not None else now
            final_valid_to = valid_to
        else:
            final_valid_from = valid_from if valid_from is not None else existing["valid_from"]
            final_valid_to = valid_to if valid_to is not None else existing["valid_to"]
        c.execute(
            """
            INSERT INTO facts (id, statement, value, unit, status, module,
                               evidence_pointer, supersedes, superseded_by,
                               owner_lane, updated_at, notes, valid_from, valid_to)
            VALUES (?,?,?,?,?,?,?,?,NULL,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                statement=excluded.statement, value=excluded.value, unit=excluded.unit,
                status=excluded.status, module=excluded.module,
                evidence_pointer=excluded.evidence_pointer, supersedes=excluded.supersedes,
                owner_lane=excluded.owner_lane, updated_at=excluded.updated_at,
                notes=excluded.notes, valid_from=excluded.valid_from, valid_to=excluded.valid_to
            """,
            (fid, statement, value, unit, status, module, evidence_pointer,
             supersedes, owner_lane, now, notes, final_valid_from, final_valid_to),
        )
        c.commit()
        return get_fact(fid, db_path=db_path)
    finally:
        c.close()


def supersede(
    old_id: str,
    new_id: str,
    *,
    old_status: str = "corrected",
    db_path: Path | str | None = None,
) -> tuple[Fact, Fact]:
    if old_status not in STATUSES:
        raise ValueError(f"bad old_status {old_status!r}")
    c = _conn(db_path)
    try:
        for fid in (old_id, new_id):
            if not c.execute("SELECT 1 FROM facts WHERE id=?", (fid,)).fetchone():
                raise ValueError(f"no such fact: {fid!r}")
        now = _now()
        c.execute(
            "UPDATE facts SET status=?, superseded_by=?, updated_at=?, "
            "valid_to=COALESCE(valid_to, ?) WHERE id=?",
            (old_status, new_id, now, now, old_id),
        )
        c.execute(
            "UPDATE facts SET supersedes=?, updated_at=? WHERE id=?",
            (old_id, now, new_id),
        )
        c.commit()
    finally:
        c.close()
    return (
        get_fact(old_id, db_path=db_path),
        get_fact(new_id, db_path=db_path),
    )


def get_fact(id: str, db_path: Path | str | None = None) -> Fact | None:
    c = _read_conn(db_path)
    if c is None:
        return None
    try:
        row = c.execute("SELECT * FROM facts WHERE id=?", (id,)).fetchone()
        return Fact.from_row(row) if row else None
    finally:
        c.close()


@dataclass(frozen=True)
class ConflictResolution:

    winner: Fact | None
    loser: Fact | None
    rule: str
    resolved: bool
    candidates: tuple[Fact, ...] = ()


class UnresolvedFactConflict(RuntimeError):

    def __init__(self, statement: str, candidates: tuple[Fact, ...]):
        self.statement = statement
        self.candidates = candidates
        ids = ", ".join(f.id for f in candidates)
        super().__init__(
            f"UNRESOLVED fact conflict for statement {statement!r}: "
            f"{len(candidates)} candidates tie under every deterministic rule "
            f"({ids}). Refusing to silently pick one — resolve explicitly "
            "(supersede() one, or set a distinguishing evidence_pointer)."
        )


def _supersedes_transitively(
    x: Fact, target_id: str, *, db_path: Path | str | None = None, max_hops: int = 64
) -> bool:
    seen: set[str] = set()
    cur = x
    hops = 0
    while cur is not None and cur.supersedes and hops < max_hops:
        nxt_id = cur.supersedes
        if nxt_id == target_id:
            return True
        if nxt_id in seen:
            break
        seen.add(nxt_id)
        cur = get_fact(nxt_id, db_path=db_path)
        hops += 1
    return False


def resolve_fact_conflict(
    a: Fact,
    b: Fact,
    *,
    db_path: Path | str | None = None,
    max_hops: int = 64,
) -> ConflictResolution:
    if a.id == b.id:
        return ConflictResolution(a, None, "same_row", True, (a,))

    if _supersedes_transitively(a, b.id, db_path=db_path, max_hops=max_hops):
        return ConflictResolution(a, b, "explicit_supersedes_chain", True, (a, b))
    if _supersedes_transitively(b, a.id, db_path=db_path, max_hops=max_hops):
        return ConflictResolution(b, a, "explicit_supersedes_chain", True, (a, b))

    if a.updated_at != b.updated_at:
        winner, loser = (a, b) if a.updated_at > b.updated_at else (b, a)
        return ConflictResolution(winner, loser, "later_transaction_time", True, (a, b))

    a_ev = bool((a.evidence_pointer or "").strip())
    b_ev = bool((b.evidence_pointer or "").strip())
    if a_ev != b_ev:
        winner, loser = (a, b) if a_ev else (b, a)
        return ConflictResolution(winner, loser, "evidence_pointer_present", True, (a, b))

    return ConflictResolution(None, None, "unresolved_tie", False, (a, b))


def current_value(
    statement: str,
    *,
    module: str | None = None,
    db_path: Path | str | None = None,
) -> Fact | None:
    c = _read_conn(db_path)
    if c is None:
        return None
    try:
        params: list[str] = [statement]
        where = "statement=? COLLATE NOCASE"
        if module is not None:
            where += " AND module=? COLLATE NOCASE"
            params.append(module)
        rows = c.execute(
            f"SELECT * FROM facts WHERE {where} AND status='live' "
            "ORDER BY updated_at DESC", params
        ).fetchall()
        if not rows:
            rows = c.execute(
                f"SELECT * FROM facts WHERE {where} "
                "AND status NOT IN ('superseded','corrected') "
                "ORDER BY updated_at DESC", params
            ).fetchall()
        if not rows:
            return None
        candidates = [Fact.from_row(r) for r in rows]
    finally:
        c.close()

    winner = candidates[0]
    for other in candidates[1:]:
        resolution = resolve_fact_conflict(winner, other, db_path=db_path)
        if not resolution.resolved:
            raise UnresolvedFactConflict(statement, tuple(candidates))
        winner = resolution.winner
    return winner


_SEARCH_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "after",
        "already",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "did",
        "do",
        "does",
        "for",
        "from",
        "have",
        "help",
        "is",
        "it",
        "of",
        "on",
        "or",
        "our",
        "should",
        "the",
        "to",
        "use",
        "was",
        "we",
        "what",
        "whether",
        "which",
        "with",
    }
)


def already_decided(
    query: str,
    *,
    db_path: Path | str | None = None,
) -> list[Fact]:
    return [
        hit.fact
        for hit in search_facts(
            query,
            statuses=("closed", "parked", "corrected"),
            db_path=db_path,
            include_history=True,
            limit=200,
        )
    ]


def search_facts(
    query: str,
    *,
    module: str | None = None,
    status: str | None = None,
    statuses: tuple[str, ...] | list[str] | None = None,
    db_path: Path | str | None = None,
    limit: int = 200,
    include_history: bool = False,
) -> list[FactSearchHit]:
    if status is not None and statuses is not None:
        raise ValueError("pass either status or statuses, not both")
    selected_statuses = tuple(statuses or ((status,) if status is not None else ()))
    for value in selected_statuses:
        if value not in STATUSES:
            raise ValueError(f"bad status {value!r}")
    query_terms = _search_terms(query)
    clauses: list[str] = []
    params: list[str] = []
    if module is not None:
        clauses.append("module=? COLLATE NOCASE")
        params.append(module)
    if selected_statuses:
        placeholders = ",".join("?" for _ in selected_statuses)
        clauses.append(f"status IN ({placeholders})")
        params.extend(selected_statuses)
    elif not include_history:
        clauses.append("status NOT IN ('superseded','corrected')")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    c = _read_conn(db_path)
    if c is None:
        return []
    try:
        try:
            rows = _candidate_rows(c, where, params, query_terms, limit=limit)
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return []
            raise
    finally:
        c.close()

    hits: list[FactSearchHit] = []
    for row in rows:
        fact = Fact.from_row(row)
        scoring_fact = fact
        if fact.status in _STALE_STATUSES and isinstance(row, sqlite3.Row):
            scoring_fact = dc_replace(fact, value=row["value"])
        score, matched_terms, reasons = _score_fact_search_hit(scoring_fact, query_terms)
        if query_terms and not matched_terms:
            continue
        if not include_history and fact.status in {"superseded", "corrected"}:
            continue
        hits.append(
            FactSearchHit(
                fact=fact,
                score=round(score, 3),
                matched_terms=tuple(matched_terms),
                rank_reasons=tuple(reasons),
            )
        )
    hits.sort(
        key=lambda hit: (
            -hit.score,
            _status_rank(hit.fact.status),
            hit.fact.module or "",
            hit.fact.statement,
            hit.fact.id,
        )
    )
    return hits[: max(0, int(limit))]


def query_facts(
    *,
    module: str | None = None,
    status: str | None = None,
    text: str | None = None,
    db_path: Path | str | None = None,
    limit: int = 200,
) -> list[Fact]:
    if text is not None:
        return [
            hit.fact
            for hit in search_facts(
                text,
                module=module,
                status=status,
                db_path=db_path,
                limit=limit,
            )
        ]
    clauses: list[str] = []
    params: list[str] = []
    if module is not None:
        clauses.append("module=? COLLATE NOCASE")
        params.append(module)
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"bad status {status!r}")
        clauses.append("status=?")
        params.append(status)
    if text is not None:
        like = f"%{text.strip()}%"
        clauses.append(
            "(statement LIKE ? COLLATE NOCASE OR notes LIKE ? COLLATE NOCASE"
            " OR value LIKE ? COLLATE NOCASE)"
        )
        params.extend([like, like, like])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    c = _read_conn(db_path)
    if c is None:
        return []
    try:
        try:
            rows = c.execute(
                f"SELECT * FROM facts{where} ORDER BY module, statement, updated_at DESC "
                "LIMIT ?", (*params, int(limit)),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return []
            raise
        return [Fact.from_row(r) for r in rows]
    finally:
        c.close()


def _search_terms(value: str | None) -> tuple[str, ...]:
    return tuple(_shared_query_tokens(value, stopwords=_SEARCH_STOPWORDS, max_terms=24))


def _candidate_rows(
    conn: sqlite3.Connection,
    base_where: str,
    base_params: list[str],
    query_terms: tuple[str, ...],
    *,
    limit: int,
) -> list[sqlite3.Row]:
    candidate_limit = min(MAX_FACT_SEARCH_CANDIDATES, max(int(limit) * 12, 50))
    if not query_terms:
        return conn.execute(
            f"SELECT * FROM facts{base_where} LIMIT ?",
            (*base_params, candidate_limit),
        ).fetchall()
    patterns = _candidate_patterns(query_terms)
    if not patterns:
        return conn.execute(
            f"SELECT * FROM facts{base_where} LIMIT ?",
            (*base_params, candidate_limit),
        ).fetchall()
    searchable = (
        "lower(coalesce(id,'') || ' ' || coalesce(statement,'') || ' ' || "
        "coalesce(module,'') || ' ' || coalesce(value,'') || ' ' || "
        "coalesce(unit,'') || ' ' || coalesce(notes,'') || ' ' || "
        "coalesce(evidence_pointer,'') || ' ' || coalesce(status,''))"
    )
    candidate_clause = " OR ".join(f"{searchable} LIKE ? ESCAPE '\\'" for _ in patterns)
    where = f"{base_where} AND ({candidate_clause})" if base_where else f" WHERE ({candidate_clause})"
    rows = conn.execute(
        f"SELECT * FROM facts{where} LIMIT ?",
        (*base_params, *patterns, candidate_limit),
    ).fetchall()
    if rows:
        return rows
    return conn.execute(
        f"SELECT * FROM facts{base_where} LIMIT ?",
        (*base_params, candidate_limit),
    ).fetchall()


def _candidate_patterns(query_terms: tuple[str, ...]) -> tuple[str, ...]:
    patterns: list[str] = []
    seen: set[str] = set()
    for raw in query_terms:
        term = str(raw or "").strip().lower()
        if not term:
            continue
        candidates = [term]
        if len(term) >= 7:
            candidates.append(term[:-3])
        for candidate in candidates:
            if len(candidate) < 3 and not candidate.isdigit():
                continue
            pattern = f"%{_escape_like(candidate)}%"
            if pattern not in seen:
                seen.add(pattern)
                patterns.append(pattern)
    return tuple(patterns[:32])


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _field_terms(value: str | None) -> set[str]:
    return _shared_field_terms(value)


def _term_matches(term: str, field_terms: set[str]) -> bool:
    return _shared_term_matches(term, field_terms)


def _score_fact_search_hit(fact: Fact, query_terms: tuple[str, ...]) -> tuple[float, list[str], list[str]]:
    fields: tuple[tuple[str, str | None, float], ...] = (
        ("statement", fact.statement, 4.0),
        ("id", fact.id, 3.0),
        ("module", fact.module, 2.5),
        ("value", fact.value, 2.0),
        ("unit", fact.unit, 1.5),
        ("notes", fact.notes, 1.25),
        ("evidence_pointer", fact.evidence_pointer, 1.0),
        ("status", fact.status, 0.75),
    )
    field_terms = [(name, _field_terms(value), weight) for name, value, weight in fields]
    score = float(_status_boost(fact.status))
    matched: list[str] = []
    reasons: list[str] = [f"query_terms:{len(query_terms)}", f"status:{fact.status}"]
    for term in query_terms:
        term_score = 0.0
        term_fields: list[str] = []
        for name, terms, weight in field_terms:
            if _term_matches(term, terms):
                term_score += weight
                term_fields.append(name)
        if term_score:
            matched.append(term)
            score += term_score
            reasons.append(f"{term}:{'+'.join(term_fields[:3])}")
    if matched:
        score += len(matched) * 0.5
        reasons.insert(0, f"term_matches:{len(matched)}")
    return score, matched, reasons


def _status_boost(status: str) -> float:
    return {
        "live": 8.0,
        "closed": 7.0,
        "dark": 5.0,
        "parked": 4.0,
        "superseded": 1.0,
        "corrected": 0.5,
    }.get(status, 0.0)


def _status_rank(status: str) -> int:
    return {
        "live": 0,
        "closed": 1,
        "dark": 2,
        "parked": 3,
        "superseded": 4,
        "corrected": 5,
    }.get(status, 9)


def resolve_fact_evidence(
    fact_or_pointer: Fact | str | None,
    *,
    max_bytes: int = 2000,
    context_db: Path | str | None = None,
) -> dict:
    pointer = (
        fact_or_pointer.evidence_pointer
        if isinstance(fact_or_pointer, Fact)
        else str(fact_or_pointer or "").strip()
    )
    if not pointer:
        return {"resolved": False, "reason": "missing_pointer", "pointer": None}
    if not pointer.startswith(("memory://", "kfts://")):
        return {"resolved": False, "reason": "unsupported_pointer", "pointer": pointer}
    try:
        from .context_federator import read_context_pointer

        kwargs = {
            "max_bytes": max(1, int(max_bytes)),
            "neighbor_radius": 0,
        }
        if context_db is not None:
            kwargs["context_db"] = context_db
        note = read_context_pointer(pointer, **kwargs)
    except Exception as exc:
        return {
            "resolved": False,
            "reason": exc.__class__.__name__,
            "pointer": pointer,
        }
    body = str(note.get("content") or "")
    if not note.get("exists"):
        alias = (note.get("metadata") or {}).get("alias_resolution") if isinstance(note.get("metadata"), dict) else None
        return {
            "resolved": False,
            "reason": (alias or {}).get("status") if isinstance(alias, dict) and alias.get("status") != "not_alias" else "not_found",
            "pointer": note.get("pointer") or pointer,
            "alias_resolution": alias,
        }
    metadata = note.get("metadata") if isinstance(note.get("metadata"), dict) else {}
    compact_body = " ".join(body.split())
    return {
        "resolved": True,
        "pointer": note.get("pointer") or pointer,
        "doc_pointer": metadata.get("doc_pointer"),
        "heading": metadata.get("heading"),
        "heading_path": metadata.get("heading_path"),
        "heading_slug": metadata.get("heading_slug"),
        "line_start": metadata.get("line_start"),
        "line_end": metadata.get("line_end"),
        "line_count": metadata.get("line_count"),
        "truncated": bool(note.get("truncated")),
        "snippet": compact_body[: max(1, int(max_bytes))],
    }


def supersession_chain(id: str, db_path: Path | str | None = None) -> list[Fact]:
    chain: list[Fact] = []
    seen: set[str] = set()
    cur = get_fact(id, db_path=db_path)
    while cur and cur.id not in seen:
        chain.append(cur)
        seen.add(cur.id)
        cur = get_fact(cur.supersedes, db_path=db_path) if cur.supersedes else None
    return list(reversed(chain))


def stats(db_path: Path | str | None = None) -> dict:
    c = _read_conn(db_path)
    if c is None:
        return {"total": 0, "by_status": {}}
    try:
        total = c.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        by_status = {
            r["status"]: r["n"]
            for r in c.execute(
                "SELECT status, COUNT(*) AS n FROM facts GROUP BY status"
            ).fetchall()
        }
        return {"total": total, "by_status": by_status}
    finally:
        c.close()
