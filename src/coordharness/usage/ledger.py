
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator, Mapping, Sequence
import uuid


SCHEMA_ID = "coordharness.usage-v2.sqlite.v2"
UNKNOWN_ACCOUNT = "unknown"
PROVIDERS = frozenset({"claude", "codex"})
COVERAGE_STATES = frozenset({"complete", "partial", "unknown", "error"})
OBSERVATION_ROLES = frozenset(
    {"raw", "cache_checkpoint", "legacy_high_water", "screenshot", "manual"}
)
BILLING_CATEGORIES = frozenset(
    {"usage", "subscription", "credit", "refund", "adjustment", "tax", "other"}
)
TOKEN_METRICS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_create_5m_tokens",
    "cache_create_1h_tokens",
    "cache_create_other_tokens",
)
COST_METRICS = ("provider_native_cost_nanos", "api_rate_estimate_nanos")
ALL_METRICS = TOKEN_METRICS + COST_METRICS + ("request_count",)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class LedgerError(RuntimeError):
    pass


class LedgerIntegrityError(LedgerError):
    pass


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _validate_digest(value: str, *, field: str) -> str:
    normalized = value.lower()
    if not _DIGEST.fullmatch(normalized):
        raise LedgerIntegrityError(f"{field} must be a lowercase SHA-256 digest")
    return normalized


