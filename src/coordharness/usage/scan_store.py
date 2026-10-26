"""Persistent, incremental scan of this machine's CLI transcripts.

A live bounded scan can only ever see the last few days: the transcripts on a
working machine run to tens of gigabytes, and re-reading them on every
dashboard refresh is not an option. This store parses each JSONL file once,
keyed by ``(size, mtime_ns)``, and keeps that file's contribution as a small
per-file aggregate over fixed UTC time slots. A refresh then reads aggregates
instead of JSON, and derives calendar days from the slots in whatever zone the
reader asks for (see ``local_history.SLOT_SECONDS`` for why slots, not days).

Three properties are load-bearing:

* **Per-file aggregates.** A changed source rebuilds its aggregate from durable
  receipt maxima, preserving already observed spend and cross-file raises.
* **Owned replay identities with a maximum representative.** A resumed session
  replays earlier turns verbatim, so the same billable message appears in many
  files. Each identity is owned by the first file that claimed it, and its
  stored contribution is the COMPONENT-WISE MAXIMUM over every occurrence:
  Claude Code writes one line per content block of a response and only the
  last carries the complete usage snapshot, so keeping the first occurrence
  threw the rest away. ``UsageScanStore._claim`` carries the measurement.
* **One file per transaction.** A killed scan loses at most the file it was
  parsing, and re-running resumes from there.

Historical retention: deleted, renamed and truncated sources do not withdraw
identified billable receipts. Their largest observed components survive owner
rescans. Explicit rebuild is destructive and not a reconciliation operation.

"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone, tzinfo
import hashlib
import logging
from pathlib import Path
import sqlite3
import time
from typing import Any, Final

from .ledger import DailyUsage
from .local_history import (
    FileParseCounters,
    LocalHistoryImport,
    ParsedRecord,
    SlotUsage,
    fingerprint_identity,
    history_scan_roots,
    iter_history_records,
    rollup_days,
    root_identity_digest,
)

_logger = logging.getLogger("coordharness.usage.scan")

# 5: aggregates keyed by UTC time slot instead of a scan-time local day.
SCHEMA_VERSION: Final = 5
PROVIDERS: Final = ("claude", "codex")

# Transcripts stream one record at a time. A total-file size cap silently loses
# usage from long-lived chats (real files already exceed 10 GiB). Callers may
# still supply an explicit byte budget for bounded diagnostic scans.
_MAX_FILE_BYTES: Final = None

_TOKEN_COLUMNS: Final = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_create_5m_tokens",
    "cache_create_1h_tokens",
    "cache_create_other_tokens",
)

# Files are keyed by a small integer rather than by their path. A busy machine
# owns millions of replay identities, and a path averages ~180 bytes here: one
# `seen_message` row per identity carrying its own copy of the path made the
# store several times larger than the aggregates it exists to serve.
_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS scanned_file(
    file_id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    root_digest TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    scanned_at TEXT NOT NULL,
    records_scanned INTEGER NOT NULL,
    records_accepted INTEGER NOT NULL,
    records_rejected INTEGER NOT NULL,
    records_deduplicated INTEGER NOT NULL,
    records_unidentified INTEGER NOT NULL,
    parse_error_count INTEGER NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS scanned_file_provider
    ON scanned_file(provider, root_digest);

-- `usage_slot` is the UTC epoch second a 15-minute slot starts at. It replaced
-- schema 4's `file_daily.usage_date`, a calendar day fixed in whatever zone the
-- scanning process ran in; the table was renamed with it so a reader built for
-- days fails loudly on a slot store instead of misreading epoch seconds.
CREATE TABLE IF NOT EXISTS file_slot(
    file_id INTEGER NOT NULL,
    usage_slot INTEGER NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_create_5m_tokens INTEGER NOT NULL,
    cache_create_1h_tokens INTEGER NOT NULL,
    cache_create_other_tokens INTEGER NOT NULL,
    request_count INTEGER NOT NULL,
    PRIMARY KEY(file_id, usage_slot, model)
) STRICT;

CREATE INDEX IF NOT EXISTS file_slot_rollup
    ON file_slot(usage_slot, model);

-- Compatibility for a process still running schema-4 code against this file
-- (an installed runtime not yet upgraded). A READER built for days gets UTC
-- days from it, which is a correct answer on a basis it did not choose. A
-- WRITER built for days cannot open the store at all: its `CREATE TABLE IF NOT
-- EXISTS file_daily` is a no-op against this view, and its next statement,
-- `CREATE INDEX ... ON file_daily`, fails ("views may not be indexed"). That is
-- deliberate -- it would otherwise write day rows beside the slots, reset
-- `user_version` to 4, and make the next schema-5 open discard the whole store
-- again, on every refresh, for as long as both ran.
CREATE VIEW IF NOT EXISTS file_daily AS
    SELECT file_id, date(usage_slot, 'unixepoch') AS usage_date, model,
        SUM(input_tokens) AS input_tokens,
        SUM(output_tokens) AS output_tokens,
        SUM(cache_read_tokens) AS cache_read_tokens,
        SUM(cache_create_5m_tokens) AS cache_create_5m_tokens,
        SUM(cache_create_1h_tokens) AS cache_create_1h_tokens,
        SUM(cache_create_other_tokens) AS cache_create_other_tokens,
        SUM(request_count) AS request_count
    FROM file_slot GROUP BY file_id, date(usage_slot, 'unixepoch'), model;

-- One row per replay identity, holding the REPRESENTATIVE observation of that
-- identity: the component-wise maximum over every occurrence seen anywhere.
-- The slot, model and counts are stored rather than derived because a later
-- occurrence in a different file has to know which aggregate row of the owning
-- file its increase belongs to. `representative` marks a row written by this
-- schema; a row carried over from an earlier one holds no counts to compare
-- against, so raising it from an implicit zero would add a whole record on top
-- of a contribution already counted.
CREATE TABLE IF NOT EXISTS seen_message(
    fingerprint INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    file_id INTEGER NOT NULL,
    representative INTEGER NOT NULL DEFAULT 0,
    usage_slot INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_create_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_create_1h_tokens INTEGER NOT NULL DEFAULT 0,
    cache_create_other_tokens INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE INDEX IF NOT EXISTS seen_message_owner
    ON seen_message(file_id);

CREATE TABLE IF NOT EXISTS usage_identity_alias(
    alias_fingerprint INTEGER PRIMARY KEY,
    canonical_fingerprint INTEGER NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS usage_identity_alias_canonical
    ON usage_identity_alias(canonical_fingerprint);
CREATE TABLE IF NOT EXISTS usage_receipt_metadata(
    fingerprint INTEGER PRIMARY KEY,
    service_tier TEXT,
    inference_speed TEXT,
    region TEXT
) STRICT;

-- Unattributed receipts are retained numerically, never included in totals.
CREATE TABLE IF NOT EXISTS unplaced_claude_receipt(
    fingerprint INTEGER PRIMARY KEY,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL, cache_create_5m_tokens INTEGER NOT NULL,
    cache_create_1h_tokens INTEGER NOT NULL, cache_create_other_tokens INTEGER NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS file_unidentified_slot(
    file_id INTEGER NOT NULL, usage_slot INTEGER NOT NULL, model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL, cache_create_5m_tokens INTEGER NOT NULL,
    cache_create_1h_tokens INTEGER NOT NULL, cache_create_other_tokens INTEGER NOT NULL,
    request_count INTEGER NOT NULL, PRIMARY KEY(file_id,usage_slot,model)
) STRICT;

-- Days that predate this machine's transcripts, imported frozen from a
-- third-party ledger. Deliberately a SEPARATE table rather than `file_slot`
-- rows with a flag: these carry another tool's accounting, they are never
-- reparsed, and dropping them must be one statement that cannot touch a
-- self-computed row. `source` keys the importer so several can coexist and
-- each can be withdrawn on its own. These stay DAY rows: the source only ever
-- knew days, and the Codex ledger's days are UTC days -- they equal OpenAI's
-- own UTC daily buckets exactly for 2026-03-20..2026-07-16.
CREATE TABLE IF NOT EXISTS legacy_daily(
    provider TEXT NOT NULL,
    source TEXT NOT NULL,
    usage_date TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    api_rate_estimate_nanos INTEGER,
    imported_at TEXT NOT NULL,
    cache_create_other_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(provider, source, usage_date, model)
) STRICT;

CREATE INDEX IF NOT EXISTS legacy_daily_day
    ON legacy_daily(provider, usage_date);

-- Explicit audited partial-overlap policy; retained receipt rows stay intact.
CREATE TABLE IF NOT EXISTS legacy_overlap_day(
    provider TEXT NOT NULL,
    usage_date TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY(provider,usage_date)
) STRICT;
"""

