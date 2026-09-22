"""Read-only import of pre-transcript CodexBar usage ledgers.

``~/.codex/sessions`` only reaches back to the day Codex began keeping
rollouts -- 2026-07-17 on this machine. Spend before that is not recoverable
from transcripts at all; the only surviving record is the high-water JSON that
the CodexBar-derived tooling left behind. The Claude side has the same problem
in a different shape: three days inside the transcript range hold no surviving
``~/.claude/projects`` records at all, and only the Claude high-water JSON
still remembers them.

Either file is evidence of a different kind and must never be blended with a
self-computed day:

* Its accounting is not ours. The header names it
  ``provider_volume_anchored_local_mix_estimate`` and its own warning calls it
  an API-equivalent list-price estimate rather than billed spend.
* For Codex its accounting is one we have since found wrong. Codex reports
  ``input_tokens`` inclusive of the cached prefix; this ledger counted that
  prefix again as fresh input and priced it at the full input rate. Its cost
  therefore OVERSTATES, and by an amount that grows with cache hit rate.
* For Claude the direction of the bias is NOT known. The ledger's high-water
  rule -- a component-wise maximum over every preserved observation -- cannot
  decrease, which can leave it above a later corrected source. But the maximum
  is also the CORRECT representative for Claude Code's per-content-block
  streaming records, where the early lines of one response carry partial usage
  snapshots and only the last carries the complete one. Calling this side
  "overstates" was an assertion the evidence does not support, so it is
  labelled indeterminate instead.

So these rows are imported frozen, under their own source key, and are only
ever read for days no self-computed row covers. Nothing here writes to the
source file; it is opened for reading and parsed, and that is all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from .ledger import DailyUsage

# The identity these rows are stored under. Anything reading the scan store can
# tell a frozen legacy row from a self-computed one by this alone.
LEGACY_SOURCE: Final = "codexbar_high_water_v1"

# What the numbers mean, carried into the dashboard payload verbatim so the UI
# labels them rather than having to guess.
LEGACY_SEMANTICS: Final = "third_party_list_price_estimate_known_to_overstate"

# Kept within the dashboard proxy's 240-character bounded-text limit on
# purpose: a warning the proxy refuses is a warning the UI never shows, and
# these rows must not reach a chart without it.
LEGACY_WARNING: Final = (
    "Imported from a third-party CodexBar ledger, not computed from this "
    "machine's transcripts. It counted cached input twice at the full input "
    "rate, so this cost is an upper bound that overstates by an unmeasured margin."
)

# A ledger names its format as ``<publisher>.<format>``. Only the format is
# checked: the publisher is whoever produced the file, and this importer reads
# the layout, not the producer. The header sums verified per layout below are
# what actually prove a file is readable, so this is a guard against pointing
# the importer at the wrong kind of file, not a trust boundary.
_SCHEMA: Final = "codex-usage-high-water.v1"

# Each (day, model) entry is a packed list of four integers. Index 3 is the
# cost in nanodollars. Verified against the file's own header on 2026-09-21:
# sum(index 0) + sum(index 2) == total_tokens exactly, and sum(index 3) ==
# total_cost_nanos exactly -- which also settles that index 1 (the cached
# prefix) is a SUBSET of index 0 rather than a separate bucket.
_INPUT_INCLUSIVE: Final = 0
_CACHED_INPUT: Final = 1
_OUTPUT: Final = 2
_COST_NANOS: Final = 3

_MAX_BYTES: Final = 64 * 1024 * 1024

# The Claude ledger is the same idea in a different packing, so the two are
# described rather than branched on.
CLAUDE_LEGACY_SOURCE: Final = "codexbar_claude_high_water_v1"
CLAUDE_LEGACY_SEMANTICS: Final = "third_party_high_water_list_price_estimate"

# Also inside the dashboard proxy's 240-character bounded-text limit.
CLAUDE_LEGACY_WARNING: Final = (
    "Imported from a third-party CodexBar ledger, not computed from this "
    "machine's transcripts. It is a component-wise high-water maximum, which "
    "is the right estimator for streamed usage, so its bias is unmeasured."
)

_CLAUDE_SCHEMA: Final = "claude-usage-high-water.v1"


@dataclass(frozen=True)
class LedgerLayout:
    """How one high-water document packs a ``(day, model)`` entry.

    Both files store a list of integers per entry, but not the same list. The
    positions below were each pinned against the document's own header -- the
    sums of the token indices reproduce ``total_tokens`` exactly and the cost
    index reproduces ``total_cost_nanos`` exactly -- so a wrong guess here
    cannot pass unnoticed.
    """

    provider: str
    schema: str
    source: str
    semantics: str
    warning: str
    input: int
    output: int
    cache_read: int
    cost_nanos: int
    cache_create: int | None = None
    # Codex reports its input inclusive of the cached prefix; Claude does not.
    input_includes_cache_read: bool = False
    # Which way this ledger's cost is known to be wrong. Per layout rather than
    # per project, because the two ledgers are wrong for different reasons and
    # one of them is not known to be wrong at all: a reader shown "overstates"
    # against the Claude rows would be told something the evidence does not
    # support. An inaccurate provenance label is worse than none.
    cost_bias: str = "overstates"

    @property
    def width(self) -> int:
        indices = [self.input, self.output, self.cache_read, self.cost_nanos]
        if self.cache_create is not None:
            indices.append(self.cache_create)
        return max(indices) + 1


CODEX_LAYOUT: Final = LedgerLayout(
    provider="codex",
    schema=_SCHEMA,
    source=LEGACY_SOURCE,
    semantics=LEGACY_SEMANTICS,
    warning=LEGACY_WARNING,
    input=_INPUT_INCLUSIVE,
    cache_read=_CACHED_INPUT,
    output=_OUTPUT,
    cost_nanos=_COST_NANOS,
    input_includes_cache_read=True,
)

# Verified against the live file on 2026-09-21 two independent ways. Its own
# header: sum(0)+sum(1)+sum(2)+sum(3) == total_tokens (134,807,951,119) and
# sum(4) == total_cost_nanos (87,130,802,988,550), both exact. And against the
# scan store on days the two agree component for component -- 2026-03-21
# claude-haiku-4-5 reads input 16,311 / cache_read 12,792,649 / cache_creation
# 1,006,474 / output 75,439 in both, which is what fixes index 2 as the cache
# write and index 3 as the output rather than the reverse.
CLAUDE_LAYOUT: Final = LedgerLayout(
    provider="claude",
    schema=_CLAUDE_SCHEMA,
    source=CLAUDE_LEGACY_SOURCE,
    semantics=CLAUDE_LEGACY_SEMANTICS,
    warning=CLAUDE_LEGACY_WARNING,
    input=0,
    cache_read=1,
    cache_create=2,
    output=3,
    cost_nanos=4,
    # See this module's docstring: the high-water maximum is the right
    # representative for per-content-block streaming records, so this ledger is
    # not known to lean either way.
    cost_bias="indeterminate",
)

LAYOUTS: Final = {"codex": CODEX_LAYOUT, "claude": CLAUDE_LAYOUT}


class LegacyLedgerError(RuntimeError):
    """The legacy ledger could not be read or is not the expected document."""


@dataclass(frozen=True)
class LegacyLedgerRead:
    """One read of the legacy ledger, already filtered to the days wanted."""

    source: str
    rows: tuple[DailyUsage, ...]
    before: str
    first_day: str | None
    last_day: str | None
    days_available: int
    days_selected: int
    rows_skipped: int
    source_digest: str
    pricing_key: str
    accounting: str
    semantics: str = LEGACY_SEMANTICS
    warning: str = LEGACY_WARNING
    # Empty when the selector was `before`; the named days otherwise, so the
    # payload records which rule admitted these rows rather than implying a
    # cutoff that was never applied.
    selected_days: tuple[str, ...] = ()

    def provenance(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "semantics": self.semantics,
            "canonical": False,
            "self_computed": False,
            "frozen": True,
            "warning": self.warning,
            "imported_before": self.before,
            "imported_days": list(self.selected_days),
            "first_day": self.first_day,
            "last_day": self.last_day,
            "days_available": self.days_available,
            "days_selected": self.days_selected,
            "rows_skipped": self.rows_skipped,
            "source_digest": self.source_digest,
            "upstream_pricing_key": self.pricing_key,
            "upstream_accounting": self.accounting,
        }


def _schema_matches(schema: str, expected_format: str) -> bool:
    """True for ``<format>`` itself or any ``<publisher>.<format>``."""

    return schema == expected_format or schema.endswith("." + expected_format)


def _text(value: object, fallback: str = "") -> str:
    return value if isinstance(value, str) and len(value) <= 200 else fallback


def _day(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _packed(value: object, layout: LedgerLayout = CODEX_LAYOUT) -> dict[str, int] | None:
    """Unpack one packed entry into named counts, or reject it.

    Rejects rather than pads: an entry shorter than the layout needs, or one
    carrying anything but a nonnegative integer where a count belongs, is not
    an entry this layout describes and guessing at it would invent spend.
    """

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    if len(value) < layout.width:
        return None
    fields = {
        "input": layout.input,
        "output": layout.output,
        "cache_read": layout.cache_read,
        "cost_nanos": layout.cost_nanos,
    }
    if layout.cache_create is not None:
        fields["cache_create"] = layout.cache_create
    out: dict[str, int] = {"cache_create": 0}
    for name, index in fields.items():
        item = value[index]
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            return None
        out[name] = item
    return out


def _load(
    path: Path | str | None,
    *,
    layout: LedgerLayout,
) -> tuple[bytes, Mapping[str, Any], Mapping[str, Any]]:
    """Open, bound, parse and schema-check one ledger document.

    Read-only by construction: the file is opened for reading, and a symlink
    is refused rather than followed, so nothing here can be redirected into
    writing or into a file this import was not pointed at.
    """

    # No default location: a ledger belongs to whatever produced it, and a
    # guessed path would couple this importer to one producer's layout on disk.
    if path is None:
        raise LegacyLedgerError("name the legacy ledger to import")
    source = Path(path)
    try:
        if source.is_symlink() or not source.is_file():
            raise LegacyLedgerError(f"no legacy ledger at {source.name}")
        if source.stat().st_size > _MAX_BYTES:
            raise LegacyLedgerError("legacy ledger is implausibly large")
        raw = source.read_bytes()
    except OSError as error:
        raise LegacyLedgerError(f"legacy ledger unreadable: {error}") from error
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise LegacyLedgerError(f"legacy ledger is not JSON: {error}") from error
    if not isinstance(document, Mapping):
        raise LegacyLedgerError("legacy ledger is not a JSON object")
    schema = _text(document.get("schema"))
    if schema and not _schema_matches(schema, layout.schema):
        raise LegacyLedgerError(f"unsupported legacy ledger schema {schema!r}")
    days = document.get("days")
    if not isinstance(days, Mapping):
        raise LegacyLedgerError("legacy ledger carries no days")
    return raw, document, days


def legacy_ledger_days(
    path: Path | str | None = None,
    *,
    layout: LedgerLayout = CODEX_LAYOUT,
) -> tuple[str, ...]:
    """Every valid day the ledger names, for a caller choosing which to import.

    Goes through the same bounded, schema-checked load as the import itself, so
    a caller cannot end up selecting days out of a document the importer would
    then refuse.
    """

    _raw, _document, days = _load(path, layout=layout)
    return tuple(sorted({day for raw in days if (day := _day(raw)) is not None}))


def read_legacy_ledger(
    path: Path | str | None = None,
    *,
    before: str | None = None,
    only_days: Sequence[str] | None = None,
    layout: LedgerLayout = CODEX_LAYOUT,
) -> LegacyLedgerRead:
    """Parse a legacy ledger, keeping only the days a caller can justify.

    Exactly one selector is required, and both express the same rule -- import
    only what this machine cannot compute for itself:

    ``before``
        Keep days strictly before this one. Used where the transcripts have a
        clean floor, as Codex does: everything from the first rollout day
        onward is self-computed.
    ``only_days``
        Keep exactly these days. Used where the gap is interior rather than a
        floor, as Claude's is: three days inside the transcript range have no
        surviving records, and naming them is the only honest selector.

    Either way the filtering happens here rather than at read time, because a
    legacy row that never enters the store cannot later be mistaken for one
    that lost a tie.
    """

    if (before is None) == (only_days is None):
        raise LegacyLedgerError("pass exactly one of `before` or `only_days`")
    cutoff = ""
    wanted: set[str] = set()
    if before is not None:
        parsed = _day(before)
        if parsed is None:
            raise LegacyLedgerError("cutoff day must be an ISO date")
        cutoff = parsed
    else:
        assert only_days is not None
        for candidate in only_days:
            parsed = _day(candidate)
            if parsed is None:
                raise LegacyLedgerError("every selected day must be an ISO date")
            wanted.add(parsed)
        if not wanted:
            raise LegacyLedgerError("`only_days` names no days")
    raw, document, days = _load(path, layout=layout)

    rows: list[DailyUsage] = []
    selected: set[str] = set()
    available: set[str] = set()
    skipped = 0
    for raw_day, models in days.items():
        usage_date = _day(raw_day)
        if usage_date is None or not isinstance(models, Mapping):
            skipped += 1
            continue
        available.add(usage_date)
        keep = usage_date < cutoff if before is not None else usage_date in wanted
        if not keep:
            continue
        for raw_model, packed in models.items():
            model = _text(raw_model)
            entry = _packed(packed, layout)
            if not model or entry is None:
                skipped += 1
                continue
            # Where the ledger reports its input inclusive of the cached
            # prefix, restate it the way this project counts that provider
            # everywhere else: the cached prefix is its own bucket and the
            # fresh input excludes it. Token TOTALS are unchanged by this; only
            # the shape is, so a legacy day and a self-computed day can be read
            # with one set of column names. The COST is left exactly as the
            # ledger computed it -- restating it at our rates would invent a
            # figure this ledger never asserted.
            cache_read = entry["cache_read"]
            input_tokens = entry["input"]
            if layout.input_includes_cache_read:
                cache_read = min(cache_read, input_tokens)
                input_tokens -= cache_read
            rows.append(
                DailyUsage(
                    usage_date=usage_date,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=entry["output"],
                    cache_read_tokens=cache_read,
                    # The ledger does not split a cache write by TTL, and
                    # guessing a TTL would change what the row says. `other` is
                    # the bucket that asserts only "a cache write happened".
                    cache_create_other_tokens=entry["cache_create"],
                    api_rate_estimate_nanos=entry["cost_nanos"],
                )
            )
            selected.add(usage_date)
    rows.sort(key=lambda row: (row.usage_date, row.model))
    return LegacyLedgerRead(
        source=layout.source,
        rows=tuple(rows),
        before=cutoff,
        first_day=min(selected) if selected else None,
        last_day=max(selected) if selected else None,
        days_available=len(available),
        days_selected=len(selected),
        rows_skipped=skipped,
        source_digest=hashlib.sha256(raw).hexdigest(),
        pricing_key=_text(document.get("pricing_key")),
        accounting=_text(document.get("accounting")),
        semantics=layout.semantics,
        warning=layout.warning,
        selected_days=tuple(sorted(wanted)),
    )