def _validate_day(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise LedgerIntegrityError(f"invalid usage_date {value!r}") from exc
    if parsed.isoformat() != value:
        raise LedgerIntegrityError(f"usage_date must be canonical ISO date: {value!r}")
    return value


def _validate_timestamp(value: str, *, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerIntegrityError(f"invalid {field}: {value!r}") from exc
    if parsed.tzinfo is None:
        raise LedgerIntegrityError(f"{field} must include a timezone")
    return value


def _validate_nonempty(value: str, *, field: str) -> str:
    if not value or value.strip() != value:
        raise LedgerIntegrityError(f"{field} must be nonempty and trimmed")
    return value


def _validate_optional_count(value: int | None, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LedgerIntegrityError(f"{field} must be a nonnegative integer or null")
    return value


def _observation_identity_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:

    return {
        key: metadata[key]
        for key in (
            "artifact_digest",
            "artifact_version",
            "producer_key",
            "parser_version",
            "pricing_key",
            "coverage_state",
            "observation_role",
            "canonical_eligible",
            "coverage_start",
            "coverage_end",
            "source_manifest_digest",
            "cursor_before",
            "cursor_after",
            "files_scanned",
            "records_scanned",
            "records_accepted",
            "records_rejected",
            "records_conflicted",
            "parse_error_count",
        )
    }


@dataclass(frozen=True)
class SourceInstance:
    provider: str
    account_key: str
    source_kind: str
    root_uri: str
    timezone_name: str
    root_identity_digest: str
    label: str = ""

    def normalized(self) -> dict[str, Any]:
        provider = self.provider.lower()
        if provider not in PROVIDERS:
            raise LedgerIntegrityError(f"unsupported provider: {self.provider!r}")
        account = _validate_nonempty(self.account_key, field="account_key")
        if account == UNKNOWN_ACCOUNT:
            raise LedgerIntegrityError(
                "unknown accounts must be source-scoped, for example unknown:<profile-digest>"
            )
        if account.startswith(f"{UNKNOWN_ACCOUNT}:") and len(account.split(":", 1)[1]) < 8:
            raise LedgerIntegrityError("source-scoped unknown account suffix is too short")
        source_kind = _validate_nonempty(self.source_kind, field="source_kind")
        root_uri = _validate_nonempty(self.root_uri, field="root_uri")
        timezone_name = _validate_nonempty(self.timezone_name, field="timezone_name")
        return {
            "provider": provider,
            "account_key": account,
            "source_kind": source_kind,
            "root_uri": root_uri,
            "timezone_name": timezone_name,
            "root_identity_digest": _validate_digest(
                self.root_identity_digest, field="root_identity_digest"
            ),
            "label": self.label.strip(),
        }


@dataclass(frozen=True)
class DailyUsage:
    usage_date: str
    model: str
    tier: str = "default"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_create_5m_tokens: int = 0
    cache_create_1h_tokens: int = 0
    cache_create_other_tokens: int = 0
    request_count: int | None = None
    provider_native_cost_nanos: int | None = None
    api_rate_estimate_nanos: int | None = None

    def normalized(self) -> dict[str, Any]:
        result = asdict(self)
        result["usage_date"] = _validate_day(self.usage_date)
        result["model"] = _validate_nonempty(self.model, field="model")
        result["tier"] = _validate_nonempty(self.tier, field="tier")
        for metric in TOKEN_METRICS:
            result[metric] = _validate_optional_count(result[metric], field=metric)
            assert result[metric] is not None
        for metric in COST_METRICS + ("request_count",):
            result[metric] = _validate_optional_count(result[metric], field=metric)
        return result


@dataclass(frozen=True)
class ObservationInput:
    artifact_digest: str
    artifact_version: str
    producer_key: str
    parser_version: str
    pricing_key: str
    captured_at: str
    coverage_state: str
    observation_role: str
    canonical_eligible: bool
    rows: Sequence[DailyUsage]
    artifact_uri: str = ""
    notes: str = ""
    coverage_start: str | None = None
    coverage_end: str | None = None
    source_manifest_digest: str | None = None
    cursor_before: str | None = None
    cursor_after: str | None = None
    files_scanned: int | None = None
    records_scanned: int | None = None
    records_accepted: int | None = None
    records_rejected: int = 0
    records_conflicted: int = 0
    parse_error_count: int = 0

    def normalized(self) -> dict[str, Any]:
        coverage = self.coverage_state.lower()
        if coverage not in COVERAGE_STATES:
            raise LedgerIntegrityError(f"unsupported coverage_state: {self.coverage_state!r}")
        role = self.observation_role.lower()
        if role not in OBSERVATION_ROLES:
            raise LedgerIntegrityError(f"unsupported observation_role: {self.observation_role!r}")
        normalized_rows = [row.normalized() for row in self.rows]
        normalized_rows.sort(key=lambda row: (row["usage_date"], row["model"], row["tier"]))
        keys = [(row["usage_date"], row["model"], row["tier"]) for row in normalized_rows]
        if len(keys) != len(set(keys)):
            raise LedgerIntegrityError("an observation cannot contain duplicate day/model/tier rows")
        if not normalized_rows:
            raise LedgerIntegrityError("an observation must contain at least one daily row")
        coverage_start = (
            _validate_timestamp(self.coverage_start, field="coverage_start")
            if self.coverage_start
            else None
        )
        coverage_end = (
            _validate_timestamp(self.coverage_end, field="coverage_end")
            if self.coverage_end
            else None
        )
        if coverage_start and coverage_end:
            start = datetime.fromisoformat(coverage_start.replace("Z", "+00:00"))
            end = datetime.fromisoformat(coverage_end.replace("Z", "+00:00"))
            if start > end:
                raise LedgerIntegrityError("coverage_start cannot be after coverage_end")
        source_manifest_digest = (
            _validate_digest(self.source_manifest_digest, field="source_manifest_digest")
            if self.source_manifest_digest
            else None
        )
        counts = {
            field: _validate_optional_count(getattr(self, field), field=field)
            for field in (
                "files_scanned",
                "records_scanned",
                "records_accepted",
                "records_rejected",
                "records_conflicted",
                "parse_error_count",
            )
        }
        if coverage == "complete":
            required = {
                "coverage_start": coverage_start,
                "coverage_end": coverage_end,
                "source_manifest_digest": source_manifest_digest,
                "files_scanned": counts["files_scanned"],
                "records_scanned": counts["records_scanned"],
                "records_accepted": counts["records_accepted"],
            }
            missing = sorted(key for key, value in required.items() if value is None)
            if missing:
                raise LedgerIntegrityError(
                    "complete coverage requires " + ", ".join(missing)
                )
            if any(counts[field] for field in ("records_rejected", "records_conflicted", "parse_error_count")):
                raise LedgerIntegrityError(
                    "complete coverage cannot contain rejects, conflicts, or parse errors"
                )
            assert counts["records_scanned"] is not None
            assert counts["records_accepted"] is not None
            if counts["records_accepted"] > counts["records_scanned"]:
                raise LedgerIntegrityError("records_accepted cannot exceed records_scanned")
        return {
            "artifact_digest": _validate_digest(self.artifact_digest, field="artifact_digest"),
            "artifact_version": _validate_nonempty(
                self.artifact_version, field="artifact_version"
            ),
            "producer_key": _validate_nonempty(self.producer_key, field="producer_key"),
            "parser_version": _validate_nonempty(self.parser_version, field="parser_version"),
            "pricing_key": _validate_nonempty(self.pricing_key, field="pricing_key"),
            "captured_at": _validate_timestamp(self.captured_at, field="captured_at"),
            "coverage_state": coverage,
            "observation_role": role,
            "canonical_eligible": bool(self.canonical_eligible),
            "artifact_uri": self.artifact_uri.strip(),
            "notes": self.notes.strip(),
            "coverage_start": coverage_start,
            "coverage_end": coverage_end,
            "source_manifest_digest": source_manifest_digest,
            "cursor_before": self.cursor_before,
            "cursor_after": self.cursor_after,
            **counts,
            "rows": normalized_rows,
        }


@dataclass(frozen=True)
class BillingLineItem:
    provider: str
    account_key: str
    provider_item_key: str
    occurred_at: str
    currency: str
    amount_nanos: int
    category: str
    artifact_digest: str
    source_id: str | None = None
    description: str = ""

    def normalized(self) -> dict[str, Any]:
        provider = self.provider.lower()
        if provider not in PROVIDERS:
            raise LedgerIntegrityError(f"unsupported provider: {self.provider!r}")
        account_key = _validate_nonempty(self.account_key, field="account_key")
        if account_key == UNKNOWN_ACCOUNT:
            raise LedgerIntegrityError("billing account must be explicit or source-scoped unknown")
        if isinstance(self.amount_nanos, bool) or not isinstance(self.amount_nanos, int):
            raise LedgerIntegrityError("billing amount_nanos must be a signed integer")
        currency = _validate_nonempty(self.currency.upper(), field="currency")
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise LedgerIntegrityError("billing currency must be a three-letter ISO code")
        category = self.category.lower()
        if category not in BILLING_CATEGORIES:
            raise LedgerIntegrityError(f"unsupported billing category: {self.category!r}")
        return {
            "provider": provider,
            "account_key": account_key,
            "provider_item_key": _validate_nonempty(
                self.provider_item_key, field="provider_item_key"
            ),
            "occurred_at": _validate_timestamp(self.occurred_at, field="occurred_at"),
            "currency": currency,
            "amount_nanos": self.amount_nanos,
            "category": category,
            "artifact_digest": _validate_digest(
                self.artifact_digest, field="artifact_digest"
            ),
            "source_id": self.source_id,
            "description": self.description.strip(),
        }


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ledger_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS source_instance (
    source_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL CHECK (provider IN ('claude', 'codex')),
    account_key TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    root_uri TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    root_identity_digest TEXT NOT NULL,
    label TEXT NOT NULL,
    record_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(provider, account_key, source_kind, root_uri, timezone_name)
) STRICT;

CREATE TABLE IF NOT EXISTS artifact (
    artifact_digest TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS source_occurrence (
    occurrence_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source_instance(source_id),
    artifact_digest TEXT NOT NULL REFERENCES artifact(artifact_digest),
    artifact_uri TEXT NOT NULL,
    first_observed_at TEXT NOT NULL,
    occurrence_digest TEXT NOT NULL,
    UNIQUE(source_id, artifact_digest, artifact_uri)
) STRICT;

CREATE TABLE IF NOT EXISTS observation (
    observation_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source_instance(source_id),
    artifact_digest TEXT NOT NULL,
    artifact_version TEXT NOT NULL,
    producer_key TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    pricing_key TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    coverage_state TEXT NOT NULL CHECK (coverage_state IN ('complete', 'partial', 'unknown', 'error')),
    observation_role TEXT NOT NULL CHECK (
        observation_role IN ('raw', 'cache_checkpoint', 'legacy_high_water', 'screenshot', 'manual')
    ),
    canonical_eligible INTEGER NOT NULL CHECK (canonical_eligible IN (0, 1)),
    artifact_uri TEXT NOT NULL,
    notes TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    UNIQUE(source_id, artifact_digest, artifact_version, producer_key, parser_version, pricing_key)
) STRICT;

CREATE TABLE IF NOT EXISTS coverage_interval (
    observation_id TEXT PRIMARY KEY REFERENCES observation(observation_id),
    coverage_start TEXT,
    coverage_end TEXT,
    source_manifest_digest TEXT,
    cursor_before TEXT,
    cursor_after TEXT,
    files_scanned INTEGER CHECK (files_scanned IS NULL OR files_scanned >= 0),
    records_scanned INTEGER CHECK (records_scanned IS NULL OR records_scanned >= 0),
    records_accepted INTEGER CHECK (records_accepted IS NULL OR records_accepted >= 0),
    records_rejected INTEGER NOT NULL CHECK (records_rejected >= 0),
    records_conflicted INTEGER NOT NULL CHECK (records_conflicted >= 0),
    parse_error_count INTEGER NOT NULL CHECK (parse_error_count >= 0),
    coverage_digest TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS daily_observation (
    observation_id TEXT NOT NULL REFERENCES observation(observation_id),
    usage_date TEXT NOT NULL,
    model TEXT NOT NULL,
    tier TEXT NOT NULL,
    input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    cache_read_tokens INTEGER NOT NULL CHECK (cache_read_tokens >= 0),
    cache_create_5m_tokens INTEGER NOT NULL CHECK (cache_create_5m_tokens >= 0),
    cache_create_1h_tokens INTEGER NOT NULL CHECK (cache_create_1h_tokens >= 0),
    cache_create_other_tokens INTEGER NOT NULL CHECK (cache_create_other_tokens >= 0),
    request_count INTEGER CHECK (request_count IS NULL OR request_count >= 0),
    provider_native_cost_nanos INTEGER CHECK (
        provider_native_cost_nanos IS NULL OR provider_native_cost_nanos >= 0
    ),
    api_rate_estimate_nanos INTEGER CHECK (
        api_rate_estimate_nanos IS NULL OR api_rate_estimate_nanos >= 0
    ),
    row_digest TEXT NOT NULL,
    PRIMARY KEY(observation_id, usage_date, model, tier)
) STRICT;

CREATE TABLE IF NOT EXISTS canonical_decision (
    decision_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL CHECK (provider IN ('claude', 'codex')),
    account_key TEXT NOT NULL,
    usage_date TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    model TEXT NOT NULL,
    tier TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('select', 'withdraw')),
    observation_id TEXT REFERENCES observation(observation_id),
    reason TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    prior_decision_id TEXT,
    decision_digest TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS ledger_commit (
    commit_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_kind TEXT NOT NULL CHECK (entity_kind IN ('source', 'observation', 'decision', 'billing')),
    entity_id TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    previous_commit_hash TEXT,
    commit_hash TEXT NOT NULL UNIQUE,
    UNIQUE(entity_kind, entity_id)
) STRICT;

CREATE TABLE IF NOT EXISTS source_cursor (
    cursor_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES source_instance(source_id),
    cursor_before TEXT,
    cursor_after TEXT NOT NULL,
    observation_id TEXT NOT NULL REFERENCES observation(observation_id),
    advanced_at TEXT NOT NULL,
    cursor_digest TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS external_anchor_receipt (
    anchor_digest TEXT PRIMARY KEY,
    commit_seq INTEGER NOT NULL,
    commit_hash TEXT NOT NULL,
    ledger_uuid TEXT NOT NULL,
    anchor_uri TEXT NOT NULL,
    recorded_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS billing_line_item (
    billing_item_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL CHECK (provider IN ('claude', 'codex')),
    account_key TEXT NOT NULL,
    provider_item_key TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_nanos INTEGER NOT NULL,
    category TEXT NOT NULL CHECK (
        category IN ('usage', 'subscription', 'credit', 'refund', 'adjustment', 'tax', 'other')
    ),
    artifact_digest TEXT NOT NULL,
    source_id TEXT REFERENCES source_instance(source_id),
    description TEXT NOT NULL,
    item_digest TEXT NOT NULL,
    UNIQUE(provider, account_key, provider_item_key)
) STRICT;

CREATE VIEW IF NOT EXISTS daily_corrected AS
WITH latest AS (
    SELECT provider, account_key, usage_date, timezone_name, model, tier,
           MAX(decision_seq) AS decision_seq
      FROM canonical_decision
     GROUP BY provider, account_key, usage_date, timezone_name, model, tier
)
SELECT d.provider, d.account_key, d.usage_date, d.timezone_name, d.model, d.tier,
       d.observation_id, o.coverage_state, o.observation_role,
       r.input_tokens, r.output_tokens, r.cache_read_tokens,
       r.cache_create_5m_tokens, r.cache_create_1h_tokens,
       r.cache_create_other_tokens, r.request_count,
       r.provider_native_cost_nanos, r.api_rate_estimate_nanos
  FROM latest l
  JOIN canonical_decision d ON d.decision_seq = l.decision_seq
  JOIN observation o ON o.observation_id = d.observation_id
  JOIN daily_observation r
    ON r.observation_id = d.observation_id
   AND r.usage_date = d.usage_date
   AND r.model = d.model
   AND r.tier = d.tier
 WHERE d.action = 'select';

CREATE VIEW IF NOT EXISTS daily_custody_metric AS
SELECT s.provider, s.account_key, r.usage_date, s.timezone_name, r.model, r.tier,
       r.observation_id, 'input_tokens' AS metric, r.input_tokens AS value
  FROM daily_observation r JOIN observation o USING(observation_id)
  JOIN source_instance s USING(source_id)
UNION ALL SELECT s.provider, s.account_key, r.usage_date, s.timezone_name, r.model, r.tier,
       r.observation_id, 'output_tokens', r.output_tokens
  FROM daily_observation r JOIN observation o USING(observation_id)
  JOIN source_instance s USING(source_id)
UNION ALL SELECT s.provider, s.account_key, r.usage_date, s.timezone_name, r.model, r.tier,
       r.observation_id, 'cache_read_tokens', r.cache_read_tokens
  FROM daily_observation r JOIN observation o USING(observation_id)
  JOIN source_instance s USING(source_id)
UNION ALL SELECT s.provider, s.account_key, r.usage_date, s.timezone_name, r.model, r.tier,
       r.observation_id, 'cache_create_5m_tokens', r.cache_create_5m_tokens
  FROM daily_observation r JOIN observation o USING(observation_id)
  JOIN source_instance s USING(source_id)
UNION ALL SELECT s.provider, s.account_key, r.usage_date, s.timezone_name, r.model, r.tier,
       r.observation_id, 'cache_create_1h_tokens', r.cache_create_1h_tokens
  FROM daily_observation r JOIN observation o USING(observation_id)
  JOIN source_instance s USING(source_id)
UNION ALL SELECT s.provider, s.account_key, r.usage_date, s.timezone_name, r.model, r.tier,
       r.observation_id, 'cache_create_other_tokens', r.cache_create_other_tokens
  FROM daily_observation r JOIN observation o USING(observation_id)
  JOIN source_instance s USING(source_id)
UNION ALL SELECT s.provider, s.account_key, r.usage_date, s.timezone_name, r.model, r.tier,
       r.observation_id, 'request_count', r.request_count
  FROM daily_observation r JOIN observation o USING(observation_id)
  JOIN source_instance s USING(source_id) WHERE r.request_count IS NOT NULL
UNION ALL SELECT s.provider, s.account_key, r.usage_date, s.timezone_name, r.model, r.tier,
       r.observation_id, 'provider_native_cost_nanos', r.provider_native_cost_nanos
  FROM daily_observation r JOIN observation o USING(observation_id)
  JOIN source_instance s USING(source_id) WHERE r.provider_native_cost_nanos IS NOT NULL
UNION ALL SELECT s.provider, s.account_key, r.usage_date, s.timezone_name, r.model, r.tier,
       r.observation_id, 'api_rate_estimate_nanos', r.api_rate_estimate_nanos
  FROM daily_observation r JOIN observation o USING(observation_id)
  JOIN source_instance s USING(source_id) WHERE r.api_rate_estimate_nanos IS NOT NULL;

CREATE VIEW IF NOT EXISTS daily_custody_high_water_metric AS
WITH ranked AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY provider, account_key, usage_date, timezone_name, model, tier, metric
        ORDER BY value DESC, observation_id ASC
    ) AS metric_rank
    FROM daily_custody_metric
)
SELECT provider, account_key, usage_date, timezone_name, model, tier,
       metric, value, observation_id AS origin_observation_id
  FROM ranked
 WHERE metric_rank = 1;

CREATE TRIGGER IF NOT EXISTS source_instance_no_update
BEFORE UPDATE ON source_instance BEGIN SELECT RAISE(ABORT, 'usage-v2 immutable table'); END;
CREATE TRIGGER IF NOT EXISTS source_instance_no_delete
BEFORE DELETE ON source_instance BEGIN SELECT RAISE(ABORT, 'usage-v2 immutable table'); END;
CREATE TRIGGER IF NOT EXISTS artifact_no_update
BEFORE UPDATE ON artifact BEGIN SELECT RAISE(ABORT, 'usage-v2 immutable table'); END;
CREATE TRIGGER IF NOT EXISTS artifact_no_delete
BEFORE DELETE ON artifact BEGIN SELECT RAISE(ABORT, 'usage-v2 immutable table'); END;
CREATE TRIGGER IF NOT EXISTS source_occurrence_no_update
BEFORE UPDATE ON source_occurrence BEGIN SELECT RAISE(ABORT, 'usage-v2 immutable table'); END;
CREATE TRIGGER IF NOT EXISTS source_occurrence_no_delete
BEFORE DELETE ON source_occurrence BEGIN SELECT RAISE(ABORT, 'usage-v2 immutable table'); END;
CREATE TRIGGER IF NOT EXISTS observation_no_update
BEFORE UPDATE ON observation BEGIN SELECT RAISE(ABORT, 'usage-v2 immutable table'); END;
CREATE TRIGGER IF NOT EXISTS observation_no_delete
BEFORE DELETE ON observation BEGIN SELECT RAISE(ABORT, 'usage-v2 immutable table'); END;
CREATE TRIGGER IF NOT EXISTS coverage_interval_no_update
BEFORE UPDATE ON coverage_interval BEGIN SELECT RAISE(ABORT, 'usage-v2 immutable table'); END;
CREATE TRIGGER IF NOT EXISTS coverage_interval_no_delete
BEFORE DELETE ON coverage_interval BEGIN SELECT RAISE(ABORT, 'usage-v2 immutable table'); END;
CREATE TRIGGER IF NOT EXISTS daily_observation_no_update
BEFORE UPDATE ON daily_observation BEGIN SELECT RAISE(ABORT, 'usage-v2 immutable table'); END;
CREATE TRIGGER IF NOT EXISTS daily_observation_no_delete
BEFORE DELETE ON daily_observation BEGIN SELECT RAISE(ABORT, 'usage-v2 immutable table'); END;
CREATE TRIGGER IF NOT EXISTS canonical_decision_no_update
BEFORE UPDATE ON canonical_decision BEGIN SELECT RAISE(ABORT, 'usage-v2 immutable table'); END;
CREATE TRIGGER IF NOT EXISTS canonical_decision_no_delete
BEFORE DELETE ON canonical_decision BEGIN SELECT RAISE(ABORT, 'usage-v2 immutable table'); END;
CREATE TRIGGER IF NOT EXISTS ledger_commit_no_update
BEFORE UPDATE ON ledger_commit BEGIN SELECT RAISE(ABORT, 'usage-v2 immutable table'); END;
CREATE TRIGGER IF NOT EXISTS ledger_commit_no_delete
BEFORE DELETE ON ledger_commit BEGIN SELECT RAISE(ABORT, 'usage-v2 immutable table'); END;
CREATE TRIGGER IF NOT EXISTS billing_line_item_no_update
BEFORE UPDATE ON billing_line_item BEGIN SELECT RAISE(ABORT, 'usage-v2 immutable table'); END;
CREATE TRIGGER IF NOT EXISTS billing_line_item_no_delete
BEFORE DELETE ON billing_line_item BEGIN SELECT RAISE(ABORT, 'usage-v2 immutable table'); END;
"""


def _schema_fingerprints(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute(
        """
        SELECT type, name, sql FROM sqlite_master
         WHERE type IN ('table', 'view', 'trigger')
           AND name NOT LIKE 'sqlite_%'
         ORDER BY type, name
        """
    ).fetchall()
    return {
        f"{row[0]}:{row[1]}": hashlib.sha256(
            " ".join(str(row[2] or "").split()).encode("utf-8")
        ).hexdigest()
        for row in rows
    }


def _expected_schema_fingerprints() -> dict[str, str]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(SCHEMA_SQL)
        return _schema_fingerprints(connection)
    finally:
        connection.close()


EXPECTED_SCHEMA_FINGERPRINTS = _expected_schema_fingerprints()


class UsageLedger:

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        objects = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        has_meta = any(row[0] == "ledger_meta" for row in objects)
        try:
            if has_meta:
                observed_row = self.connection.execute(
                    "SELECT value FROM ledger_meta WHERE key = 'schema'"
                ).fetchone()
                observed = observed_row[0] if observed_row else None
                if observed != SCHEMA_ID:
                    raise LedgerIntegrityError(f"unsupported ledger schema: {observed!r}")
                self._assert_schema_shape()
                uuid_row = self.connection.execute(
                    "SELECT value FROM ledger_meta WHERE key = 'ledger_uuid'"
                ).fetchone()
                if uuid_row is None or not uuid_row[0]:
                    raise LedgerIntegrityError("usage-v2 ledger has no immutable ledger_uuid")
            else:
                if objects:
                    raise LedgerIntegrityError(
                        "refusing to initialize over an unrecognized nonempty SQLite database"
                    )
                self.connection.executescript(SCHEMA_SQL)
                self.connection.execute(
                    "INSERT INTO ledger_meta(key, value) VALUES('schema', ?)",
                    (SCHEMA_ID,),
                )
                self.connection.execute(
                    "INSERT INTO ledger_meta(key, value) VALUES('ledger_uuid', ?)",
                    (str(uuid.uuid4()),),
                )
                self._assert_schema_shape()
                self.connection.commit()
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.execute("PRAGMA synchronous = FULL")
        except BaseException:
            self.connection.close()
            raise

    def _schema_shape_failures(self) -> list[dict[str, Any]]:
        observed = _schema_fingerprints(self.connection)
        failures: list[dict[str, Any]] = []
        for key in sorted(EXPECTED_SCHEMA_FINGERPRINTS.keys() - observed.keys()):
            failures.append({"code": "SCHEMA_OBJECT_MISSING", "object": key})
        for key in sorted(observed.keys() - EXPECTED_SCHEMA_FINGERPRINTS.keys()):
            failures.append({"code": "SCHEMA_OBJECT_UNEXPECTED", "object": key})
        for key in sorted(observed.keys() & EXPECTED_SCHEMA_FINGERPRINTS.keys()):
            if observed[key] != EXPECTED_SCHEMA_FINGERPRINTS[key]:
                failures.append({"code": "SCHEMA_OBJECT_MISMATCH", "object": key})
        return failures

    def _assert_schema_shape(self) -> None:
        failures = self._schema_shape_failures()
        if failures:
            raise LedgerIntegrityError(
                "usage-v2 schema shape mismatch: " + _canonical_json(failures)
            )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "UsageLedger":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _append_commit(
        self,
        connection: sqlite3.Connection,
        *,
        entity_kind: str,
        entity_id: str,
        payload: Mapping[str, Any],
        committed_at: str,
    ) -> None:
        payload_json = _canonical_json(payload)
        payload_digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        previous = connection.execute(
            "SELECT commit_hash FROM ledger_commit ORDER BY commit_seq DESC LIMIT 1"
        ).fetchone()
        previous_hash = previous[0] if previous else None
        commit_hash = _digest(
            {
                "entity_kind": entity_kind,
                "entity_id": entity_id,
                "committed_at": committed_at,
                "payload_digest": payload_digest,
                "previous_commit_hash": previous_hash,
            }
        )
        connection.execute(
            """
            INSERT INTO ledger_commit(
                entity_kind, entity_id, committed_at, payload_json, payload_digest,
                previous_commit_hash, commit_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_kind,
                entity_id,
                committed_at,
                payload_json,
                payload_digest,
                previous_hash,
                commit_hash,
            ),
        )

    def register_source(self, source: SourceInstance) -> str:
        record = source.normalized()
        source_identity = {
            key: record[key]
            for key in (
                "provider",
                "account_key",
                "source_kind",
                "root_uri",
                "timezone_name",
            )
        }
        source_id = _digest(source_identity)
        existing = self.connection.execute(
            "SELECT * FROM source_instance WHERE source_id = ?", (source_id,)
        ).fetchone()
        if existing:
            observed = {
                key: existing[key]
                for key in (
                    "provider",
                    "account_key",
                    "source_kind",
                    "root_uri",
                    "timezone_name",
                    "root_identity_digest",
                    "label",
                )
            }
            if observed != record:
                raise LedgerIntegrityError(
                    "source identity already exists with different immutable metadata"
                )
            return source_id
        created_at = _utc_now()
        record_digest = _digest(record)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO source_instance(
                    source_id, provider, account_key, source_kind, root_uri,
                    timezone_name, root_identity_digest, label, record_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    record["provider"],
                    record["account_key"],
                    record["source_kind"],
                    record["root_uri"],
                    record["timezone_name"],
                    record["root_identity_digest"],
                    record["label"],
                    record_digest,
                    created_at,
                ),
            )
            self._append_commit(
                connection,
                entity_kind="source",
                entity_id=source_id,
                payload={"record": record, "record_digest": record_digest},
                committed_at=created_at,
            )
        return source_id

    def record_observation(
        self, source_id: str, observation: ObservationInput
    ) -> tuple[str, bool]:
        source = self.connection.execute(
            "SELECT source_id FROM source_instance WHERE source_id = ?", (source_id,)
        ).fetchone()
        if source is None:
            raise LedgerIntegrityError(f"unknown source_id: {source_id}")
        normalized = observation.normalized()
        metadata = {key: value for key, value in normalized.items() if key != "rows"}
        identity_metadata = _observation_identity_metadata(metadata)
        payload_digest = _digest(normalized["rows"])
        observation_id = _digest(
            {
                "source_id": source_id,
                "identity_metadata": identity_metadata,
                "payload_digest": payload_digest,
            }
        )
        occurrence_payload = {
            "source_id": source_id,
            "artifact_digest": metadata["artifact_digest"],
            "artifact_uri": metadata["artifact_uri"],
        }
        occurrence_id = _digest(occurrence_payload)
        existing = self.connection.execute(
            "SELECT payload_digest, metadata_json FROM observation WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        if existing:
            if existing["payload_digest"] != payload_digest:
                raise LedgerIntegrityError("observation digest collision")
            return observation_id, False
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO artifact(artifact_digest, first_seen_at) VALUES (?, ?)",
                (metadata["artifact_digest"], metadata["captured_at"]),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO source_occurrence(
                    occurrence_id, source_id, artifact_digest, artifact_uri,
                    first_observed_at, occurrence_digest
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    occurrence_id,
                    source_id,
                    metadata["artifact_digest"],
                    metadata["artifact_uri"],
                    metadata["captured_at"],
                    _digest(occurrence_payload),
                ),
            )
            inserted = connection.execute(
                    """
                    INSERT OR IGNORE INTO observation(
                        observation_id, source_id, artifact_digest, artifact_version,
                        producer_key, parser_version, pricing_key, captured_at,
                        coverage_state, observation_role, canonical_eligible,
                        artifact_uri, notes, metadata_json, payload_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation_id,
                        source_id,
                        metadata["artifact_digest"],
                        metadata["artifact_version"],
                        metadata["producer_key"],
                        metadata["parser_version"],
                        metadata["pricing_key"],
                        metadata["captured_at"],
                        metadata["coverage_state"],
                        metadata["observation_role"],
                        int(metadata["canonical_eligible"]),
                        metadata["artifact_uri"],
                        metadata["notes"],
                        _canonical_json(metadata),
                        payload_digest,
                    ),
                )
            if inserted.rowcount == 0:
                competing = connection.execute(
                    """
                    SELECT observation_id, payload_digest FROM observation
                     WHERE source_id = ? AND artifact_digest = ? AND artifact_version = ?
                       AND producer_key = ? AND parser_version = ? AND pricing_key = ?
                    """,
                    (
                        source_id,
                        metadata["artifact_digest"],
                        metadata["artifact_version"],
                        metadata["producer_key"],
                        metadata["parser_version"],
                        metadata["pricing_key"],
                    ),
                ).fetchone()
                if (
                    competing is not None
                    and competing["observation_id"] == observation_id
                    and competing["payload_digest"] == payload_digest
                ):
                    return observation_id, False
                raise LedgerIntegrityError(
                    "the same source artifact/parser/pricing identity produced conflicting content"
                )
            coverage = {
                key: metadata[key]
                for key in (
                    "coverage_start",
                    "coverage_end",
                    "source_manifest_digest",
                    "cursor_before",
                    "cursor_after",
                    "files_scanned",
                    "records_scanned",
                    "records_accepted",
                    "records_rejected",
                    "records_conflicted",
                    "parse_error_count",
                )
            }
            connection.execute(
                """
                INSERT INTO coverage_interval(
                    observation_id, coverage_start, coverage_end, source_manifest_digest,
                    cursor_before, cursor_after, files_scanned, records_scanned,
                    records_accepted, records_rejected, records_conflicted,
                    parse_error_count, coverage_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id,
                    coverage["coverage_start"],
                    coverage["coverage_end"],
                    coverage["source_manifest_digest"],
                    coverage["cursor_before"],
                    coverage["cursor_after"],
                    coverage["files_scanned"],
                    coverage["records_scanned"],
                    coverage["records_accepted"],
                    coverage["records_rejected"],
                    coverage["records_conflicted"],
                    coverage["parse_error_count"],
                    _digest(coverage),
                ),
            )
            for row in normalized["rows"]:
                row_digest = _digest(row)
                connection.execute(
                    """
                    INSERT INTO daily_observation(
                        observation_id, usage_date, model, tier, input_tokens,
                        output_tokens, cache_read_tokens, cache_create_5m_tokens,
                        cache_create_1h_tokens, cache_create_other_tokens,
                        request_count, provider_native_cost_nanos, api_rate_estimate_nanos,
                        row_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation_id,
                        row["usage_date"],
                        row["model"],
                        row["tier"],
                        row["input_tokens"],
                        row["output_tokens"],
                        row["cache_read_tokens"],
                        row["cache_create_5m_tokens"],
                        row["cache_create_1h_tokens"],
                        row["cache_create_other_tokens"],
                        row["request_count"],
                        row["provider_native_cost_nanos"],
                        row["api_rate_estimate_nanos"],
                        row_digest,
                    ),
                )
            self._append_commit(
                connection,
                entity_kind="observation",
                entity_id=observation_id,
                payload={
                    "source_id": source_id,
                    "identity_metadata": identity_metadata,
                    "metadata": metadata,
                    "payload_digest": payload_digest,
                    "occurrence_id": occurrence_id,
                },
                committed_at=metadata["captured_at"],
            )
        return observation_id, True

    def select_canonical(
        self,
        observation_id: str,
        *,
        reason: str,
        decided_at: str | None = None,
        allow_partial: bool = False,
    ) -> list[str]:
        reason = _validate_nonempty(reason, field="reason")
        decided_at = _validate_timestamp(decided_at or _utc_now(), field="decided_at")
        observation = self.connection.execute(
            """
            SELECT o.*, s.provider, s.account_key, s.timezone_name
              FROM observation o JOIN source_instance s USING(source_id)
             WHERE observation_id = ?
            """,
            (observation_id,),
        ).fetchone()
        if observation is None:
            raise LedgerIntegrityError(f"unknown observation_id: {observation_id}")
        if not observation["canonical_eligible"]:
            raise LedgerIntegrityError("observation is custody evidence, not canonical-eligible")
        if observation["coverage_state"] != "complete" and not allow_partial:
            raise LedgerIntegrityError(
                "partial or unknown coverage requires explicit allow_partial=True"
            )
        rows = self.connection.execute(
            "SELECT usage_date, model, tier FROM daily_observation WHERE observation_id = ? "
            "ORDER BY usage_date, model, tier",
            (observation_id,),
        ).fetchall()
        decisions: list[tuple[str, dict[str, Any], str | None]] = []
        for row in rows:
            key_values = (
                observation["provider"],
                observation["account_key"],
                row["usage_date"],
                observation["timezone_name"],
                row["model"],
                row["tier"],
            )
            prior = self.connection.execute(
                """
                SELECT decision_id FROM canonical_decision
                 WHERE provider = ? AND account_key = ? AND usage_date = ?
                   AND timezone_name = ? AND model = ? AND tier = ?
                 ORDER BY decision_seq DESC LIMIT 1
                """,
                key_values,
            ).fetchone()
            payload = {
                "provider": key_values[0],
                "account_key": key_values[1],
                "usage_date": key_values[2],
                "timezone_name": key_values[3],
                "model": key_values[4],
                "tier": key_values[5],
                "action": "select",
                "observation_id": observation_id,
                "reason": reason,
                "decided_at": decided_at,
                "prior_decision_id": prior[0] if prior else None,
            }
            decisions.append((_digest(payload), payload, prior[0] if prior else None))
        created: list[str] = []
        with self._transaction() as connection:
            for decision_id, payload, _ in decisions:
                if connection.execute(
                    "SELECT 1 FROM canonical_decision WHERE decision_id = ?", (decision_id,)
                ).fetchone():
                    continue
                decision_digest = _digest(payload)
                connection.execute(
                    """
                    INSERT INTO canonical_decision(
                        decision_id, provider, account_key, usage_date, timezone_name,
                        model, tier, action, observation_id, reason, decided_at,
                        prior_decision_id, decision_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision_id,
                        payload["provider"],
                        payload["account_key"],
                        payload["usage_date"],
                        payload["timezone_name"],
                        payload["model"],
                        payload["tier"],
                        payload["action"],
                        payload["observation_id"],
                        payload["reason"],
                        payload["decided_at"],
                        payload["prior_decision_id"],
                        decision_digest,
                    ),
                )
                self._append_commit(
                    connection,
                    entity_kind="decision",
                    entity_id=decision_id,
                    payload={"decision": payload, "decision_digest": decision_digest},
                    committed_at=decided_at,
                )
                created.append(decision_id)
        return created

    def withdraw_canonical(
        self,
        *,
        provider: str,
        account_key: str,
        usage_date: str,
        timezone_name: str,
        model: str,
        tier: str = "default",
        reason: str,
        decided_at: str | None = None,
    ) -> str:

        provider = provider.lower()
        if provider not in PROVIDERS:
            raise LedgerIntegrityError(f"unsupported provider: {provider!r}")
        account_key = _validate_nonempty(account_key, field="account_key")
        usage_date = _validate_day(usage_date)
        timezone_name = _validate_nonempty(timezone_name, field="timezone_name")
        model = _validate_nonempty(model, field="model")
        tier = _validate_nonempty(tier, field="tier")
        reason = _validate_nonempty(reason, field="reason")
        decided_at = _validate_timestamp(decided_at or _utc_now(), field="decided_at")
        key_values = (provider, account_key, usage_date, timezone_name, model, tier)
        prior = self.connection.execute(
            """
            SELECT decision_id FROM canonical_decision
             WHERE provider = ? AND account_key = ? AND usage_date = ?
               AND timezone_name = ? AND model = ? AND tier = ?
             ORDER BY decision_seq DESC LIMIT 1
            """,
            key_values,
        ).fetchone()
        if prior is None:
            raise LedgerIntegrityError("cannot withdraw a canonical cell with no prior decision")
        payload = {
            "provider": provider,
            "account_key": account_key,
            "usage_date": usage_date,
            "timezone_name": timezone_name,
            "model": model,
            "tier": tier,
            "action": "withdraw",
            "observation_id": None,
            "reason": reason,
            "decided_at": decided_at,
            "prior_decision_id": prior["decision_id"],
        }
        decision_id = _digest(payload)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO canonical_decision(
                    decision_id, provider, account_key, usage_date, timezone_name,
                    model, tier, action, observation_id, reason, decided_at,
                    prior_decision_id, decision_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    provider,
                    account_key,
                    usage_date,
                    timezone_name,
                    model,
                    tier,
                    "withdraw",
                    None,
                    reason,
                    decided_at,
                    prior["decision_id"],
                    decision_id,
                ),
            )
            self._append_commit(
                connection,
                entity_kind="decision",
                entity_id=decision_id,
                payload={"decision": payload, "decision_digest": decision_id},
                committed_at=decided_at,
            )
        return decision_id

    def record_billing_line_item(self, item: BillingLineItem) -> tuple[str, bool]:

        normalized = item.normalized()
        if normalized["source_id"] is not None:
            source = self.connection.execute(
                "SELECT provider, account_key FROM source_instance WHERE source_id = ?",
                (normalized["source_id"],),
            ).fetchone()
            if source is None:
                raise LedgerIntegrityError("billing line item references an unknown source")
            if (source["provider"], source["account_key"]) != (
                normalized["provider"],
                normalized["account_key"],
            ):
                raise LedgerIntegrityError("billing source provider/account mismatch")
        billing_item_id = _digest(normalized)
        existing = self.connection.execute(
            "SELECT item_digest FROM billing_line_item WHERE billing_item_id = ?",
            (billing_item_id,),
        ).fetchone()
        if existing:
            return billing_item_id, False
        item_digest = _digest(normalized)
        with self._transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO billing_line_item(
                        billing_item_id, provider, account_key, provider_item_key,
                        occurred_at, currency, amount_nanos, category, artifact_digest,
                        source_id, description, item_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        billing_item_id,
                        normalized["provider"],
                        normalized["account_key"],
                        normalized["provider_item_key"],
                        normalized["occurred_at"],
                        normalized["currency"],
                        normalized["amount_nanos"],
                        normalized["category"],
                        normalized["artifact_digest"],
                        normalized["source_id"],
                        normalized["description"],
                        item_digest,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerIntegrityError(
                    "provider billing item key conflicts with a different immutable item"
                ) from exc
            self._append_commit(
                connection,
                entity_kind="billing",
                entity_id=billing_item_id,
                payload={"item": normalized, "item_digest": item_digest},
                committed_at=normalized["occurred_at"],
            )
        return billing_item_id, True

    def provider_billed_totals(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT provider, account_key, currency, SUM(amount_nanos) AS amount_nanos
                  FROM billing_line_item
                 GROUP BY provider, account_key, currency
                 ORDER BY provider, account_key, currency
                """
            )
        ]

    def corrected_rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM daily_corrected ORDER BY provider, account_key, usage_date, model, tier"
        )]

    def custody_high_water_metrics(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM daily_custody_high_water_metric "
            "ORDER BY provider, account_key, usage_date, model, tier, metric"
        )]

    def custody_envelope_totals(self) -> dict[str, Any]:
        rows = self.connection.execute(
            """
            SELECT provider, account_key, metric, SUM(value) AS value
              FROM daily_custody_high_water_metric
             GROUP BY provider, account_key, metric
             ORDER BY provider, account_key, metric
            """
        ).fetchall()
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (row["provider"], row["account_key"])
            item = grouped.setdefault(
                key,
                {
                    "provider": row["provider"],
                    "account_key": row["account_key"],
                    "metric_totals": {},
                },
            )
            item["metric_totals"][row["metric"]] = row["value"]
        for item in grouped.values():
            item["all_usage_tokens"] = sum(
                item["metric_totals"].get(metric, 0) for metric in TOKEN_METRICS
            )
            item["input_output_tokens"] = sum(
                item["metric_totals"].get(metric, 0)
                for metric in ("input_tokens", "output_tokens")
            )
            item["cache_tokens"] = item["all_usage_tokens"] - item["input_output_tokens"]
        return {
            "semantics": "ever_observed_envelope",
            "coherent_observation": False,
            "warning": (
                "Metric totals retain maxima and origins independently; their combined vector "
                "may never have appeared in one source observation."
            ),
            "token_total_policy": (
                "all_usage_tokens includes input, output, cache-read, and every cache-create bucket; "
                "input_output_tokens is also exposed so providers are never compared under hidden conventions"
            ),
            "groups": list(grouped.values()),
        }

    def lifetime_totals(self, *, semantics: str = "canonical_correctable") -> dict[str, Any]:
        if semantics != "canonical_correctable":
            raise LedgerIntegrityError(
                "lifetime_totals only accepts canonical_correctable; query custody metrics separately"
            )
        sums = ", ".join(f"COALESCE(SUM({metric}), 0) AS {metric}" for metric in ALL_METRICS)
        rows = self.connection.execute(
            f"SELECT provider, account_key, {sums} FROM daily_corrected "
            "GROUP BY provider, account_key ORDER BY provider, account_key"
        ).fetchall()
        groups: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["all_usage_tokens"] = sum(item[metric] for metric in TOKEN_METRICS)
            item["input_output_tokens"] = item["input_tokens"] + item["output_tokens"]
            item["cache_tokens"] = item["all_usage_tokens"] - item["input_output_tokens"]
            item["total_tokens"] = item["all_usage_tokens"]
            groups.append(item)
        total_tokens = sum(row["total_tokens"] for row in groups)
        return {
            "semantics": semantics,
            "token_total_policy": "total_tokens equals all_usage_tokens across all explicit buckets",
            "groups": groups,
            "total_tokens": total_tokens,
            "provider_native_cost_nanos": sum(
                row["provider_native_cost_nanos"] for row in groups
            ),
            "api_rate_estimate_nanos": sum(row["api_rate_estimate_nanos"] for row in groups),
        }

    def build_anchor(self, *, created_at: str | None = None) -> dict[str, Any]:

        verification = self.verify_integrity()
        if verification["status"] != "PASS":
            raise LedgerIntegrityError("cannot anchor a ledger that fails integrity verification")
        head = self.connection.execute(
            "SELECT commit_seq, commit_hash, committed_at FROM ledger_commit "
            "ORDER BY commit_seq DESC LIMIT 1"
        ).fetchone()
        if head is None:
            raise LedgerIntegrityError("cannot anchor an empty ledger")
        entity_roots = self._entity_roots_from_commits(max_commit_seq=head["commit_seq"])
        payload = {
            "schema": "coordharness.usage-v2.external-anchor.v1",
            "ledger_schema": SCHEMA_ID,
            "ledger_uuid": self.connection.execute(
                "SELECT value FROM ledger_meta WHERE key = 'ledger_uuid'"
            ).fetchone()[0],
            "commit_seq": head["commit_seq"],
            "commit_hash": head["commit_hash"],
            "entity_roots": entity_roots,
            "created_at": _validate_timestamp(
                created_at or head["committed_at"], field="created_at"
            ),
            "custody_requirement": (
                "Retain this exact anchor in an independent versioned or signed location; "
                "the local database alone is not an independent anchor."
            ),
        }
        return {**payload, "anchor_digest": _digest(payload)}

    def write_anchor(self, directory: Path, *, created_at: str | None = None) -> Path:

        anchor = self.build_anchor(created_at=created_at)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (
            f"{int(anchor['commit_seq']):012d}-{str(anchor['anchor_digest'])[:16]}.json"
        )
        raw = json.dumps(anchor, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.read_bytes() == raw:
                return path
            raise LedgerIntegrityError(f"anchor path collision with different bytes: {path}")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return path

    def verify_anchor(self, path: Path) -> dict[str, Any]:
        failures: list[dict[str, Any]] = []
        try:
            anchor = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "status": "FAIL",
                "failures": [{"code": "ANCHOR_UNREADABLE", "detail": str(exc)}],
            }
        if not isinstance(anchor, dict):
            return {"status": "FAIL", "failures": [{"code": "ANCHOR_INVALID_SHAPE"}]}
        supplied_digest = anchor.pop("anchor_digest", None)
        if supplied_digest != _digest(anchor):
            failures.append({"code": "ANCHOR_DIGEST_MISMATCH"})
        ledger_uuid = self.connection.execute(
            "SELECT value FROM ledger_meta WHERE key = 'ledger_uuid'"
        ).fetchone()[0]
        if anchor.get("ledger_uuid") != ledger_uuid:
            failures.append({"code": "ANCHOR_LEDGER_UUID_MISMATCH"})
        commit = self.connection.execute(
            "SELECT commit_hash FROM ledger_commit WHERE commit_seq = ?",
            (anchor.get("commit_seq"),),
        ).fetchone()
        if commit is None:
            failures.append({"code": "ANCHOR_COMMIT_MISSING_OR_ROLLBACK"})
        elif commit["commit_hash"] != anchor.get("commit_hash"):
            failures.append({"code": "ANCHOR_COMMIT_HASH_MISMATCH"})
        if isinstance(anchor.get("commit_seq"), int):
            recomputed_roots = self._entity_roots_from_commits(
                max_commit_seq=anchor["commit_seq"]
            )
            if anchor.get("entity_roots") != recomputed_roots:
                failures.append({"code": "ANCHOR_ENTITY_ROOT_MISMATCH"})
        integrity = self.verify_integrity()
        if integrity["status"] != "PASS":
            failures.append(
                {
                    "code": "ANCHOR_LEDGER_INTEGRITY_FAILURE",
                    "ledger_failures": integrity["failures"],
                }
            )
        return {
            "status": "PASS" if not failures else "FAIL",
            "failures": failures,
            "anchor_digest": supplied_digest,
            "commit_seq": anchor.get("commit_seq"),
        }

    def _entity_roots_from_commits(self, *, max_commit_seq: int) -> dict[str, str]:
        entities: dict[str, list[list[str]]] = {
            "sources": [],
            "observations": [],
            "coverage": [],
            "decisions": [],
            "billing": [],
        }
        rows = self.connection.execute(
            "SELECT commit_seq, entity_kind, entity_id, payload_json FROM ledger_commit "
            "WHERE commit_seq <= ? ORDER BY commit_seq",
            (max_commit_seq,),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                payload = {}
            kind = row["entity_kind"]
            if kind == "source":
                entities["sources"].append(
                    [row["entity_id"], str(payload.get("record_digest"))]
                )
            elif kind == "observation":
                entities["observations"].append(
                    [row["entity_id"], str(payload.get("payload_digest"))]
                )
                metadata = payload.get("metadata") if isinstance(payload, dict) else None
                if isinstance(metadata, dict):
                    coverage = {
                        key: metadata.get(key)
                        for key in (
                            "coverage_start", "coverage_end", "source_manifest_digest",
                            "cursor_before", "cursor_after", "files_scanned", "records_scanned",
                            "records_accepted", "records_rejected", "records_conflicted",
                            "parse_error_count"
                        )
                    }
                    entities["coverage"].append([row["entity_id"], _digest(coverage)])
            elif kind == "decision":
                entities["decisions"].append(
                    [row["entity_id"], str(payload.get("decision_digest"))]
                )
            elif kind == "billing":
                entities["billing"].append(
                    [row["entity_id"], str(payload.get("item_digest"))]
                )
        return {
            name: _digest(sorted(values, key=lambda value: value[0]))
            for name, values in entities.items()
        }

    def verify_integrity(self) -> dict[str, Any]:
        failures: list[dict[str, Any]] = []
        failures.extend(self._schema_shape_failures())
        integrity = self.connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            failures.append({"code": "SQLITE_INTEGRITY", "detail": integrity})

        prior_hash: str | None = None
        commit_payloads: dict[tuple[str, str], dict[str, Any]] = {}
        commits = self.connection.execute(
            "SELECT * FROM ledger_commit ORDER BY commit_seq"
        ).fetchall()
        for row in commits:
            try:
                decoded_payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                decoded_payload = {}
                failures.append(
                    {"code": "COMMIT_PAYLOAD_INVALID", "commit_seq": row["commit_seq"]}
                )
            commit_payloads[(row["entity_kind"], row["entity_id"])] = decoded_payload
            payload_digest = hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest()
            expected = _digest(
                {
                    "entity_kind": row["entity_kind"],
                    "entity_id": row["entity_id"],
                    "committed_at": row["committed_at"],
                    "payload_digest": payload_digest,
                    "previous_commit_hash": prior_hash,
                }
            )
            if row["payload_digest"] != payload_digest or row["commit_hash"] != expected:
                failures.append(
                    {"code": "COMMIT_HASH_MISMATCH", "commit_seq": row["commit_seq"]}
                )
            if row["previous_commit_hash"] != prior_hash:
                failures.append(
                    {"code": "COMMIT_CHAIN_BREAK", "commit_seq": row["commit_seq"]}
                )
            prior_hash = expected

        for row in self.connection.execute("SELECT * FROM source_instance"):
            record = {
                key: row[key]
                for key in (
                    "provider",
                    "account_key",
                    "source_kind",
                    "root_uri",
                    "timezone_name",
                    "root_identity_digest",
                    "label",
                )
            }
            if row["record_digest"] != _digest(record):
                failures.append({"code": "SOURCE_DIGEST_MISMATCH", "source_id": row["source_id"]})
            commit_payload = commit_payloads.get(("source", row["source_id"]))
            if commit_payload != {"record": record, "record_digest": row["record_digest"]}:
                failures.append({"code": "SOURCE_COMMIT_MISMATCH", "source_id": row["source_id"]})

        for row in self.connection.execute("SELECT * FROM source_occurrence"):
            payload = {
                "source_id": row["source_id"],
                "artifact_digest": row["artifact_digest"],
                "artifact_uri": row["artifact_uri"],
            }
            if row["occurrence_id"] != _digest(payload) or row["occurrence_digest"] != _digest(payload):
                failures.append(
                    {"code": "SOURCE_OCCURRENCE_DIGEST_MISMATCH", "occurrence_id": row["occurrence_id"]}
                )

        for observation in self.connection.execute("SELECT * FROM observation"):
            metadata = json.loads(observation["metadata_json"])
            row_payload: list[dict[str, Any]] = []
            for row in self.connection.execute(
                "SELECT * FROM daily_observation WHERE observation_id = ? "
                "ORDER BY usage_date, model, tier",
                (observation["observation_id"],),
            ):
                payload = {key: row[key] for key in (
                    "usage_date", "model", "tier", *TOKEN_METRICS, "request_count", *COST_METRICS
                )}
                if row["row_digest"] != _digest(payload):
                    failures.append(
                        {
                            "code": "DAILY_ROW_DIGEST_MISMATCH",
                            "observation_id": observation["observation_id"],
                            "usage_date": row["usage_date"],
                            "model": row["model"],
                            "tier": row["tier"],
                        }
                    )
                row_payload.append(payload)
            payload_digest = _digest(row_payload)
            expected_id = _digest(
                {
                    "source_id": observation["source_id"],
                    "identity_metadata": _observation_identity_metadata(metadata),
                    "payload_digest": payload_digest,
                }
            )
            if observation["payload_digest"] != payload_digest or expected_id != observation["observation_id"]:
                failures.append(
                    {
                        "code": "OBSERVATION_DIGEST_MISMATCH",
                        "observation_id": observation["observation_id"],
                    }
                )
            coverage = self.connection.execute(
                "SELECT * FROM coverage_interval WHERE observation_id = ?",
                (observation["observation_id"],),
            ).fetchone()
            if coverage is None:
                failures.append(
                    {"code": "COVERAGE_MISSING", "observation_id": observation["observation_id"]}
                )
            else:
                coverage_payload = {
                    key: coverage[key]
                    for key in (
                        "coverage_start", "coverage_end", "source_manifest_digest",
                        "cursor_before", "cursor_after", "files_scanned", "records_scanned",
                        "records_accepted", "records_rejected", "records_conflicted",
                        "parse_error_count"
                    )
                }
                if coverage["coverage_digest"] != _digest(coverage_payload):
                    failures.append(
                        {"code": "COVERAGE_DIGEST_MISMATCH", "observation_id": observation["observation_id"]}
                    )
                if any(metadata[key] != coverage_payload[key] for key in coverage_payload):
                    failures.append(
                        {"code": "COVERAGE_METADATA_MISMATCH", "observation_id": observation["observation_id"]}
                    )
            commit_payload = commit_payloads.get(("observation", observation["observation_id"]))
            if not commit_payload or (
                commit_payload.get("source_id") != observation["source_id"]
                or commit_payload.get("identity_metadata")
                != _observation_identity_metadata(metadata)
                or commit_payload.get("metadata") != metadata
                or commit_payload.get("payload_digest") != payload_digest
            ):
                failures.append(
                    {"code": "OBSERVATION_COMMIT_MISMATCH", "observation_id": observation["observation_id"]}
                )

        for row in self.connection.execute("SELECT * FROM canonical_decision"):
            payload = {
                key: row[key]
                for key in (
                    "provider", "account_key", "usage_date", "timezone_name", "model", "tier",
                    "action", "observation_id", "reason", "decided_at", "prior_decision_id"
                )
            }
            expected = _digest(payload)
            if row["decision_digest"] != expected or row["decision_id"] != expected:
                failures.append(
                    {"code": "DECISION_DIGEST_MISMATCH", "decision_id": row["decision_id"]}
                )
            commit_payload = commit_payloads.get(("decision", row["decision_id"]))
            if commit_payload != {"decision": payload, "decision_digest": expected}:
                failures.append(
                    {"code": "DECISION_COMMIT_MISMATCH", "decision_id": row["decision_id"]}
                )

        for row in self.connection.execute("SELECT * FROM billing_line_item"):
            payload = {
                key: row[key]
                for key in (
                    "provider", "account_key", "provider_item_key", "occurred_at",
                    "currency", "amount_nanos", "category", "artifact_digest",
                    "source_id", "description"
                )
            }
            expected = _digest(payload)
            if row["billing_item_id"] != expected or row["item_digest"] != expected:
                failures.append(
                    {"code": "BILLING_DIGEST_MISMATCH", "billing_item_id": row["billing_item_id"]}
                )
            commit_payload = commit_payloads.get(("billing", row["billing_item_id"]))
            if commit_payload != {"item": payload, "item_digest": expected}:
                failures.append(
                    {"code": "BILLING_COMMIT_MISMATCH", "billing_item_id": row["billing_item_id"]}
                )

        for kind, table, id_column in (
            ("source", "source_instance", "source_id"),
            ("observation", "observation", "observation_id"),
            ("decision", "canonical_decision", "decision_id"),
            ("billing", "billing_line_item", "billing_item_id"),
        ):
            missing = self.connection.execute(
                f"SELECT {id_column} FROM {table} EXCEPT "
                "SELECT entity_id FROM ledger_commit WHERE entity_kind = ?",
                (kind,),
            ).fetchall()
            failures.extend(
                {"code": "MISSING_COMMIT", "entity_kind": kind, "entity_id": row[0]}
                for row in missing
            )
            orphaned = self.connection.execute(
                "SELECT entity_id FROM ledger_commit WHERE entity_kind = ? EXCEPT "
                f"SELECT {id_column} FROM {table}",
                (kind,),
            ).fetchall()
            failures.extend(
                {"code": "ORPHAN_COMMIT", "entity_kind": kind, "entity_id": row[0]}
                for row in orphaned
            )

        return {
            "schema": SCHEMA_ID,
            "status": "PASS" if not failures else "FAIL",
            "failures": failures,
            "counts": {
                "sources": self.connection.execute(
                    "SELECT COUNT(*) FROM source_instance"
                ).fetchone()[0],
                "observations": self.connection.execute(
                    "SELECT COUNT(*) FROM observation"
                ).fetchone()[0],
                "daily_rows": self.connection.execute(
                    "SELECT COUNT(*) FROM daily_observation"
                ).fetchone()[0],
                "decisions": self.connection.execute(
                    "SELECT COUNT(*) FROM canonical_decision"
                ).fetchone()[0],
                "commits": len(commits),
                "billing_items": self.connection.execute(
                    "SELECT COUNT(*) FROM billing_line_item"
                ).fetchone()[0],
            },
            "head_commit_hash": prior_hash,
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def utc_timestamp() -> str:
    return _utc_now()