# The Codex ledger reports no cache writes, so schema 2 had nowhere to put
# one and needed nowhere. The Claude ledger does report them, and dropping
# that column would make an imported day understate its own token count.
_LEGACY_TOKEN_COLUMNS: Final = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_create_other_tokens",
)


class ScanStoreError(RuntimeError):
    """The scan store could not be opened or is not usable."""


# Columns added after a table first shipped, as (table, column, definition).
# `CREATE TABLE IF NOT EXISTS` is a no-op on a store written by an earlier
# schema, so a new column has to be added explicitly or the store keeps the
# old shape and every write naming the column fails.
#
# The schema-4 `seen_message` additions are gone from this list on purpose:
# schema 5 drops and recreates that table (see `_migrate_day_basis`), so a
# pre-slot shape never survives to be extended.
_ADDED_COLUMNS: Final = (
    ("legacy_daily", "cache_create_other_tokens", "INTEGER NOT NULL DEFAULT 0"),
)

# Tables whose rows are derived from transcripts and carry a day fixed at scan
# time. Their content cannot be converted to slots -- the timestamps are gone --
# so the only correct migration is to discard it and parse again.
_DAY_BASIS_TABLES: Final = ("file_daily", "seen_message")


def _migrate_day_basis(conn: sqlite3.Connection) -> bool:
    """Discard a pre-slot store's derived rows so the next scan rebuilds them.

    Returns whether anything was discarded. The frozen ``legacy_daily`` rows
    are kept: they are not derived from transcripts and cannot be re-derived.

    WHY discard rather than keep serving the old rows until a rebuild: the
    incremental scan never re-reads an unchanged file, so a day-bucketed row
    left in place would be served in its scan-time zone forever. Forgetting
    every scanned file (``scanned_file``) makes the next ordinary scan a full
    one, and until it completes the store reads as having no history -- which
    readers already answer with a bounded live scan, and the refresher reports
    as ``building`` -- rather than as a wrong one.
    """

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return False
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-read under the write lock: another process may have migrated.
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        kinds = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        stale = version < SCHEMA_VERSION and bool(set(kinds) & {"file_daily", "scanned_file"})
        if stale:
            for name in _DAY_BASIS_TABLES:
                # `file_daily` is a TABLE in a schema-4 store and the
                # compatibility VIEW in a schema-5 one; either way the schema
                # script recreates what belongs.
                if kinds.get(name) == "view":
                    conn.execute(f"DROP VIEW {name}")
                elif kinds.get(name) == "table":
                    conn.execute(f"DROP TABLE {name}")
            if kinds.get("scanned_file") == "table":
                conn.execute("DELETE FROM scanned_file")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    if stale:
        _logger.warning(
            "usage scan store: schema %d day-bucketed rows discarded for the slot "
            "basis; the next scan re-parses every transcript",
            version,
        )
    return stale


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing store up to ``SCHEMA_VERSION``, idempotently.

    Only additive: every step here is a column with a default, so a store one
    version behind gains the column without rewriting a row, and a store
    already current does nothing. Nothing is dropped or retyped, so an older
    reader keeps working against a migrated store.
    """

    for table, column, definition in _ADDED_COLUMNS:
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not present or column in present:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError:
            # Another process migrated it between the read and the write.
            pass


@dataclass(frozen=True)
class ScanResult:
    """What one ``scan`` call did, in full."""

    provider: str
    files_seen: int = 0
    files_parsed: int = 0
    files_unchanged: int = 0
    files_failed: int = 0
    files_oversized: int = 0
    bytes_parsed: int = 0
    records_scanned: int = 0
    records_accepted: int = 0
    records_rejected: int = 0
    records_deduplicated: int = 0
    records_unidentified: int = 0
    parse_error_count: int = 0
    elapsed_seconds: float = 0.0
    truncated: bool = False
    # Occurrences that RAISED an existing stored
    # representative rather than adding nothing. Reported per run rather than
    # stored, because it describes what one scan learned, not what the store
    # holds. `records_raised_across_files` is the subset of those that raised
    # an identity owned by a DIFFERENT file, which is the only case the
    # incremental contract cannot reproduce exactly; see `_absorb`.
    records_raised: int = 0
    records_raised_across_files: int = 0
    # Codex usage records skipped as re-emissions (the thread's running total
    # did not advance); see `local_history.iter_history_records`.
    records_reemitted: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "files_seen": self.files_seen,
            "files_parsed": self.files_parsed,
            "files_unchanged": self.files_unchanged,
            "files_failed": self.files_failed,
            "files_oversized": self.files_oversized,
            "bytes_parsed": self.bytes_parsed,
            "records_scanned": self.records_scanned,
            "records_accepted": self.records_accepted,
            "records_rejected": self.records_rejected,
            "records_deduplicated": self.records_deduplicated,
            "records_unidentified": self.records_unidentified,
            "parse_error_count": self.parse_error_count,
            "records_raised": self.records_raised,
            "records_raised_across_files": self.records_raised_across_files,
            "records_reemitted": self.records_reemitted,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class _Raise:
    """How much a repeat occurrence pushes a stored representative higher.

    Carries the OWNING file's aggregate coordinates, not the raising file's:
    the increase belongs beside the observation it corrects, so one reparse of
    the owner still replaces that identity's whole contribution.
    """

    owner_file_id: int
    usage_slot: int
    model: str
    deltas: tuple[int, ...]


# The answer for an identity this schema cannot compare against: already
# claimed, and contributing nothing.
_NO_RAISE: Final = _Raise(
    owner_file_id=-1, usage_slot=0, model="", deltas=(0,) * len(_TOKEN_COLUMNS)
)


@dataclass(frozen=True)
class LegacyImportResult:
    """What one ``import_legacy`` call did."""

    provider: str
    source: str
    rows_offered: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_stored: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "source": self.source,
            "rows_offered": self.rows_offered,
            "rows_inserted": self.rows_inserted,
            "rows_updated": self.rows_updated,
            "rows_stored": self.rows_stored,
        }


def default_store_path(home: Path | str | None = None) -> Path:
    """Where the scan store lives when the caller names no path."""

    return Path(home) if home is not None else Path.home() / ".coordharness" / "usage-scan.sqlite"


def _normalized_provider(provider: str) -> str:
    value = provider.lower()
    if value not in PROVIDERS:
        raise ValueError("unsupported provider")
    return value


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _transcripts(scan_roots: Sequence[Path]) -> Iterator[Path]:
    """Every nonsymlink JSONL beneath a provider's scan roots, in a stable order.

    Stable so an interrupted run resumes over the same sequence, and so a run
    capped with ``max_files`` walks the backlog rather than the same prefix.
    Roots are walked in the order given, each sorted within itself.

    A path reachable from two roots is yielded twice and scanned once: the
    second visit matches the stored ``(size, mtime_ns)`` and counts as
    unchanged, so no aggregate is doubled.
    """

    for scan_root in scan_roots:
        if not scan_root.is_dir() or scan_root.is_symlink():
            continue
        for path in sorted((*scan_root.rglob("*.jsonl"), *scan_root.rglob("*.jsonl.zst"))):
            if path.is_file() and not path.is_symlink():
                yield path


class UsageScanStore:
    """A SQLite-backed incremental parse of local CLI transcripts."""

    def __init__(self, path: Path | str | None = None, *, read_only: bool = False) -> None:
        self.path = default_store_path(path)
        self._read_only = read_only
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> "UsageScanStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            self._connection = self._connect()
        return self._connection

    def _connect(self) -> sqlite3.Connection:
        try:
            if self._read_only:
                if not self.path.is_file():
                    raise ScanStoreError(f"no usage scan store at {self.path.name}")
                conn = sqlite3.connect(
                    f"file:{self.path}?mode=ro", uri=True, isolation_level=None
                )
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(self.path, isolation_level=None)
        except (OSError, sqlite3.Error) as error:
            raise ScanStoreError(f"usage scan store unusable: {error}") from error
        conn.row_factory = sqlite3.Row
        try:
            if not self._read_only:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                # Before the schema script: `CREATE TABLE IF NOT EXISTS` would
                # otherwise leave a schema-4 `seen_message` in its day shape.
                _migrate_day_basis(conn)
                conn.executescript(_SCHEMA)
                _migrate(conn)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error as error:
            conn.close()
            raise ScanStoreError(f"usage scan store unusable: {error}") from error
        return conn

    # -- scanning ---------------------------------------------------------

    def scan(
        self,
        root: Path | str,
        *,
        provider: str,
        max_files: int | None = None,
        max_file_bytes: int | None = _MAX_FILE_BYTES,
        log_every: int = 200,
    ) -> ScanResult:
        """Parse every new or changed transcript beneath one CLI home.

        ``max_files`` counts only files this run actually parses, so a capped
        run walks the backlog forward instead of re-deciding to skip the same
        prefix.
        """

        if self._read_only:
            raise ScanStoreError("this scan store was opened read-only")
        provider = _normalized_provider(provider)
        root = Path(root)
        digest = root_identity_digest(root)
        conn = self.connection
        result = ScanResult(provider=provider)
        started = time.monotonic()
        for path in _transcripts(history_scan_roots(root, provider)):
            result = replace(result, files_seen=result.files_seen + 1)
            try:
                stat = path.stat()
            except OSError:
                result = replace(result, files_failed=result.files_failed + 1)
                continue
            key = path.as_posix()
            known = conn.execute(
                "SELECT size, mtime_ns FROM scanned_file WHERE path=?", (key,)
            ).fetchone()
            if known is not None and known["size"] == stat.st_size and (
                known["mtime_ns"] == stat.st_mtime_ns
            ):
                result = replace(result, files_unchanged=result.files_unchanged + 1)
                continue
            if max_file_bytes is not None and stat.st_size > max_file_bytes:
                result = replace(result, files_oversized=result.files_oversized + 1)
                continue
            if max_files is not None and result.files_parsed >= max_files:
                return replace(
                    result, truncated=True, elapsed_seconds=time.monotonic() - started
                )
            result = self._absorb(
                conn,
                path,
                key=key,
                provider=provider,
                root_digest=digest,
                stat_size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                result=result,
            )
            if log_every > 0 and result.files_parsed % log_every == 0:
                _logger.info(
                    "usage scan %s: %d parsed, %d unchanged, %d records accepted",
                    provider,
                    result.files_parsed,
                    result.files_unchanged,
                    result.records_accepted,
                )
        return replace(result, elapsed_seconds=time.monotonic() - started)

    def _absorb(
        self,
        conn: sqlite3.Connection,
        path: Path,
        *,
        key: str,
        provider: str,
        root_digest: str,
        stat_size: int,
        mtime_ns: int,
        result: ScanResult,
    ) -> ScanResult:
        """Retain observed billable maxima independently of source lifetime.

        Identified receipts and cross-file raises survive source truncation and
        owner rescans. Anonymous usage keeps a disclosed per-source/slot maximum.
        Explicit destructive rebuild is separate from historical reconciliation.
        """

        counters = FileParseCounters()
        accepted = duplicates = unidentified = 0
        raised = raised_across_files = 0
        conn.execute("BEGIN IMMEDIATE")
        try:
            file_id = conn.execute(
                "INSERT INTO scanned_file(path, provider, root_digest, size, mtime_ns, "
                "scanned_at, records_scanned, records_accepted, records_rejected, "
                "records_deduplicated, records_unidentified, parse_error_count) "
                "VALUES(?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 0) "
                "ON CONFLICT(path) DO UPDATE SET provider=excluded.provider, "
                "root_digest=excluded.root_digest, size=excluded.size, "
                "mtime_ns=excluded.mtime_ns, scanned_at=excluded.scanned_at "
                "RETURNING file_id",
                (
                    key,
                    provider,
                    root_digest,
                    stat_size,
                    mtime_ns,
                    _utc_iso(datetime.now(timezone.utc)),
                ),
            ).fetchone()["file_id"]
            # A legacy identity lacking a representative cannot be rebuilt
            # from a partial source without guessing. Preserve the old history.
            if conn.execute("SELECT 1 FROM seen_message WHERE file_id=? AND representative=0 LIMIT 1", (file_id,)).fetchone():
                conn.execute("ROLLBACK")
                return replace(result, files_failed=result.files_failed + 1)
            prior_anonymous = conn.execute("SELECT records_unidentified FROM scanned_file WHERE file_id=?", (file_id,)).fetchone()[0]
            has_anonymous = conn.execute("SELECT 1 FROM file_unidentified_slot WHERE file_id=? LIMIT 1", (file_id,)).fetchone()
            if prior_anonymous and not has_anonymous:
                # Seed anonymous residuals once, before any representatives
                # change. With complete identified representatives subtraction
                # is exact; inconsistency must fail closed rather than inflate.
                known = {(row["usage_slot"], row["model"]): row for row in conn.execute(
                    "SELECT usage_slot,model," + ",".join(f"SUM({col}) AS {col}" for col in _TOKEN_COLUMNS)
                    + ",COUNT(*) AS request_count FROM seen_message WHERE file_id=? GROUP BY usage_slot,model", (file_id,))}
                residuals = []
                for row in conn.execute("SELECT * FROM file_slot WHERE file_id=?", (file_id,)):
                    held = known.get((row["usage_slot"],row["model"]))
                    values = [row[col] - (held[col] if held is not None else 0) for col in (*_TOKEN_COLUMNS,"request_count")]
                    if any(value < 0 for value in values):
                        raise ScanStoreError("anonymous baseline cannot be separated losslessly")
                    if any(values):
                        residuals.append((file_id, row["usage_slot"], row["model"], *values))
                conn.executemany("INSERT INTO file_unidentified_slot VALUES(?,?,?,?,?,?,?,?,?,?)", residuals)
            legacy_days = {row[0] for row in conn.execute(
                "SELECT DISTINCT usage_date FROM legacy_daily WHERE provider=?", (provider,)
            )}
            totals: dict[tuple[int, str], list[int]] = {}
            # Increases to identities owned by OTHER files, applied once at the
            # end so a raise costs one statement per (owner, slot, model) rather
            # than one per record.
            foreign: dict[tuple[int, int, str], list[int]] = {}
            seen_owned: set[int] = set()
            prior_owned_slots = {(row[0],row[1]) for row in conn.execute(
                "SELECT DISTINCT usage_slot,model FROM seen_message WHERE file_id=?", (file_id,)
            )}
            anonymous_slots = {(row[0],row[1]) for row in conn.execute(
                "SELECT usage_slot,model FROM file_unidentified_slot WHERE file_id=?", (file_id,)
            )}
            for record in iter_history_records(path, provider=provider, counters=counters):
                if record.attribution_unknown or record.usage_slot is None:
                    if provider != "claude" or record.identity is None:
                        counters.records_rejected += 1
                        continue
                    fingerprint = fingerprint_identity(record.identity)
                    held = conn.execute(
                        "SELECT provider,representative,usage_slot,model FROM seen_message WHERE fingerprint=?",
                        (fingerprint,),
                    ).fetchone()
                    if held is None or held["provider"] != "claude" or not held["representative"] or held["model"] == "unknown":
                        # Keep numerical evidence unplaced. A later dated native
                        # claim consumes these maxima, without re-reading sources.
                        self._record_pricing_metadata(conn,fingerprint,record)
                        values = tuple(int(record.metrics.get(column,0)) for column in _TOKEN_COLUMNS)
                        conn.execute(
                            "INSERT INTO unplaced_claude_receipt VALUES(?,?,?,?,?,?,?) "
                            "ON CONFLICT(fingerprint) DO UPDATE SET " + ",".join(
                                f"{column}=MAX({column},excluded.{column})" for column in _TOKEN_COLUMNS),
                            (fingerprint,*values),
                        )
                        counters.records_rejected += 1
                        continue
                    record = replace(record,usage_slot=held["usage_slot"],model=held["model"],attribution_unknown=False)
                identity = record.identity
                if identity is None:
                    if datetime.fromtimestamp(record.usage_slot,timezone.utc).date().isoformat() in legacy_days:
                        counters.records_rejected += 1
                        continue  # Anonymous usage cannot prove new legacy-day volume.
                    if (record.usage_slot,record.model) in prior_owned_slots and (record.usage_slot,record.model) not in anonymous_slots:
                        raise ScanStoreError("new anonymous receipts may overlap retained identified usage")
                    unidentified += 1
                else:
                    fingerprint = self._resolve_receipt_fingerprint(
                        conn, provider, identity, getattr(record, "alias_identities", ())
                    )
                    if (datetime.fromtimestamp(record.usage_slot,timezone.utc).date().isoformat() in legacy_days
                        and conn.execute("SELECT 1 FROM seen_message WHERE fingerprint=?",(fingerprint,)).fetchone() is None):
                        counters.records_rejected += 1
                        continue  # Partial archives must not replace a whole frozen day.
                    if anonymous_slots and (record.usage_slot,record.model) in anonymous_slots:
                        if conn.execute("SELECT 1 FROM seen_message WHERE fingerprint=?", (fingerprint,)).fetchone() is None:
                            raise ScanStoreError("anonymous and newly identified receipts may overlap")
                    increase = self._claim(conn, provider, identity, record, file_id)
                    if increase is not None:
                        if increase.owner_file_id == file_id and fingerprint not in seen_owned:
                            accepted += 1
                            seen_owned.add(fingerprint)
                        else:
                            duplicates += 1
                        if any(increase.deltas):
                            raised += 1
                            # The OWNER's coordinates, never this record's: the
                            # increase has to land in the same aggregate row as
                            # the observation it corrects.
                            owned = (increase.usage_slot, increase.model)
                            if increase.owner_file_id == file_id:
                                continue  # Durable maximum is reconstructed below.
                            raised_across_files += 1
                            bucket = foreign.setdefault((increase.owner_file_id, *owned), [0] * len(_TOKEN_COLUMNS))
                            for index, delta in enumerate(increase.deltas):
                                bucket[index] += delta
                        continue
                    accepted += 1
                    seen_owned.add(fingerprint)
                    continue  # New identified receipt enters the canonical sum.
                bucket = totals.setdefault(
                    (record.usage_slot, record.model), [0] * (len(_TOKEN_COLUMNS) + 1)
                )
                for index, column in enumerate(_TOKEN_COLUMNS):
                    bucket[index] += int(record.metrics.get(column, 0))
                bucket[-1] += 1
                accepted += 1
            conn.executemany(
                f"INSERT INTO file_unidentified_slot(file_id,usage_slot,model,{','.join(_TOKEN_COLUMNS)},request_count) "
                f"VALUES(?,?,?,{','.join('?' * (len(_TOKEN_COLUMNS)+1))}) "
                "ON CONFLICT(file_id,usage_slot,model) DO UPDATE SET "
                + ','.join(f"{col}=MAX({col},excluded.{col})" for col in (*_TOKEN_COLUMNS,"request_count")),
                [(file_id,slot,model,*amounts) for (slot,model),amounts in totals.items()],
            )
            totals = {(row["usage_slot"],row["model"]): [row[col] for col in (*_TOKEN_COLUMNS,"request_count")]
                      for row in conn.execute("SELECT * FROM file_unidentified_slot WHERE file_id=?", (file_id,))}
            for row in conn.execute(
                "SELECT usage_slot,model," + ','.join(f"SUM({col}) AS {col}" for col in _TOKEN_COLUMNS)
                + ",COUNT(*) AS request_count FROM seen_message WHERE file_id=? GROUP BY usage_slot,model", (file_id,)
            ):
                bucket = totals.setdefault((row["usage_slot"],row["model"]),[0]*(len(_TOKEN_COLUMNS)+1))
                for index, col in enumerate((*_TOKEN_COLUMNS, "request_count")):
                    bucket[index] += row[col]
            conn.execute("DELETE FROM file_slot WHERE file_id=?", (file_id,))
            conn.executemany(
                f"INSERT INTO file_slot(file_id, usage_slot, model, "
                f"{', '.join(_TOKEN_COLUMNS)}, request_count) "
                f"VALUES(?, ?, ?, {', '.join('?' * (len(_TOKEN_COLUMNS) + 1))})",
                [
                    (file_id, slot, model, *amounts)
                    for (slot, model), amounts in totals.items()
                ],
            )
            # An increase belongs to the aggregate of the file that OWNS the
            # identity, not to this one, so the maximum stays attributable to
            # one row and a later reparse of the owner replaces it whole. The
            # upsert adds no request: the owner already counted the request
            # when it claimed the identity.
            conn.executemany(
                f"INSERT INTO file_slot(file_id, usage_slot, model, "
                f"{', '.join(_TOKEN_COLUMNS)}, request_count) "
                f"VALUES(?, ?, ?, {', '.join('?' * len(_TOKEN_COLUMNS))}, 0) "
                "ON CONFLICT(file_id, usage_slot, model) DO UPDATE SET "
                + ", ".join(f"{column}={column}+excluded.{column}" for column in _TOKEN_COLUMNS),
                [
                    (owner, slot, model, *amounts)
                    for (owner, slot, model), amounts in foreign.items()
                ],
            )
            conn.execute(
                "UPDATE scanned_file SET records_scanned=?, records_accepted=?, "
                "records_rejected=?, records_deduplicated=?, records_unidentified=?, "
                "parse_error_count=? WHERE file_id=?",
                (
                    counters.records_scanned,
                    accepted,
                    counters.records_rejected,
                    duplicates,
                    unidentified,
                    counters.parse_error_count,
                    file_id,
                ),
            )
            conn.execute("COMMIT")
        except OSError:
            # A transcript that became unreadable partway through leaves no
            # trace, so the next run treats it as never scanned rather than as
            # scanned up to wherever the read died.
            conn.execute("ROLLBACK")
            return replace(result, files_failed=result.files_failed + 1)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return replace(
            result,
            files_parsed=result.files_parsed + 1,
            bytes_parsed=result.bytes_parsed + stat_size,
            records_scanned=result.records_scanned + counters.records_scanned,
            records_accepted=result.records_accepted + accepted,
            records_rejected=result.records_rejected + counters.records_rejected,
            records_deduplicated=result.records_deduplicated + duplicates,
            records_unidentified=result.records_unidentified + unidentified,
            parse_error_count=result.parse_error_count + counters.parse_error_count,
            records_raised=result.records_raised + raised,
            records_raised_across_files=(
                result.records_raised_across_files + raised_across_files
            ),
            records_reemitted=result.records_reemitted + counters.records_reemitted,
        )

    @staticmethod
    def _resolve_receipt_fingerprint(
        conn: sqlite3.Connection, provider: str, identity: str,
        aliases: Sequence[str] = (),
    ) -> int:
        return UsageScanStore._resolve_fingerprints(
            conn, provider, fingerprint_identity(identity),
            tuple(fingerprint_identity(value) for value in aliases),
        )

    @staticmethod
    def _resolve_fingerprints(
        conn: sqlite3.Connection, provider: str, primary: int,
        aliases: Sequence[int] = (),
    ) -> int:
        if provider != "codex" and not aliases:
            return primary
        candidates = {primary, *aliases}
        placeholders = ",".join("?" for _ in candidates)
        mapped = {row[0] for row in conn.execute(
            f"SELECT canonical_fingerprint FROM usage_identity_alias WHERE alias_fingerprint IN ({placeholders})",
            tuple(candidates),
        )}
        candidates.update(mapped)
        placeholders = ",".join("?" for _ in candidates)
        held = conn.execute(
            f"SELECT fingerprint,provider FROM seen_message WHERE fingerprint IN ({placeholders})",
            tuple(candidates),
        ).fetchall()
        if any(row["provider"] != provider for row in held) or len(held) > 1:
            raise ScanStoreError("receipt alias joins multiple retained identities; explicit union proof required")
        chosen = held[0]["fingerprint"] if held else (next(iter(mapped)) if len(mapped)==1 else primary)
        if not held and len(mapped) > 1:
            raise ScanStoreError("receipt alias has conflicting canonical targets")
        conn.executemany(
            "INSERT INTO usage_identity_alias(alias_fingerprint,canonical_fingerprint) VALUES(?,?) "
            "ON CONFLICT(alias_fingerprint) DO UPDATE SET canonical_fingerprint=excluded.canonical_fingerprint",
            [(value,chosen) for value in candidates if value != chosen],
        )
        return chosen

    @staticmethod
    def _record_pricing_metadata(conn: sqlite3.Connection, fingerprint: int, record: ParsedRecord) -> None:
        metadata = getattr(record,"pricing_metadata",None)
        if not metadata:
            return
        allowed = {
            "service_tier": {"default","standard","priority","batch","flex","fast","ultrafast","scale"},
            "inference_speed": {"standard","fast"},
            "region": {"global","us_only","regional","eu_only","us","eu","fedramp"},
        }
        values = [metadata.get(key) if metadata.get(key) in choices else None for key,choices in allowed.items()]
        if not any(value is not None for value in values):
            return
        held = conn.execute(
            "SELECT service_tier,inference_speed,region FROM usage_receipt_metadata WHERE fingerprint=?",
            (fingerprint,),
        ).fetchone()
        if held is not None:
            for index, name in enumerate(allowed):
                if held[index] is not None and values[index] is not None and held[index] != values[index]:
                    raise ScanStoreError(f"conflicting recognized receipt metadata: {name}")
        conn.execute(
            "INSERT INTO usage_receipt_metadata(fingerprint,service_tier,inference_speed,region) VALUES(?,?,?,?) "
            "ON CONFLICT(fingerprint) DO UPDATE SET "
            "service_tier=COALESCE(excluded.service_tier,service_tier),"
            "inference_speed=COALESCE(excluded.inference_speed,inference_speed),"
            "region=COALESCE(excluded.region,region)", (fingerprint,*values)
        )

    @staticmethod
    def _claim(
        conn: sqlite3.Connection,
        provider: str,
        identity: str,
        record: ParsedRecord,
        file_id: int,
    ) -> "_Raise | None":
        """Claim a replay identity, or say how much this occurrence raises it.

        ``None`` means this file now owns the identity and the caller should
        count the record's own metrics. Otherwise the identity was already
        claimed and the returned ``_Raise`` names the owning file's aggregate
        row and the per-component increase to apply there; all-zero deltas mean
        this occurrence adds nothing.

        WHY the representative is the component-wise MAXIMUM rather than the
        first occurrence: Claude Code writes one transcript line per CONTENT
        BLOCK of a single assistant response. Every line shares `message.id`
        and `requestId`, the early ones carry a PARTIAL streaming usage
        snapshot, and only the last carries the complete one -- one measured
        request runs 2, 2, 2, 2, 286 output tokens across five lines. Keeping
        the first claimant kept the partial snapshot and discarded the rest.

        Measured over this machine's 373,924 multi-occurrence identities:
        216,309 had a first occurrence strictly BELOW the component-wise
        maximum and not one had a first occurrence above it, losing
        244,378,596 output, 8,286,934 cache-read and 4,365 input tokens.

        WHY the maximum and not simply the last occurrence: the maximum does
        not depend on traversal order, which matters because the same identity
        appears in several files and the order those are walked in is not a
        property of the data. It is also provably not smaller here --
        `last == max` on 373,893 of the 373,924.

        One fingerprint space covers both providers: the identity strings are
        namespaced before they are hashed, so the column stays the primary key
        and costs no second index.
        """

        fingerprint = UsageScanStore._resolve_receipt_fingerprint(
            conn, provider, identity, getattr(record, "alias_identities", ())
        )
        UsageScanStore._record_pricing_metadata(conn, fingerprint, record)
        if record.attribution_unknown or record.usage_slot is None:
            raise ScanStoreError("unplaced receipt cannot claim a canonical identity")
        values = tuple(int(record.metrics.get(column, 0)) for column in _TOKEN_COLUMNS)
        if provider == "claude":
            pending = conn.execute("SELECT * FROM unplaced_claude_receipt WHERE fingerprint=?",(fingerprint,)).fetchone()
            if pending is not None:
                canonical = conn.execute("SELECT provider,representative,model FROM seen_message WHERE fingerprint=?",(fingerprint,)).fetchone()
                placed = ((canonical is None and record.model != "unknown") or
                          (canonical is not None and canonical["provider"] == "claude" and
                           canonical["representative"] and canonical["model"] != "unknown"))
                if placed:
                    values = tuple(max(value,pending[column]) for value,column in zip(values,_TOKEN_COLUMNS))
                    conn.execute("DELETE FROM unplaced_claude_receipt WHERE fingerprint=?",(fingerprint,))
        cursor = conn.execute(
            "INSERT INTO seen_message(fingerprint, provider, file_id, representative, "
            f"usage_slot, model, {', '.join(_TOKEN_COLUMNS)}) "
            f"VALUES(?, ?, ?, 1, ?, ?, {', '.join('?' * len(_TOKEN_COLUMNS))}) "
            "ON CONFLICT(fingerprint) DO NOTHING",
            (fingerprint, provider, file_id, record.usage_slot, record.model, *values),
        )
        if cursor.rowcount == 1:
            return None
        held = conn.execute(
            "SELECT file_id, representative, usage_slot, model, "
            f"{', '.join(_TOKEN_COLUMNS)} FROM seen_message WHERE fingerprint=?",
            (fingerprint,),
        ).fetchone()
        if held is None or not held["representative"]:
            # A row written by schema 3 carries no representative to compare
            # against, and its zeros are absence rather than an observation of
            # zero. Raising from them would add this whole record on top of a
            # contribution the owner already counted, so such an identity keeps
            # the schema-3 first-claimant rule until a `rebuild` replaces it.
            return _NO_RAISE
        deltas = tuple(
            max(0, value - held[column]) for value, column in zip(values, _TOKEN_COLUMNS)
        )
        if any(deltas):
            conn.execute(
                "UPDATE seen_message SET "
                + ", ".join(f"{column}={column}+?" for column in _TOKEN_COLUMNS)
                + " WHERE fingerprint=?",
                (*deltas, fingerprint),
            )
        return _Raise(
            owner_file_id=held["file_id"],
            usage_slot=held["usage_slot"],
            model=held["model"],
            deltas=deltas,
        )

    def rebuild(
        self,
        root: Path | str,
        *,
        provider: str,
        max_file_bytes: int | None = _MAX_FILE_BYTES,
        log_every: int = 200,
    ) -> ScanResult:
        """Drop everything stored for one provider and scan it again.

        The exact repair for the deleted-file limitation described in this
        module's docstring, and for any store written by an older schema.
        """

        if self._read_only:
            raise ScanStoreError("this scan store was opened read-only")
        provider = _normalized_provider(provider)
        conn = self.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            owned = "SELECT file_id FROM scanned_file WHERE provider=?"
            conn.execute(f"DELETE FROM file_slot WHERE file_id IN ({owned})", (provider,))
            conn.execute("DELETE FROM usage_receipt_metadata WHERE fingerprint IN (SELECT fingerprint FROM seen_message WHERE provider=?)", (provider,))
            conn.execute("DELETE FROM usage_identity_alias WHERE canonical_fingerprint IN (SELECT fingerprint FROM seen_message WHERE provider=?)", (provider,))
            conn.execute(f"DELETE FROM seen_message WHERE file_id IN ({owned})", (provider,))
            conn.execute("DELETE FROM file_unidentified_slot WHERE file_id IN (SELECT file_id FROM scanned_file WHERE provider=?)", (provider,))
            conn.execute("DELETE FROM scanned_file WHERE provider=?", (provider,))
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        return self.scan(
            root, provider=provider, max_file_bytes=max_file_bytes, log_every=log_every
        )

    # -- reading ----------------------------------------------------------

    def unplaced_claude_totals(self) -> dict[str, Any]:
        """Unattributed numeric evidence, explicitly outside charged history.

        Read-only stores predating this additive table remain readable. Absence
        of that table means coverage was not tracked, never proven complete.
        """
        tracked = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='unplaced_claude_receipt'"
        ).fetchone() is not None
        row = self.connection.execute(
            "SELECT COUNT(*)," + ",".join(f"COALESCE(SUM({column}),0)" for column in _TOKEN_COLUMNS)
            + " FROM unplaced_claude_receipt"
        ).fetchone() if tracked else (0, *([0] * len(_TOKEN_COLUMNS)))
        return {"attribution_state": "unplaced", "charged": False, "coverage_tracked": tracked,
                "unplaced_identities": row[0], "total_tokens": sum(row[1:]),
                "attribution_complete": tracked and row[0] == 0,
                **dict(zip(_TOKEN_COLUMNS,row[1:]))}

    def mark_legacy_overlap_days(self, provider: str, usage_dates: Sequence[str], *, reason: str) -> int:
        """Explicit audited policy: retain raw identities but serve frozen days.

        Mark only dates independently established as partial transcript overlap.
        This is additive and transactional; no source/identity/count is removed.
        """
        if self._read_only:
            raise ScanStoreError("this scan store was opened read-only")
        provider = _normalized_provider(provider)
        if not isinstance(reason,str) or not reason.strip() or len(reason)>1000:
            raise ValueError("legacy overlap requires a bounded audit reason")
        days=sorted(set(usage_dates))
        for day in days:
            if not isinstance(day,str) or len(day)!=10 or datetime.fromisoformat(day).date().isoformat()!=day:
                raise ValueError("legacy overlap dates must be ISO calendar dates")
        conn=self.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            available={row[0] for row in conn.execute("SELECT DISTINCT usage_date FROM legacy_daily WHERE provider=?",(provider,))}
            if any(day not in available for day in days):
                raise ScanStoreError("legacy overlap day has no frozen source to serve")
            conn.executemany(
                "INSERT INTO legacy_overlap_day(provider,usage_date,reason) VALUES(?,?,?) "
                "ON CONFLICT(provider,usage_date) DO UPDATE SET reason=excluded.reason",
                [(provider,day,reason) for day in days],
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return len(days)

    def _visible_slot_guard(self, slot_alias: str) -> str:
        # Read-only connections to older schema-5 files must remain usable.
        if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_overlap_day'").fetchone() is None:
            return ""
        return (
            " AND NOT EXISTS (SELECT 1 FROM legacy_overlap_day AS overlap "
            "WHERE overlap.provider=f.provider AND overlap.usage_date=date("+slot_alias+".usage_slot,'unixepoch') "
            "AND EXISTS (SELECT 1 FROM legacy_daily AS frozen WHERE frozen.provider=overlap.provider "
            "AND frozen.usage_date=overlap.usage_date))"
        )

    def slot_totals(
        self, provider: str, *, root: Path | str | None = None
    ) -> tuple[SlotUsage, ...]:
        """Sum every stored file aggregate into per-(UTC slot, model) rows."""

        provider = _normalized_provider(provider)
        sums = ", ".join(f"SUM(d.{column}) AS {column}" for column in _TOKEN_COLUMNS)
        where, params = self._file_filter(provider, root)
        where += self._visible_slot_guard("d")
        rows = self.connection.execute(
            f"SELECT d.usage_slot AS usage_slot, d.model AS model, {sums}, "
            "SUM(d.request_count) AS request_count "
            "FROM file_slot AS d JOIN scanned_file AS f ON f.file_id = d.file_id "
            f"WHERE {where} GROUP BY d.usage_slot, d.model ORDER BY d.usage_slot, d.model",
            params,
        ).fetchall()
        return tuple(
            SlotUsage(
                usage_slot=row["usage_slot"],
                model=row["model"],
                request_count=row["request_count"],
                **{column: row[column] for column in _TOKEN_COLUMNS},
            )
            for row in rows
        )

    def totals(
        self,
        provider: str,
        *,
        root: Path | str | None = None,
        include_legacy: bool = False,
        tz: tzinfo | None = None,
    ) -> tuple[DailyUsage, ...]:
        """Sum every stored file aggregate into per-(day, model) rows.

        Days are derived from the stored UTC slots in ``tz`` at read time --
        the system zone when ``None``, ``timezone.utc`` for provider-aligned
        days -- so the answer follows the reader's zone without a rescan.

        ``include_legacy`` folds in frozen pre-transcript rows for days no
        self-computed row covers. It defaults to off so the existing contract
        -- self-computed rows only -- is exactly what every current caller
        keeps getting; a caller that wants the longer history has to ask, and
        should prefer ``history_import``, which keeps the two sets apart
        instead of merging them into one indistinguishable sequence.
        """

        computed = rollup_days(self.slot_totals(provider, root=root), tz)
        if not include_legacy:
            return computed
        merged = computed + self.legacy_totals(provider)
        return tuple(sorted(merged, key=lambda row: (row.usage_date, row.model)))

    # -- frozen legacy days -----------------------------------------------

    def import_legacy(
        self,
        provider: str,
        *,
        source: str,
        rows: Sequence[DailyUsage],
    ) -> "LegacyImportResult":
        """Store pre-transcript days from another tool's ledger, idempotently.

        Re-running with the same ledger rewrites the same primary keys to the
        same values, so the import is safe to repeat and safe to interrupt. It
        never reads or writes ``file_slot``: a self-computed day cannot be
        overwritten here because this table cannot express one.
        """

        if self._read_only:
            raise ScanStoreError("this scan store was opened read-only")
        provider = _normalized_provider(provider)
        if not source or len(source) > 200:
            raise ValueError("legacy source key must be a short nonempty string")
        stamp = _utc_iso(datetime.now(timezone.utc))
        conn = self.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            before = conn.execute(
                "SELECT COUNT(*) AS n FROM legacy_daily WHERE provider=? AND source=?",
                (provider, source),
            ).fetchone()["n"]
            columns = (
                "provider",
                "source",
                "usage_date",
                "model",
                *_LEGACY_TOKEN_COLUMNS,
                "api_rate_estimate_nanos",
                "imported_at",
            )
            # Every non-key column is restated on conflict, so re-running with
            # the same ledger rewrites the same values and re-running with a
            # corrected one replaces them -- rather than leaving a stale token
            # column beside a fresh cost.
            updates = ", ".join(
                f"{column}=excluded.{column}"
                for column in columns
                if column not in ("provider", "source", "usage_date", "model")
            )
            conn.executemany(
                f"INSERT INTO legacy_daily({', '.join(columns)}) "
                f"VALUES({', '.join('?' * len(columns))}) "
                f"ON CONFLICT(provider, source, usage_date, model) DO UPDATE SET {updates}",
                [
                    (
                        provider,
                        source,
                        row.usage_date,
                        row.model,
                        *(getattr(row, column) for column in _LEGACY_TOKEN_COLUMNS),
                        row.api_rate_estimate_nanos,
                        stamp,
                    )
                    for row in rows
                ],
            )
            after = conn.execute(
                "SELECT COUNT(*) AS n FROM legacy_daily WHERE provider=? AND source=?",
                (provider, source),
            ).fetchone()["n"]
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return LegacyImportResult(
            provider=provider,
            source=source,
            rows_offered=len(rows),
            rows_inserted=after - before,
            rows_updated=len(rows) - (after - before),
            rows_stored=after,
        )

    def drop_legacy(self, provider: str, *, source: str | None = None) -> int:
        """Withdraw frozen legacy rows. Self-computed rows are untouchable here."""

        if self._read_only:
            raise ScanStoreError("this scan store was opened read-only")
        provider = _normalized_provider(provider)
        where = "provider=?"
        params: list[object] = [provider]
        if source is not None:
            where += " AND source=?"
            params.append(source)
        cursor = self.connection.execute(f"DELETE FROM legacy_daily WHERE {where}", params)
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    def legacy_totals(
        self, provider: str, *, source: str | None = None
    ) -> tuple[DailyUsage, ...]:
        """Frozen legacy rows for days no self-computed row covers.

        The precedence is applied on every read rather than once at import, so
        a day that only becomes self-computed later stops being served from
        the legacy table the moment it does -- without rewriting anything.

        "Covers" is decided on UTC days, never on the reader's local days: a
        zone-dependent rule would make a legacy day appear or vanish when the
        machine changed timezone. The Codex ledger's days are UTC days (they
        equal the provider's UTC buckets exactly). The Claude ledger's are the
        producer's local days, which a UTC day only overlaps; any measured row
        on the UTC day still supersedes one, because measured usage genuinely
        exists inside it. Measured on 2026-09-22, each of the three Claude
        legacy days holds 54M-65M measured tokens on its UTC date (from the
        desktop-app transcript tree the ledger's gap check did not read), so
        serving the legacy row beside them would count that usage twice.
        """

        provider = _normalized_provider(provider)
        where = "l.provider=?"
        params: list[object] = [provider]
        if source is not None:
            where += " AND l.source=?"
            params.append(source)
        sums = ", ".join(f"SUM(l.{column}) AS {column}" for column in _LEGACY_TOKEN_COLUMNS)
        visible = self._visible_slot_guard("d")
        rows = self.connection.execute(
            f"SELECT l.usage_date AS usage_date, l.model AS model, {sums}, "
            "SUM(l.api_rate_estimate_nanos) AS api_rate_estimate_nanos "
            "FROM legacy_daily AS l "
            f"WHERE {where} AND l.usage_date NOT IN ("
            "  SELECT DISTINCT date(d.usage_slot, 'unixepoch') FROM file_slot AS d "
            "  JOIN scanned_file AS f ON f.file_id = d.file_id WHERE f.provider=?"
            + visible + ") GROUP BY l.usage_date, l.model ORDER BY l.usage_date, l.model",
            [*params, provider],
        ).fetchall()
        return tuple(
            DailyUsage(
                usage_date=row["usage_date"],
                model=row["model"],
                api_rate_estimate_nanos=row["api_rate_estimate_nanos"],
                **{column: row[column] for column in _LEGACY_TOKEN_COLUMNS},
            )
            for row in rows
        )

    def legacy_provenance(self, provider: str) -> dict[str, Any]:
        """How a reader should label the legacy rows it was just handed.

        Deliberately blunt about what these numbers are not: a payload that
        presented them beside self-computed days without this would be
        asserting a precision the source never had.
        """

        from .legacy_ledger import LAYOUTS, LEGACY_SEMANTICS, LEGACY_WARNING

        counts = self.legacy_counts(provider)
        served = self.legacy_totals(provider)
        # Each provider's ledger overstates for its own reason, and a reader
        # shown the Codex explanation beside Claude rows would be told
        # something untrue about them. The generic pair stays the fallback so
        # an unrecognized source is still labelled rather than left bare.
        layout = LAYOUTS.get(provider)
        return {
            "semantics": layout.semantics if layout else LEGACY_SEMANTICS,
            "warning": layout.warning if layout else LEGACY_WARNING,
            "canonical": False,
            "self_computed": False,
            "frozen": True,
            # Per layout: the Codex ledger's cached-token double-count is a
            # measured overstatement, while Claude daily aggregates have
            # unresolved overlap and indeterminate bias. Response-level
            # streaming maxima do not prove a daily population bound.
            "cost_bias": layout.cost_bias if layout else "overstates",
            "sources": [row["source"] for row in counts["sources"]],
            "days_served": counts["days_served"],
            "days_superseded_by_self_computed": counts["days_superseded_by_self_computed"],
            "first_day": min((row.usage_date for row in served), default=None),
            "last_day": max((row.usage_date for row in served), default=None),
        }

    def legacy_counts(self, provider: str) -> dict[str, Any]:
        """What is stored, and how much of it a read would currently suppress."""

        provider = _normalized_provider(provider)
        stored = self.connection.execute(
            "SELECT source, COUNT(*) AS rows, COUNT(DISTINCT usage_date) AS days, "
            "MIN(usage_date) AS first_day, MAX(usage_date) AS last_day "
            "FROM legacy_daily WHERE provider=? GROUP BY source ORDER BY source",
            (provider,),
        ).fetchall()
        stored_days = self.connection.execute(
            "SELECT COUNT(DISTINCT usage_date) AS days FROM legacy_daily WHERE provider=?",
            (provider,),
        ).fetchone()["days"]
        served_days = len({row.usage_date for row in self.legacy_totals(provider)})
        return {
            "sources": [dict(row) for row in stored],
            "rows_stored": sum(row["rows"] for row in stored),
            "days_stored": stored_days,
            "days_served": served_days,
            "days_superseded_by_self_computed": stored_days - served_days,
        }

    @staticmethod
    def _file_filter(provider: str, root: Path | str | None) -> tuple[str, list[object]]:
        """The `scanned_file` predicate every read shares, as ``(sql, params)``."""

        where = "f.provider=?"
        params: list[object] = [provider]
        if root is not None:
            where += " AND f.root_digest=?"
            params.append(root_identity_digest(root))
        return where, params

    def file_counts(self, provider: str, *, root: Path | str | None = None) -> dict[str, int]:
        """The stored per-file counters, summed, for one provider."""

        provider = _normalized_provider(provider)
        columns = (
            "records_scanned",
            "records_accepted",
            "records_rejected",
            "records_deduplicated",
            "records_unidentified",
            "parse_error_count",
        )
        where, params = self._file_filter(provider, root)
        totals = ", ".join(f"COALESCE(SUM(f.{column}), 0) AS {column}" for column in columns)
        row = self.connection.execute(
            f"SELECT COUNT(*) AS files, {totals} FROM scanned_file AS f WHERE {where}", params
        ).fetchone()
        return {"files": row["files"], **{column: row[column] for column in columns}}

    def manifest_digest(self, provider: str, *, root: Path | str | None = None) -> str:
        """A digest over what the store believes it has read, not over content."""

        provider = _normalized_provider(provider)
        where, params = self._file_filter(provider, root)
        digest = hashlib.sha256()
        for row in self.connection.execute(
            f"SELECT f.path AS path, f.size AS size FROM scanned_file AS f "
            f"WHERE {where} ORDER BY f.path",
            params,
        ):
            digest.update(f"{row['path']}\0{row['size']}\0".encode())
        return digest.hexdigest()

    def request_pricing_bands(self, provider: str, *, root: Path | str | None = None) -> tuple[dict[str, Any], ...]:
        """Group canonical requests AFTER classifying their prompt contexts.

        These bounded slot/model/context/metadata bands preserve nonlinear
        request pricing without materializing the complete receipt table in
        Python. Aggregated daily token sums must never choose a context tier.
        """
        provider = _normalized_provider(provider)
        where, params = self._file_filter(provider, root)
        where += self._visible_slot_guard("s")
        metadata = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_receipt_metadata'"
        ).fetchone() is not None
        fields = ("m.service_tier,m.inference_speed,m.region" if metadata else
                  "NULL AS service_tier,NULL AS inference_speed,NULL AS region")
        join = "LEFT JOIN usage_receipt_metadata AS m ON m.fingerprint=s.fingerprint " if metadata else ""
        prompt = "+".join(f"s.{col}" for col in _TOKEN_COLUMNS if col != "output_tokens")
        context = f"(({prompt}) > CASE WHEN LOWER(s.model) LIKE 'claude-haiku-5-5%' THEN 100000 ELSE 272000 END)"
        group = "s.usage_slot,s.model,context_long" + (",m.service_tier,m.inference_speed,m.region" if metadata else "")
        rows = self.connection.execute(
            f"SELECT s.usage_slot,s.model,{context} AS context_long,{fields},"
            + ",".join(f"SUM(s.{col}) AS {col}" for col in _TOKEN_COLUMNS)
            + ",COUNT(*) AS request_count FROM seen_message AS s "
            "JOIN scanned_file AS f ON f.file_id=s.file_id " + join
            + f"WHERE {where} AND s.representative=1 GROUP BY {group} ORDER BY s.usage_slot,s.model",
            params,
        )
        return tuple(dict(row) for row in rows)

    def history_import(
        self, root: Path | str, *, provider: str, tz: tzinfo | None = None
    ) -> LocalHistoryImport | None:
        """Present the stored scan in the shape a live scan returns, or nothing.

        ``None`` means "this store has nothing to say about that root", which
        is the caller's signal to fall back to a live bounded scan rather than
        to report an empty history. ``rows`` are days in ``tz`` (the system
        zone when ``None``); ``slot_rows`` carry the UTC slots they came from.
        """

        provider = _normalized_provider(provider)
        slots = self.slot_totals(provider, root=root)
        rows = rollup_days(slots, tz)
        legacy = self.legacy_totals(provider)
        unplaced = self.unplaced_claude_totals() if provider == "claude" else None
        if not rows and not legacy and not (unplaced and unplaced["unplaced_identities"]):
            return None
        counts = self.file_counts(provider, root=root)
        return LocalHistoryImport(
            provider=provider,
            rows=rows,
            slot_rows=slots,
            request_pricing_bands=self.request_pricing_bands(provider, root=root),
            unplaced_usage=unplaced,
            legacy_rows=legacy,
            legacy_provenance=self.legacy_provenance(provider) if legacy else None,
            coverage_state="partial",
            root_identity_digest=root_identity_digest(root),
            manifest_digest=self.manifest_digest(provider, root=root),
            files_scanned=counts["files"],
            records_scanned=counts["records_scanned"],
            records_accepted=counts["records_accepted"],
            records_rejected=counts["records_rejected"],
            parse_error_count=counts["parse_error_count"],
            records_deduplicated=counts["records_deduplicated"],
            records_unidentified=counts["records_unidentified"],
        )


def read_store_history(
    root: Path | str,
    *,
    provider: str,
    store_path: Path | str | None = None,
    tz: tzinfo | None = None,
) -> LocalHistoryImport | None:
    """Read a stored scan without creating, migrating, or locking anything.

    A dashboard refresh must never be the thing that creates the store: an
    absent, unreadable, or empty store answers ``None`` so the caller can fall
    back to a live bounded scan.
    """

    try:
        with UsageScanStore(store_path, read_only=True) as store:
            return store.history_import(root, provider=provider, tz=tz)
    except (ScanStoreError, sqlite3.Error, OSError):
        return None


def scan_providers(
    home: Path | str,
    *,
    providers: Sequence[str] = PROVIDERS,
    store_path: Path | str | None = None,
    max_files: int | None = None,
    rebuild: bool = False,
    on_result: Callable[[ScanResult], None] | None = None,
) -> list[ScanResult]:
    """Scan each provider's CLI home beneath one user home directory."""

    home = Path(home)
    results: list[ScanResult] = []
    with UsageScanStore(store_path) as store:
        for provider in providers:
            name = _normalized_provider(provider)
            root = home / (".claude" if name == "claude" else ".codex")
            _logger.info(
                "usage scan %s: reading %s",
                name,
                ", ".join(str(scan_root) for scan_root in history_scan_roots(root, name)),
            )
            result = (
                store.rebuild(root, provider=name)
                if rebuild
                else store.scan(root, provider=name, max_files=max_files)
            )
            _logger.info(
                "usage scan %s: done -- %d parsed, %d unchanged, %d accepted, %d deduplicated "
                "in %.1fs",
                name,
                result.files_parsed,
                result.files_unchanged,
                result.records_accepted,
                result.records_deduplicated,
                result.elapsed_seconds,
            )
            results.append(result)
            if on_result is not None:
                on_result(result)
    return results
