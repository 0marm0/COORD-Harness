"""Bounded read-only import of current-user Claude and Codex CLI histories."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
import hashlib
import heapq
import json
from pathlib import Path
import re
from typing import Any, Final, Iterator, Mapping

from .ledger import DailyUsage

# A transcript line is one JSON object; anything this long is not one, and
# decoding it would cost more than the record could possibly be worth.
_MAX_LINE_BYTES = 2 * 1024 * 1024
_SCAN_SUBDIRECTORY: Final = {"claude": "projects", "codex": "sessions"}

# DAY BASIS. Usage is bucketed into fixed UTC time slots, never into calendar
# days, and a day is derived from a slot only when the history is served.
#
# WHY: a calendar day is a function of the reader's timezone, and a store that
# fixed it at scan time mixed zones the moment the machine's timezone changed:
# files scanned before the change stayed bucketed in the old zone beside
# everything scanned after it in the new one, and "today" in the new zone was
# under-reported by a large share of the day's usage. A slot is
# an instant, so a scan under any timezone writes the identical store, a
# timezone change needs no rescan, and the same slots serve both the local days
# a person reads and the UTC days a provider reports (OpenAI's daily buckets).
#
# WHY 15 MINUTES rather than an hour: a slot maps to exactly one local day only
# when every local midnight falls on a slot boundary, i.e. when the zone's UTC
# offset is a whole multiple of the slot width. Hourly slots are exact for
# whole-hour zones but misfile up to 30 or 45 minutes of usage per day in
# +05:30 (India), +05:45 (Nepal), +09:30 (Adelaide) or +12:45 (Chatham). Every
# offset in the current tz database -- and every DST transition, which also
# lands on a quarter hour -- is a multiple of 15 minutes, so this width is
# EXACT for all of them. The price is measured, not guessed: on this machine's
# Codex corpus it is 11,212 aggregate rows against 3,855 per-day rows, a few
# megabytes beside the replay-identity table that dominates the store. A zone
# whose offset is not a multiple of 15 minutes (none exists today) would misfile
# at most one slot of usage at each end of each day.
SLOT_SECONDS: Final = 900

# The model name a record carries when nothing in the transcript names one.
# Deliberately not priceable: a missing price must read as unknown rather than
# as free, and this label is what makes it visible in the unpriced counters.
UNKNOWN_MODEL: Final = "unknown"

# A SECOND Claude transcript tree, written by the desktop app's agent mode
# rather than by the CLI. Beneath it, 631 of 698 JSONL files are ordinary
# Claude Code transcripts at
# `local_<uuid>/.claude/projects/<project>/<uuid>.jsonl`; the other 57 are
# `audit.jsonl` sidecars that carry no usage. It overlaps `~/.claude/projects`
# by design -- a session started in the desktop app and resumed in the CLI
# appears in both -- so the replay identity, not the directory, is what keeps
# a message from being counted twice.
_CLAUDE_DESKTOP_SESSIONS: Final = (
    "Library",
    "Application Support",
    "Claude",
    "local-agent-mode-sessions",
)

# Codex names a rollout `rollout-<ISO timestamp>-<thread uuid>.jsonl`, and a
# resumed one `rollout-<ISO>-<original uuid>_<new uuid>.jsonl`. The FIRST uuid
# is the one the file's session_meta header also reports as `payload.id`, so
# taking the first keeps the fallback and the header in agreement.
_CODEX_ROLLOUT_NAME = re.compile(
    r"-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def _thread_id_from_filename(path: Path) -> str | None:
    """Recover a Codex thread id from the rollout filename.

    Only a fallback: the session_meta header is authoritative and was present
    in all 3,721 rollout files measured here. This covers a file whose header
    is missing, truncated, or too long to decode, so such a file still gets an
    identity per record rather than counting as 300k identity failures.
    """

    match = _CODEX_ROLLOUT_NAME.search(path.name)
    return match.group(1) if match is not None else None


TOKEN_COLUMNS: Final = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_create_5m_tokens",
    "cache_create_1h_tokens",
    "cache_create_other_tokens",
)


@dataclass(frozen=True)
class SlotUsage:
    """One (UTC time slot, model) token aggregate: the unit the store keeps.

    ``usage_slot`` is the slot's start as UTC epoch seconds, a multiple of
    ``SLOT_SECONDS``. It is an instant, not a day, so it means the same thing
    to every reader regardless of the timezone it was scanned or is read in.
    """

    usage_slot: int
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_create_5m_tokens: int = 0
    cache_create_1h_tokens: int = 0
    cache_create_other_tokens: int = 0
    request_count: int | None = None

    @property
    def total_tokens(self) -> int:
        return sum(getattr(self, column) for column in TOKEN_COLUMNS)


def slot_moment(slot: int) -> datetime:
    """The UTC instant a slot starts at."""

    return datetime.fromtimestamp(slot, tz=timezone.utc)


def slot_day(slot: int, tz: tzinfo | None = None) -> str:
    """The calendar day a slot falls in, in ``tz`` (the system zone when None).

    ``None`` resolves the zone the process is running in NOW, per call, rather
    than one captured at import, so a long-running reader follows a timezone
    change as soon as its libc rules are reset (``time.tzset``).
    """

    moment = slot_moment(slot)
    return (moment.astimezone(tz) if tz is not None else moment.astimezone()).date().isoformat()


def utc_slot_day(slot: int) -> str:
    """The UTC calendar day a slot falls in -- the basis provider buckets use."""

    return slot_day(slot, timezone.utc)


def rollup_days(rows: Iterable[SlotUsage], tz: tzinfo | None = None) -> tuple[DailyUsage, ...]:
    """Fold slot rows into per-(day, model) rows for the day basis ``tz``.

    Derived on every read rather than stored, which is the whole point: the
    same slots answer New York days, Berlin days and UTC days exactly.
    """

    days: dict[int, str] = {}
    totals: dict[tuple[str, str], list[int]] = {}
    requests: dict[tuple[str, str], int | None] = {}
    for row in rows:
        day = days.get(row.usage_slot)
        if day is None:
            day = days[row.usage_slot] = slot_day(row.usage_slot, tz)
        key = (day, row.model)
        bucket = totals.setdefault(key, [0] * len(TOKEN_COLUMNS))
        for index, column in enumerate(TOKEN_COLUMNS):
            bucket[index] += getattr(row, column)
        if row.request_count is not None:
            requests[key] = (requests.get(key) or 0) + row.request_count
        else:
            requests.setdefault(key, None)
    return tuple(
        DailyUsage(
            usage_date=day,
            model=model,
            request_count=requests.get((day, model)),
            **dict(zip(TOKEN_COLUMNS, amounts)),
        )
        for (day, model), amounts in sorted(totals.items())
    )


def day_slots(day: str, tz: tzinfo | None = None) -> tuple[int, ...]:
    """Every slot whose start falls on calendar ``day`` in ``tz``.

    Used to spread a quantity known only per day (a provider's daily bucket)
    over time when no measured volume says where in the day it happened.
    """

    zone = tz if tz is not None else datetime.now().astimezone().tzinfo
    start = datetime.fromisoformat(day).replace(tzinfo=zone)
    # Walk a generous window and keep what lands on the day: DST days are 23
    # or 25 hours long, and the arithmetic below stays in UTC throughout.
    first = int(start.astimezone(timezone.utc).timestamp()) // SLOT_SECONDS * SLOT_SECONDS
    window = range(first - 2 * 3600, first + 28 * 3600, SLOT_SECONDS)
    return tuple(slot for slot in window if slot_day(slot, zone) == day)


@dataclass(frozen=True)
class LocalHistoryImport:
    provider: str
    rows: tuple[DailyUsage, ...]
    coverage_state: str
    root_identity_digest: str
    manifest_digest: str
    files_scanned: int
    records_scanned: int
    records_accepted: int
    records_rejected: int
    parse_error_count: int
    records_deduplicated: int = 0
    records_unidentified: int = 0
    # Days that predate this machine's transcripts, carried alongside the
    # self-computed rows rather than inside them. They come from another
    # tool's accounting and must stay separable all the way to the payload;
    # merging them into `rows` would make that impossible one line after this.
    legacy_rows: tuple[DailyUsage, ...] = ()
    legacy_provenance: Mapping[str, Any] | None = None
    # The slots ``rows`` were derived from. ``rows`` answers the one day basis
    # the caller asked for; anything that needs another -- the UTC days a
    # provider reports, or how one UTC day splits across local days -- derives
    # it from these instead of re-reading the store.
    slot_rows: tuple[SlotUsage, ...] = ()
    records_reemitted: int = 0

    def provenance(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "source_kind": f"{self.provider}_cli_jsonl",
            "parser_version": "coord-local-cli-jsonl-v2",
            "day_basis": "utc_15min_slots_rolled_to_reader_local_days",
            "coverage_state": self.coverage_state,
            "canonical": False,
            "root_identity_digest": self.root_identity_digest,
            "manifest_digest": self.manifest_digest,
            "files_scanned": self.files_scanned,
            "records_scanned": self.records_scanned,
            "records_accepted": self.records_accepted,
            "records_rejected": self.records_rejected,
            "parse_error_count": self.parse_error_count,
            "records_deduplicated": self.records_deduplicated,
            "records_unidentified": self.records_unidentified,
            "records_reemitted": self.records_reemitted,
            "legacy": (
                dict(self.legacy_provenance)
                if self.legacy_provenance is not None and self.legacy_rows
                else None
            ),
        }


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _slot(value: object) -> int | None:
    """The UTC slot a record's timestamp falls in; never a calendar day.

    A timestamp without an offset is refused rather than read in the local
    zone: guessing the zone is exactly the scan-time dependence slots remove.
    """

    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    epoch = parsed - datetime(1970, 1, 1, tzinfo=timezone.utc)
    # Integer arithmetic on the timedelta, so a sub-second timestamp a hair
    # before a boundary cannot be rounded across it by float error.
    seconds = epoch // timedelta(seconds=1)
    return seconds - seconds % SLOT_SECONDS


def _codex_running_total(value: Mapping[str, Any]) -> tuple[int, int, int] | None:
    """The thread's cumulative usage a Codex ``token_count`` record reports.

    ``(input, cached input, output)`` from ``info.total_token_usage``, or
    ``None`` when the record states no running total.
    """

    payload = value.get("payload")
    info = payload.get("info") if isinstance(payload, Mapping) else None
    total = (
        (info.get("total_token_usage") or info.get("totalTokenUsage"))
        if isinstance(info, Mapping)
        else None
    )
    if not isinstance(total, Mapping):
        return None
    parts = (
        _count(total.get("input_tokens", total.get("inputTokens"))),
        _count(total.get("cached_input_tokens", total.get("cachedInputTokens"))) or 0,
        _count(total.get("output_tokens", total.get("outputTokens"))),
    )
    if parts[0] is None or parts[2] is None:
        return None
    return (parts[0], parts[1], parts[2])


def _model(value: object, fallback: str = UNKNOWN_MODEL) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if text and len(text) <= 120 and not any(ord(c) < 32 for c in text) else fallback


def _last_usage(value: object, depth: int = 0) -> Mapping[str, Any] | None:
    if depth > 8:
        return None
    if isinstance(value, Mapping):
        found = value.get("last_token_usage") or value.get("lastTokenUsage")
        if isinstance(found, Mapping):
            return found
        for child in value.values():
            found = _last_usage(child, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value[:64]:
            found = _last_usage(child, depth + 1)
            if found is not None:
                return found
    return None


# Where a Codex rollout states the model it is running. `payload.model` is the
# one the parser already followed; the rest are the places the name actually
# appears FIRST in the files that produced an `unknown` model here -- measured
# over ~/.codex/sessions on 2026-09-21: `payload.thread_settings.model` in 58
# of 69 such files and `payload.model` in the other 11.
_CODEX_MODEL_PATHS: Final = (
    ("model",),
    ("thread_settings", "model"),
    ("state", "model"),
    ("collaboration_mode", "settings", "model"),
)

# How far into a rollout the declaration pass will look. Measured over all
# 3,721 rollouts: the first naming is at line 6 for half of them, line 80 at
# the 99th percentile and line 238 at the worst, and exactly one file names no
# model in its first 2,000 lines. A bound well above the worst observed case
# keeps the pass from reading a multi-gigabyte transcript twice while still
# covering every file that has ever named one.
_CODEX_MODEL_PREFIX_LINES: Final = 512


def _dig(payload: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    value: Any = payload
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value if isinstance(value, str) and value.strip() else None


def codex_record_model(value: Mapping[str, Any]) -> str | None:
    """The model name one Codex record states, from any place it states it."""

    payload = value.get("payload")
    if not isinstance(payload, Mapping):
        return None
    for keys in _CODEX_MODEL_PATHS:
        found = _dig(payload, keys)
        if found is not None:
            named = _model(found, "")
            return named or None
    return None


def codex_declared_model(path: Path) -> str | None:
    """Find the model a Codex rollout declares, before parsing it for usage.

    WHY a separate pass over the head of the file: Codex names the model once
    per session, and not necessarily before the first record that carries
    usage. Measured over the 69 rollouts that produced an ``unknown`` model on
    this machine, 56 name the model only AFTER their first usage record --
    431,902,456 tokens' worth -- so a forward-only carry-forward cannot label
    them. An unlabelled model resolves to no rate at all, which makes the
    tokens read as free, and that silent zero is the failure this pass removes.

    The declaration is only a STARTING value: ``payload.model`` still
    overrides it going forward, so a session that switches model mid-run, and
    a Codex-internal label such as ``codex-auto-review``, keep their own name.
    """

    try:
        with path.open("rb") as handle:
            for index, raw in enumerate(handle):
                if index >= _CODEX_MODEL_PREFIX_LINES:
                    return None
                if len(raw) > _MAX_LINE_BYTES:
                    continue
                try:
                    value = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
                    continue
                if not isinstance(value, Mapping):
                    continue
                named = codex_record_model(value)
                if named is not None:
                    return named
    except OSError:
        # The usage pass opens the same file straight after this one and its
        # caller already knows what to do with a transcript that will not
        # read. Failing softly here keeps that one decision in one place.
        return None
    return None


def codex_thread_id(value: Mapping[str, Any]) -> str | None:
    """Read a thread id off a Codex ``session_meta`` record.

    Codex names the thread as ``payload.id`` in the file's opening record, and
    nothing further down repeats it -- in particular the ``token_count``
    records that carry the usage do not -- so a scan has to carry it forward
    the way it already carries the model name.

    Only the FIRST such record in a file may be used; see ``iter_history_records``.
    """

    if value.get("type") != "session_meta":
        return None
    payload = value.get("payload")
    if not isinstance(payload, Mapping):
        return None
    candidate = payload.get("id")
    return candidate if isinstance(candidate, str) and 0 < len(candidate) <= 200 else None


def record_identity(
    provider: str,
    value: Mapping[str, Any],
    message: object,
    *,
    thread_id: str | None = None,
) -> str | None:
    """Return a stable per-assistant-message identity, when the record carries one.

    A resumed or forked CLI session replays earlier turns verbatim into its own
    transcript, so the same billable message appears in many files. Summing
    every occurrence overstates usage by the replay factor, which is why this
    identity is required rather than advisory.

    The two CLIs put that identity in different places. Claude stamps every
    assistant message with its own id, so a replayed message keeps it and
    dedup works across files. Codex stamps nothing on the usage record itself;
    the identity has to be composed from the thread id in the file's
    ``session_meta`` header and the record's position in that thread. The
    comment below this function records what that identity can and cannot do.
    """

    if provider == "claude":
        if isinstance(message, Mapping):
            for name in ("id", "uuid"):
                candidate = message.get(name)
                if isinstance(candidate, str) and 0 < len(candidate) <= 200:
                    return f"m:{candidate}"
        for name in ("requestId", "uuid"):
            candidate = value.get(name)
            if isinstance(candidate, str) and 0 < len(candidate) <= 200:
                return f"r:{candidate}"
        return None
    payload = value.get("payload")
    # A thread id on the record itself would be better than one carried down
    # from the header, but Codex does not write one: across 307,184 usage
    # records on this machine every token_count payload had exactly the keys
    # {info, rate_limits, type}. The lookup stays because it costs nothing and
    # a future Codex may start emitting it.
    thread = payload.get("thread_id") if isinstance(payload, Mapping) else thread_id
    if not isinstance(thread, str):
        thread = thread_id
    ordinal = value.get("ordinal")
    if isinstance(thread, str) and 0 < len(thread) <= 200 and isinstance(ordinal, int):
        return f"t:{thread}:{ordinal}"
    return None


# Measured against ~/.codex/sessions on 2026-09-21: 3,721 rollout files, 20GB,
# 307,184 token_count records spanning 2026-07-17..2026-09-21.
#
# Does Codex replay a billable turn across rollout files, the way Claude does?
# Essentially no, and not in a form any identity can catch:
#
#  * Verbatim replay -- the same (timestamp, last_token_usage) in two files --
#    occurred 0 times in 307,184 records. Claude, by contrast, deduplicates
#    578,117 of 1,167,646 records (~50%) on this same machine.
#  * A looser test that ignores the timestamp and matches the full
#    (last_token_usage, total_token_usage) pair found 4,041 excess records,
#    1.3% of the corpus and ~1.2% of the tokens. They come from 84 parent/child
#    file pairs where a subagent thread opened with a snapshot of its parent's
#    conversation.
#  * That snapshot is the only replay mechanism, and it is undetectable by
#    identity: the copy is written under the CHILD's thread id, with ordinals
#    renumbered from zero, and every record restamped with the copy instant.
#    Nothing in the copied record points back at the turn it came from.
#
# So (thread_id, ordinal) is exact rather than merely best-effort. A full
# rescan of the corpus with it reports 304,472 records accepted, 0 deduplicated
# and 0 unidentified, and per-(day, model) rows identical to the totals the
# store already held -- 39,303,870,837 tokens over 2026-07-17..2026-09-21,
# unchanged. What the identity buys is not a correction but a real ownership
# key for the scan store, plus an honest records_unidentified of 0 in place of
# 304,472 records that read as identity failures.
#
# Deliberately NOT done: deduplicating on the (last, total) usage fingerprint.
# It is a content hash, not an identity. It would drop 10,202 records for a
# 1.28B-token (3.26%) reduction, but 6,161 of those collapses are two records
# INSIDE one file, which the per-thread anchor sum(last) == max(total) says are
# legitimate. Paying ~0.8B tokens of over-deduction to recover ~0.46B of real
# double-count is the wrong trade; revisit only with per-turn evidence.


def fingerprint_identity(identity: str) -> int:
    """Fold a replay identity into one signed 64-bit integer.

    Signed on purpose: the same fingerprint is held in a Python set during a
    live scan and in a SQLite INTEGER column in the persistent scan store, and
    SQLite has no unsigned integer to bind the top half of the range to.
    """

    digest = hashlib.blake2b(identity.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def parse_usage_record(
    provider: str, value: Mapping[str, Any], model: str
) -> tuple[int, str, dict[str, int]] | None:
    """Parse one usage-bearing record into ``(usage_slot, model, metrics)``.

    The first element is the record's UTC slot (see ``SLOT_SECONDS``), not a
    calendar day; ``rollup_days`` derives days from it at read time.
    """

    if provider == "claude":
        message = value.get("message")
        usage = message.get("usage") if isinstance(message, Mapping) else None
        if not isinstance(usage, Mapping):
            return None
        day = _slot(value.get("timestamp") or message.get("timestamp"))
        model = _model(message.get("model"))
        input_tokens, output_tokens = (
            _count(usage.get("input_tokens")),
            _count(usage.get("output_tokens")),
        )
        cache_read = _count(usage.get("cache_read_input_tokens")) or 0
        cache_other = _count(usage.get("cache_creation_input_tokens")) or 0
        detail = usage.get("cache_creation")
        cache_5m = (
            _count(detail.get("ephemeral_5m_input_tokens")) or 0
            if isinstance(detail, Mapping)
            else 0
        )
        cache_1h = (
            _count(detail.get("ephemeral_1h_input_tokens")) or 0
            if isinstance(detail, Mapping)
            else 0
        )
        if cache_5m or cache_1h:
            cache_other = 0
    else:
        usage = _last_usage(value)
        if usage is None:
            return None
        day = _slot(value.get("timestamp"))
        input_tokens = _count(usage.get("input_tokens") or usage.get("inputTokens"))
        output_tokens = _count(usage.get("output_tokens") or usage.get("outputTokens"))
        cache_read = _count(usage.get("cached_input_tokens") or usage.get("cachedInputTokens")) or 0
        # Codex reports cache WRITES under their own key, and dropping it made
        # the parser lose them silently. It reads 0 in every record sampled on
        # this machine, but the rate card already prices a Codex cache write
        # (`cache_write_5m: 5` for gpt-5.6-sol), so the day OpenAI starts
        # populating it the tokens would vanish rather than be counted.
        #
        # WHY the 5m component: the key names no TTL, and OpenAI publishes one
        # prompt-cache write rate rather than a pair. `cache_write_5m` is the
        # component that carries it -- the card sets 5m and 1h to the same
        # figure for every Codex model -- and `cache_create_other_tokens`
        # would price identically while asserting less about what was observed.
        #
        # Left as its own bucket rather than subtracted from `input_tokens`:
        # the file's own header arithmetic (`total == input + output`, with
        # `cached_input_tokens` a subset of input) cannot settle whether a
        # cache write is inside input too, because the key is 0 in every
        # record here. Revisit against a record that populates it.
        cache_5m = (
            _count(usage.get("cache_write_input_tokens") or usage.get("cacheWriteInputTokens"))
            or 0
        )
        cache_1h = cache_other = 0
        # Codex reports input_tokens inclusive of the cached prefix, unlike
        # Anthropic. Leaving it inclusive counts the cached tokens twice and
        # prices them at the full input rate instead of the cache-read rate.
        if input_tokens is not None:
            if cache_read > input_tokens:
                cache_read = input_tokens
            input_tokens -= cache_read
    if day is None or input_tokens is None or output_tokens is None:
        return None
    return (
        day,
        model,
        {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_create_5m_tokens": cache_5m,
            "cache_create_1h_tokens": cache_1h,
            "cache_create_other_tokens": cache_other,
        },
    )


@dataclass(frozen=True)
class ParsedRecord:
    """One usage-bearing transcript record, with the replay identity it carries."""

    usage_slot: int
    model: str
    metrics: Mapping[str, int]
    identity: str | None


@dataclass
class FileParseCounters:
    """Per-file tallies, filled in as ``iter_history_records`` consumes lines.

    Mutable, unlike everything else here, because the records are streamed: a
    single Codex transcript on this machine reaches two gigabytes, and neither
    caller can afford to hold one file's records in memory to be handed a
    frozen summary at the end.
    """

    records_scanned: int = 0
    records_rejected: int = 0
    parse_error_count: int = 0
    # Codex usage records skipped as re-emissions; see ``iter_history_records``.
    records_reemitted: int = 0


def iter_history_records(
    path: Path,
    *,
    provider: str,
    counters: FileParseCounters,
    max_records: int | None = None,
) -> Iterator[ParsedRecord]:
    """Stream one transcript's usage records, deduplicating nothing.

    Deduplication is the caller's, because the identity of a replayed message
    is only decidable against every other file in the scan -- which a live
    bounded scan holds in memory and the persistent store holds on disk.
    """

    # Codex names the model once per session rather than per record, so the
    # last name seen carries forward within the file -- and, because that name
    # can appear after the first usage record, the file's declaration is read
    # up front so the opening records are labelled rather than left `unknown`.
    # The thread id is named once too, in the session_meta header, and carries
    # forward the same way -- it is half of every Codex record's replay identity.
    model = UNKNOWN_MODEL
    thread_id = None
    if provider == "codex":
        model = codex_declared_model(path) or UNKNOWN_MODEL
        thread_id = _thread_id_from_filename(path)
    # Only the file's OWN session_meta may name the thread. A subagent rollout
    # opens with its own header and then, at the very next record, embeds its
    # PARENT's session_meta as the head of the inherited conversation snapshot.
    # Letting that second header win relabels the child's own turns with the
    # parent's thread id, and because the two files number their ordinals
    # independently, (parent_id, ordinal) then collides with unrelated parent
    # records. Measured on this machine: 19 files, 307 of the child's real
    # records wrongly deduplicated, 45,081,243 tokens and $27.66 silently lost.
    header_seen = False
    # Codex RE-EMITS a token_count record: the same `last_token_usage` again,
    # under a new ordinal, while the thread's `total_token_usage` stays where it
    # was. Measured over ~/.codex/sessions on 2026-09-22: 5,829 such records,
    # ~0.826B tokens (~$563 at each day's mix), every one a repeat of the record
    # before it. Nothing new was requested -- the running total did not move --
    # but each carries a fresh (thread, ordinal) identity, so dedup cannot see
    # it and counting it inflates MEASURED usage. A record whose running total
    # equals the previous record's in the same file is therefore skipped.
    previous_total: tuple[int, int, int] | None = None
    with path.open("rb") as handle:
        for raw in handle:
            if max_records is not None and counters.records_scanned >= max_records:
                break
            counters.records_scanned += 1
            if len(raw) > _MAX_LINE_BYTES:
                counters.records_rejected += 1
                counters.parse_error_count += 1
                continue
            try:
                value = json.loads(
                    raw,
                    parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("constant")),
                )
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
                counters.records_rejected += 1
                counters.parse_error_count += 1
                continue
            if not isinstance(value, Mapping):
                counters.records_rejected += 1
                continue
            if provider == "codex":
                payload = value.get("payload")
                if isinstance(payload, Mapping) and payload.get("model") is not None:
                    model = _model(payload.get("model"), model)
                if not header_seen:
                    own = codex_thread_id(value)
                    if own is not None:
                        thread_id, header_seen = own, True
            record = parse_usage_record(provider, value, model)
            if record is None:
                continue
            if provider == "codex":
                running = _codex_running_total(value)
                if running is not None:
                    if running == previous_total:
                        counters.records_reemitted += 1
                        continue
                    previous_total = running
            slot, row_model, metrics = record
            yield ParsedRecord(
                usage_slot=slot,
                model=row_model,
                metrics=metrics,
                identity=record_identity(
                    provider, value, value.get("message"), thread_id=thread_id
                ),
            )


def root_identity_digest(root: Path | str) -> str:
    """Identify a CLI home without exposing the path it lives at."""

    return hashlib.sha256(str(Path(root).expanduser().resolve()).encode()).hexdigest()


def history_scan_roots(root: Path | str, provider: str) -> tuple[Path, ...]:
    """Every directory that holds one provider's transcripts, in scan order.

    A provider can write more than one tree. Claude writes the CLI's
    ``~/.claude/projects`` and, separately, the desktop app's agent-mode
    sessions under ``~/Library/Application Support/Claude``; a scan that reads
    only the first misses 1.28 GB of real transcripts. Both are derived from
    the SAME home directory, which is why one ``root_identity_digest`` over
    that home still identifies the whole set -- a store built under another
    home stays refused.

    The trees overlap on purpose, so every caller must deduplicate across
    roots by replay identity rather than assume disjoint sets.
    """

    home = Path(root)
    try:
        roots = [home / _SCAN_SUBDIRECTORY[provider]]
    except KeyError:
        raise ValueError("unsupported provider") from None
    if provider == "claude":
        roots.append(home.parent.joinpath(*_CLAUDE_DESKTOP_SESSIONS))
    return tuple(roots)


def history_scan_root(root: Path | str, provider: str) -> Path:
    """The PRIMARY transcript directory beneath a CLI home.

    Kept for callers that can only name one directory. Anything that scans
    must use ``history_scan_roots``, or it silently reads a subset.
    """

    return history_scan_roots(root, provider)[0]


def discover_local_cli_history(
    root: Path | str,
    *,
    provider: str,
    max_files: int = 2_000,
    max_total_bytes: int = 64 * 1024 * 1024,
    max_records: int = 100_000,
    tz: tzinfo | None = None,
) -> LocalHistoryImport:
    """Scan only bounded, nonsymlink JSONL files beneath one CLI root.

    The result is always partial or unknown: local histories can be deleted or
    compacted and therefore are never proof of complete provider usage.
    ``tz`` is the day basis of ``rows`` (the system zone when None).
    """

    provider = provider.lower()
    root = Path(root)
    root_digest = root_identity_digest(root)
    candidates: list[tuple[Path, Path]] = []
    for scan_root in history_scan_roots(root, provider):
        if not scan_root.is_dir() or scan_root.is_symlink():
            continue
        candidates.extend(
            (scan_root, path)
            for path in scan_root.rglob("*.jsonl")
            if path.is_file() and not path.is_symlink()
        )

    def recency(item: tuple[Path, Path]) -> tuple[int, str]:
        path = item[1]
        try:
            return path.stat().st_mtime_ns, path.as_posix()
        except OSError:
            return -1, path.as_posix()

    files = heapq.nlargest(max_files, candidates, key=recency)
    manifest = hashlib.sha256()
    file_count = scanned = accepted = rejected = errors = total_bytes = 0
    duplicates = unidentified = reemitted = 0
    # Replay identities, held as 8-byte fingerprints so a full-history scan
    # stays bounded in memory. Each maps to the day/model bucket the identity
    # feeds and to the component-wise MAXIMUM seen for it so far, so a later
    # occurrence carrying a fuller streaming snapshot raises the stored
    # components instead of being discarded. See `UsageScanStore._claim` for
    # the measurement that makes the maximum the right representative.
    seen: dict[int, tuple[dict[str, int], dict[str, int]]] = {}
    totals: dict[tuple[int, str], dict[str, int]] = {}
    for scan_root, path in files:
        try:
            size = path.stat().st_size
            relative = path.relative_to(scan_root).as_posix()
        except (OSError, ValueError):
            continue
        if size > 16 * 1024 * 1024 or total_bytes + size > max_total_bytes:
            rejected += 1
            continue
        total_bytes += size
        file_count += 1
        manifest.update(f"{relative}\0{size}\0".encode())
        counters = FileParseCounters()
        try:
            for record in iter_history_records(
                path, provider=provider, counters=counters, max_records=max_records - scanned
            ):
                fingerprint: int | None = None
                if record.identity is None:
                    unidentified += 1
                else:
                    fingerprint = fingerprint_identity(record.identity)
                    held = seen.get(fingerprint)
                    if held is not None:
                        duplicates += 1
                        owned, representative = held
                        for name, amount in record.metrics.items():
                            if amount > representative[name]:
                                owned[name] += amount - representative[name]
                                representative[name] = amount
                        continue
                bucket = totals.setdefault(
                    (record.usage_slot, record.model),
                    {**{name: 0 for name in record.metrics}, "request_count": 0},
                )
                for name, amount in record.metrics.items():
                    bucket[name] += amount
                bucket["request_count"] += 1
                if fingerprint is not None:
                    seen[fingerprint] = (bucket, dict(record.metrics))
                accepted += 1
        except OSError:
            rejected += 1
            errors += 1
        scanned += counters.records_scanned
        rejected += counters.records_rejected
        errors += counters.parse_error_count
        reemitted += counters.records_reemitted
    slot_rows = tuple(
        SlotUsage(usage_slot=slot, model=model, **metrics)
        for (slot, model), metrics in sorted(totals.items())
    )
    rows = rollup_days(slot_rows, tz)
    return LocalHistoryImport(
        provider=provider,
        rows=rows,
        slot_rows=slot_rows,
        records_reemitted=reemitted,
        coverage_state="partial" if rows else "unknown",
        root_identity_digest=root_digest,
        manifest_digest=manifest.hexdigest(),
        files_scanned=file_count,
        records_scanned=scanned,
        records_accepted=accepted,
        records_rejected=rejected,
        parse_error_count=errors,
        records_deduplicated=duplicates,
        records_unidentified=unidentified,
    )
