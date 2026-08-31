"""Human-readable rendering for ``coord board``.

``coord board`` has always had exactly one output shape: one line of
unwrapped JSON, printed whether a script or a person at a terminal asked for
it, and ``--group-by`` decorated each row with a ``group`` field without ever
actually clustering rows by it. Scripts still get precisely that JSON --
``cli.py`` gates on it being a real terminal (and on ``--json``), and this
module has no part in that path or its byte-for-byte shape. What this module
renders is the *other* case: a person sitting at a terminal gets a table --
one row per work item, real group header rows, and a lease-remaining column
that is blank wherever there is no lease to show.

This takes ``coord_db.board_rows`` output directly, not the trimmed dict
``coord board`` emits as JSON -- the JSON path drops ``claim_expires_at``,
and the lease-remaining column cannot be computed without it.
"""

from __future__ import annotations

import shutil
import time
from typing import Any, Iterable

_ID_WIDTH = 22
_STATUS_WIDTH = 11
_ASSIGNEE_WIDTH = 12
_LEASE_WIDTH = 8
_COL_GAP = 2
_MIN_TITLE_WIDTH = 16
_FALLBACK_TERMINAL_WIDTH = 100

_UNGROUPED = "(ungrouped)"

_ELLIPSIS = "…"


def _truncate(text: str, width: int) -> str:
    """Shorten ``text`` to fit ``width`` columns, marking a real cut with an ellipsis.

    A value that already fits is returned unchanged rather than padded --
    padding is the formatting layer's job, applied once at the call site, so
    this stays a pure shortening function that is easy to test in isolation.
    """
    text = str(text)
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return _ELLIPSIS
    return text[: width - 1] + _ELLIPSIS


def _format_duration(seconds: float) -> str:
    """A short, fixed-vocabulary duration: ``4h03m``, ``12m08s``, ``41s``.

    One unit pair at a time -- the larger of the two units present, plus the
    next one down -- because a lease column exists to answer "is this about
    to expire", and a duration spelled out to the second when it is two days
    away answers a question nobody asked while pushing the row it is on off
    the edge of the terminal.
    """
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _lease_remaining(row: dict[str, Any], now: float) -> str:
    """Time left on this row's claim lease, or blank when there is none to show.

    ``claim_expires_at`` is only populated while ``v_work_owner``'s join finds
    a live claim; a row with none carries it as ``None``. A lease that has
    already lapsed is not "remaining" either -- the board's own status
    derivation already treats an expired lease as attention, not as running,
    so printing a stale positive number here would contradict the status
    column sitting right next to it.
    """
    expires = row.get("claim_expires_at")
    if expires in (None, ""):
        return ""
    try:
        remaining = float(expires) - float(now)
    except (TypeError, ValueError):
        return ""
    if remaining <= 0:
        return ""
    return _format_duration(remaining)


def _terminal_width(width: int | None) -> int:
    if width is not None:
        return max(1, int(width))
    try:
        return shutil.get_terminal_size(fallback=(_FALLBACK_TERMINAL_WIDTH, 24)).columns
    except OSError:
        return _FALLBACK_TERMINAL_WIDTH


def _title_width(total_width: int) -> int:
    fixed = _ID_WIDTH + _STATUS_WIDTH + _ASSIGNEE_WIDTH + _LEASE_WIDTH + _COL_GAP * 4
    return max(_MIN_TITLE_WIDTH, total_width - fixed)


def _group_key(row: dict[str, Any]) -> str:
    value = row.get("group")
    text = str(value).strip() if value is not None else ""
    return text or _UNGROUPED


def _group_rows(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Cluster ``rows`` by their already-computed ``group`` field.

    ``coord_db.board_rows`` sets ``group`` per row but never clusters rows by
    it -- that is the defect this function exists to fix for the human
    rendering. Order is preserved both across groups (by each group's first
    appearance) and within one group (the scan order ``board_rows`` already
    returned, most recently updated first), so grouping only rearranges rows
    into clusters and never re-sorts within them.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(_group_key(row), []).append(row)
    return groups


def _row_line(row: dict[str, Any], *, now: float, title_width: int) -> str:
    work_id = _truncate(row.get("work_id") or "", _ID_WIDTH)
    title = _truncate(row.get("title") or "", title_width)
    status = _truncate(row.get("status") or "", _STATUS_WIDTH)
    assignee = _truncate(row.get("assignee") or "", _ASSIGNEE_WIDTH)
    lease = _truncate(_lease_remaining(row, now), _LEASE_WIDTH)
    gap = " " * _COL_GAP
    return (
        f"{work_id:<{_ID_WIDTH}}{gap}"
        f"{title:<{title_width}}{gap}"
        f"{status:<{_STATUS_WIDTH}}{gap}"
        f"{assignee:<{_ASSIGNEE_WIDTH}}{gap}"
        f"{lease:<{_LEASE_WIDTH}}"
    ).rstrip()


def _header_line(title_width: int) -> str:
    gap = " " * _COL_GAP
    return (
        f"{'ID':<{_ID_WIDTH}}{gap}"
        f"{'TITLE':<{title_width}}{gap}"
        f"{'STATUS':<{_STATUS_WIDTH}}{gap}"
        f"{'ASSIGNEE':<{_ASSIGNEE_WIDTH}}{gap}"
        f"{'LEASE':<{_LEASE_WIDTH}}"
    ).rstrip()


def render_board_table(
    rows: list[dict[str, Any]],
    *,
    group_by: str = "module",
    now: float | None = None,
    width: int | None = None,
) -> str:
    """Render ``coord_db.board_rows`` output as a table for a human at a terminal.

    ``rows`` must be the untrimmed rows ``coord_db.board_rows`` returns, in
    their existing order -- this only clusters them by their ``group`` field
    and formats columns; it does no querying or sorting of its own beyond
    that clustering. ``now`` should be the same instant ``board_rows`` used to
    derive ``status``, so the lease-remaining column agrees with it; it
    defaults to the wall clock only for standalone callers that have no
    database instant to hand in.
    """
    if now is None:
        now = time.time()
    if not rows:
        return f"(no work items; grouped by {group_by})"

    title_width = _title_width(_terminal_width(width))
    lines = [_header_line(title_width)]
    for group, group_rows in _group_rows(rows).items():
        lines.append(f"-- {group} ({len(group_rows)}) --")
        lines.extend(_row_line(row, now=now, title_width=title_width) for row in group_rows)
    return "\n".join(lines)
