"""Lossless, idempotent imports of independently audited receipt maxima.

This is an explicit maintenance operation. It never clears source ownership,
rebuilds a provider, guesses missing identities, or adds legacy-covered volume.
Callers hold the store's normal scan lock and retain consistent SQLite backups.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from .local_history import ParsedRecord, root_identity_digest, slot_day, utc_slot_day
from .scan_store import UsageScanStore, ScanStoreError, _TOKEN_COLUMNS


@dataclass(frozen=True)
class CanonicalReceipt:
    fingerprint: int
    usage_slot: int
    model: str
    metrics: Mapping[str, int]
    alias_fingerprints: tuple[int, ...] = ()
    pricing_metadata: Mapping[str, str] | None = None
    # Only an independently audited Codex native receipt can opt in. Claude
    # wrappers preserve response identity under later observation timestamps.
    rebase_attribution: bool = False


@dataclass
class ReconciliationResult:
    examined: int = 0
    inserted: int = 0
    raised: int = 0
    added_tokens: int = 0
    metadata_observed: int = 0
    model_conflicts_preserved: int = 0
    slot_conflicts_preserved: int = 0
    legacy_covered_requests: int = 0
    legacy_covered_tokens: int = 0
    added_by_utc_day_model: dict[tuple[str, str], int] = field(default_factory=dict)
    rebased: int = 0
    rebased_tokens: int = 0
    attribution_token_delta_by_utc_day_model: dict[tuple[str, str], int] = field(default_factory=dict)
    attribution_request_delta_by_utc_day_model: dict[tuple[str, str], int] = field(default_factory=dict)


def reconcile_receipts(
    store: UsageScanStore,
    receipts: Iterable[CanonicalReceipt],
    *,
    provider: str,
    root: Path,
    source_digest: str,
    legacy_local_zone: str = "America/New_York",
) -> ReconciliationResult:
    """Merge audited identities into retained history, in one transaction.

    Every count is a component-wise maximum over the retained and audited
    observation. Modern/legacy aliases resolve to one existing representative.
    Model/date disagreements are counted and preserved unless a same-model
    Codex native timestamp has been explicitly qualified by the caller's audit.
    New identities on a frozen legacy date are withheld as already covered;
    measured data must never silently erase a larger frozen daily population.
    """
    if store._read_only:
        raise ScanStoreError("reconciliation needs a writable scan store")
    if provider not in {"claude", "codex"} or not re.fullmatch(r"[0-9a-f]{64}", source_digest):
        raise ValueError("provider and immutable audit digest are required")
    zone = ZoneInfo(legacy_local_zone)
    result = ReconciliationResult()
    conn = store.connection
    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    source_key = f"usage-reconciliation:{provider}:{source_digest}"
    conn.execute("BEGIN IMMEDIATE")
    try:
        legacy_days = {row[0] for row in conn.execute(
            "SELECT DISTINCT usage_date FROM legacy_daily WHERE provider=?", (provider,)
        )}
        conn.execute(
            "CREATE TABLE IF NOT EXISTS usage_reconciliation_run("
            "provider TEXT NOT NULL, source_digest TEXT NOT NULL, applied_at TEXT NOT NULL,"
            "examined INTEGER NOT NULL, inserted INTEGER NOT NULL, raised INTEGER NOT NULL,"
            "added_tokens INTEGER NOT NULL,legacy_covered_tokens INTEGER NOT NULL,"
            "PRIMARY KEY(provider,source_digest,applied_at)) STRICT"
        )
        file_id = conn.execute(
            "INSERT INTO scanned_file(path,provider,root_digest,size,mtime_ns,scanned_at,"
            "records_scanned,records_accepted,records_rejected,records_deduplicated,records_unidentified,parse_error_count) "
            "VALUES(?,?,?,0,0,?,0,0,0,0,0,0) ON CONFLICT(path) DO UPDATE SET scanned_at=excluded.scanned_at "
            "RETURNING file_id", (source_key,provider,root_identity_digest(root),stamp)
        ).fetchone()[0]
        increments: dict[tuple[int, int, str], list[int]] = {}
        for receipt in receipts:
            if type(receipt.fingerprint) is not int or not -(2**63) <= receipt.fingerprint < 2**63:
                raise ValueError("invalid receipt fingerprint")
            if type(receipt.usage_slot) is not int or receipt.usage_slot < 0 or receipt.usage_slot % 900:
                raise ValueError("invalid receipt UTC slot")
            if not isinstance(receipt.model,str) or not receipt.model or len(receipt.model)>200:
                raise ValueError("invalid model label")
            values = tuple(receipt.metrics.get(col,0) for col in _TOKEN_COLUMNS)
            if any(type(value) is not int or value < 0 or value >= 2**63 for value in values):
                raise ValueError("invalid canonical token count")
            if any(type(value) is not int or not -(2**63)<=value<2**63 for value in receipt.alias_fingerprints):
                raise ValueError("invalid alias fingerprint")
            if type(receipt.rebase_attribution) is not bool:
                raise ValueError("rebase_attribution must be an explicit boolean")
            if receipt.rebase_attribution and provider != "codex":
                raise ScanStoreError("only audited Codex native receipts can rebase attribution")
            result.examined += 1
            fingerprint = store._resolve_fingerprints(conn,provider,receipt.fingerprint,receipt.alias_fingerprints)
            held = conn.execute("SELECT * FROM seen_message WHERE fingerprint=?", (fingerprint,)).fetchone()
            if held is None and (utc_slot_day(receipt.usage_slot) in legacy_days or
                                 (provider=="claude" and slot_day(receipt.usage_slot,zone) in legacy_days)):
                result.legacy_covered_requests += 1
                result.legacy_covered_tokens += sum(values)
                continue
            if held is None:
                conn.execute(
                    "INSERT INTO seen_message(fingerprint,provider,file_id,representative,usage_slot,model,"
                    + ','.join(_TOKEN_COLUMNS) + ") VALUES(?,?,?,1,?,?," + ','.join('?'*len(_TOKEN_COLUMNS)) + ")",
                    (fingerprint,provider,file_id,receipt.usage_slot,receipt.model,*values),
                )
                owner,slot,model=file_id,receipt.usage_slot,receipt.model
                deltas=values
                new_request=1
                result.inserted += 1
            else:
                if held["provider"]!=provider or not held["representative"]:
                    raise ScanStoreError("historical representative lacks safe ownership or token baseline")
                owner,slot,model=held["file_id"],held["usage_slot"],held["model"]
                if receipt.rebase_attribution and model != receipt.model:
                    raise ScanStoreError("attribution rebase cannot change the retained model")
                result.model_conflicts_preserved += int(model!=receipt.model)
                if slot!=receipt.usage_slot and receipt.rebase_attribution:
                    old_slot=slot
                    old_key=(owner,old_slot,model)
                    old_row=conn.execute(
                        "SELECT " + ','.join((*_TOKEN_COLUMNS,"request_count"))
                        + " FROM file_slot WHERE file_id=? AND usage_slot=? AND model=?", old_key
                    ).fetchone()
                    pending=increments.get(old_key,[0]*(len(_TOKEN_COLUMNS)+1))
                    retained=tuple(held[col] for col in _TOKEN_COLUMNS)
                    # Include earlier changes in this transaction, so multiple
                    # audited receipts can safely share an aggregate bucket.
                    available=tuple((old_row[index] if old_row is not None else 0)+pending[index]
                                    for index in range(len(_TOKEN_COLUMNS)+1))
                    movement=(*retained,1)
                    if any(value < 0 or available[index] < value for index,value in enumerate(movement)):
                        raise ScanStoreError("retained receipt cannot be subtracted exactly from its owner bucket")
                    old_bucket=increments.setdefault(old_key,[0]*(len(_TOKEN_COLUMNS)+1))
                    new_bucket=increments.setdefault((owner,receipt.usage_slot,model),[0]*(len(_TOKEN_COLUMNS)+1))
                    for index,value in enumerate(movement):
                        old_bucket[index]-=value
                        new_bucket[index]+=value
                    slot=receipt.usage_slot
                    conn.execute("UPDATE seen_message SET usage_slot=? WHERE fingerprint=?",(slot,fingerprint))
                    result.rebased+=1
                    volume=sum(retained)
                    result.rebased_tokens+=volume
                    for day,sign in ((utc_slot_day(old_slot),-1),(utc_slot_day(slot),1)):
                        day_key=(day,model)
                        result.attribution_token_delta_by_utc_day_model[day_key]=(
                            result.attribution_token_delta_by_utc_day_model.get(day_key,0)+sign*volume)
                        result.attribution_request_delta_by_utc_day_model[day_key]=(
                            result.attribution_request_delta_by_utc_day_model.get(day_key,0)+sign)
                else:
                    result.slot_conflicts_preserved += int(slot!=receipt.usage_slot)
                deltas=tuple(max(0,value-held[col]) for value,col in zip(values,_TOKEN_COLUMNS))
                new_request=0
                if any(deltas):
                    result.raised += 1
                    conn.execute("UPDATE seen_message SET " + ','.join(f"{col}={col}+?" for col in _TOKEN_COLUMNS)
                                 + " WHERE fingerprint=?", (*deltas,fingerprint))
            if receipt.pricing_metadata:
                store._record_pricing_metadata(conn,fingerprint,ParsedRecord(
                    usage_slot=receipt.usage_slot,model=receipt.model,metrics=receipt.metrics,
                    identity=None,pricing_metadata=receipt.pricing_metadata,
                ))
                result.metadata_observed += 1
            if any(deltas) or new_request:
                bucket=increments.setdefault((owner,slot,model),[0]*(len(_TOKEN_COLUMNS)+1))
                for index,delta in enumerate(deltas): bucket[index]+=delta
                bucket[-1]+=new_request
                addition=sum(deltas)
                result.added_tokens+=addition
                key=(utc_slot_day(slot),model)
                result.added_by_utc_day_model[key]=result.added_by_utc_day_model.get(key,0)+addition
        conn.executemany(
            "INSERT INTO file_slot(file_id,usage_slot,model," + ','.join(_TOKEN_COLUMNS) + ",request_count) "
            "VALUES(?,?,?," + ','.join('?'*(len(_TOKEN_COLUMNS)+1)) + ") "
            "ON CONFLICT(file_id,usage_slot,model) DO UPDATE SET "
            + ','.join(f"{col}={col}+excluded.{col}" for col in (*_TOKEN_COLUMNS,"request_count")),
            [(owner,slot,model,*amounts) for (owner,slot,model),amounts in increments.items()],
        )
        # Empty source buckets must not continue suppressing a frozen legacy
        # day after its final receipt has moved to a qualified native slot.
        conn.executemany(
            "DELETE FROM file_slot WHERE file_id=? AND usage_slot=? AND model=? AND request_count=0 AND "
            + ' AND '.join(f"{col}=0" for col in _TOKEN_COLUMNS), list(increments)
        )
        owned=conn.execute("SELECT COUNT(*) FROM seen_message WHERE file_id=?", (file_id,)).fetchone()[0]
        conn.execute("UPDATE scanned_file SET records_scanned=?,records_accepted=? WHERE file_id=?",
                     (result.examined,owned,file_id))
        conn.execute("INSERT INTO usage_reconciliation_run VALUES(?,?,?,?,?,?,?,?)",
                     (provider,source_digest,stamp,result.examined,result.inserted,result.raised,
                      result.added_tokens,result.legacy_covered_tokens))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return result
