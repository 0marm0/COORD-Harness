#!/usr/bin/env python3
"""Export the seeded demo board as a static, read-only site.

Someone evaluating this project should not have to install anything, run a
server, or open a database to see what the board looks like. This seeds a
throwaway copy of `coordharness.demo`'s fictional scenario in a temporary
directory, builds the same read model the live board serves
(`coordharness.board.snapshot.build_snapshot`), and writes a small static
bundle -- one `index.html` plus the JSON it renders -- to `--out`. The bundle
is self-contained: opening `index.html` directly from disk (no server, no
network) renders the full board, because the page reads its data from a
`<script type="application/json">` block rather than fetching a file. The
sibling `snapshot.json` is written too, for anyone who wants the raw data
without parsing it back out of the page.

Every control on the page either works standalone (the text filter runs
against the embedded JSON in the browser) or is visibly disabled with a note
explaining that it needs a live coordination server this export does not
have. There is no fake button that looks live and does nothing.

The demo database and its job-progress sidecars live only inside a
`tempfile.TemporaryDirectory` for the seed-and-read step and are discarded
before this process exits -- nothing about the machine that ran this script,
or its filesystem layout, ends up in the output. `_scrub` is a second,
independent check on that claim: it walks the built snapshot for anything
that looks like an absolute filesystem path and fails loudly rather than
writing it out.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

from coordharness.board.snapshot import build_snapshot
from coordharness.demo import seed

# macOS user homes, Linux homes, and every realpath a Python TemporaryDirectory
# can resolve to on the platforms this runs on (/tmp, /private/tmp,
# /private/var, /var/folders) -- host-specific shapes the demo fixtures never
# contain and this export must never carry.
_ABS_PATH_RE = re.compile(r"/Users/|/home/|/private/(?:tmp|var)/|/var/folders/|/tmp/")


def _scrub(value: Any, *, where: str = "$") -> Any:
    """Recursively confirm (never rewrite) that no string looks like a host path.

    Raising here rather than redacting is deliberate: a redaction would let a
    real leak ship silently reshaped as "[REDACTED-PATH]" and look intentional.
    The demo fixtures are synthetic and reviewed to contain no such thing, so
    a match means the read model changed underneath this tool -- that is
    worth stopping the export for, not papering over.
    """
    if isinstance(value, str):
        if _ABS_PATH_RE.search(value):
            raise SystemExit(
                f"refusing to export: absolute-path-shaped string at {where!r}: {value!r}"
            )
        return value
    if isinstance(value, dict):
        for key, item in value.items():
            _scrub(item, where=f"{where}.{key}")
        return value
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scrub(item, where=f"{where}[{index}]")
        return value
    return value


def _build_snapshot() -> dict[str, Any]:
    """Seed a throwaway demo database and read back its board snapshot."""
    with tempfile.TemporaryDirectory(prefix="coord-static-export-") as tmp:
        db_path = Path(tmp) / "coord.db"
        seed(db_path, quiet=True)
        return build_snapshot(db_path)


def _embed_json(data: dict[str, Any]) -> str:
    """Serialize for a `<script type="application/json">` block.

    `</script>` inside a JSON string would otherwise close the tag early and
    hand the rest of the payload to the HTML parser as markup.
    """
    return json.dumps(data, indent=2, sort_keys=True).replace("</", "<\\/")


_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Coordination Harness -- Demo Board (Static Export)</title>
<meta name="robots" content="noindex">
<style>
:root {{
  color-scheme: light dark;
  --bg: #0f1116;
  --panel: #171a21;
  --border: #2a2e38;
  --text: #e8eaf0;
  --muted: #9aa1b1;
  --accent: #6ea8fe;
  --running: #3fb950;
  --attention: #e5934a;
  --done: #5b6272;
  --next: #6ea8fe;
}}
@media (prefers-color-scheme: light) {{
  :root {{
    --bg: #f6f7fa; --panel: #ffffff; --border: #dde1e8; --text: #14161c;
    --muted: #5a6072; --accent: #2f5fd0;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 0 0 3rem;
  background: var(--bg); color: var(--text);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
main {{ max-width: 68rem; margin: 0 auto; padding: 0 1.25rem; }}
a {{ color: var(--accent); }}
.banner {{
  background: var(--panel); border-bottom: 1px solid var(--border);
  padding: 1rem 1.25rem;
}}
.banner p {{ max-width: 68rem; margin: 0 auto; }}
.banner strong {{ color: var(--attention); }}
h1 {{ font-size: 1.35rem; margin: 1.5rem 0 0.25rem; }}
.subtitle {{ color: var(--muted); margin: 0 0 1.25rem; font-size: 0.9rem; }}
.tiles {{ display: flex; gap: 0.75rem; flex-wrap: wrap; margin: 1rem 0 1.5rem; }}
.tile {{
  background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
  padding: 0.6rem 1rem; min-width: 6rem;
}}
.tile .n {{ font-size: 1.4rem; font-weight: 600; display: block; }}
.tile .l {{ color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.03em; }}
.controls {{ display: flex; gap: 0.75rem; align-items: center; margin: 1rem 0; flex-wrap: wrap; }}
#filter {{
  background: var(--panel); border: 1px solid var(--border); color: var(--text);
  border-radius: 6px; padding: 0.45rem 0.7rem; font-size: 0.9rem; min-width: 16rem;
}}
button[disabled] {{
  background: var(--panel); color: var(--muted); border: 1px solid var(--border);
  border-radius: 6px; padding: 0.45rem 0.8rem; cursor: not-allowed; font-size: 0.85rem;
}}
.disabled-note {{ color: var(--muted); font-size: 0.8rem; }}
table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }}
th, td {{ text-align: left; padding: 0.5rem 0.7rem; border-bottom: 1px solid var(--border); font-size: 0.87rem; }}
th {{ color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: 0.72rem; letter-spacing: 0.03em; }}
tr:last-child td {{ border-bottom: none; }}
.status {{ display: inline-flex; align-items: center; gap: 0.4rem; }}
.dot {{ width: 0.55rem; height: 0.55rem; border-radius: 50%; display: inline-block; }}
.dot.running {{ background: var(--running); }}
.dot.attention {{ background: var(--attention); }}
.dot.done {{ background: var(--done); }}
.dot.next {{ background: var(--next); }}
.muted {{ color: var(--muted); }}
section {{ margin-top: 2rem; }}
.empty-row {{ color: var(--muted); font-style: italic; }}
footer {{ max-width: 68rem; margin: 3rem auto 0; padding: 0 1.25rem; color: var(--muted); font-size: 0.8rem; }}
</style>
</head>
<body>
<div class="banner">
  <p>
    <strong>Static snapshot of synthetic demo data.</strong>
    Every row on this page was generated by a fictional seed scenario
    (<code>coordharness.demo</code>) -- no real project, person, or
    credential is represented. Exported {exported_at}. This page has no
    server behind it: nothing here can claim work, run an action, or reach a
    live database.
  </p>
</div>
<main>
  <h1>Demo board</h1>
  <p class="subtitle">Read-only export -- {work_item_count} work items and {job_count} tracked jobs ({row_count} rows total), {session_count} agent sessions -- from the scenario's own clock at {generated_at}.</p>

  <div class="tiles" id="tiles"></div>

  <div class="controls">
    <input id="filter" type="search" placeholder="Filter by id, title, owner, or module" aria-label="Filter rows">
    <button type="button" disabled title="Live actions require a running coordination server, which this static export does not have.">Live actions</button>
    <span class="disabled-note">Disabled -- actions need a live server; this export has none.</span>
  </div>

  <section aria-labelledby="board-heading">
    <h2 id="board-heading" style="font-size:1rem;">Work items</h2>
    <table id="board-table">
      <thead>
        <tr><th>Status</th><th>Title</th><th>Owner</th><th>Module</th><th>Priority</th><th>Note</th></tr>
      </thead>
      <tbody></tbody>
    </table>
  </section>

  <section aria-labelledby="sessions-heading">
    <h2 id="sessions-heading" style="font-size:1rem;">Agent sessions</h2>
    <table id="sessions-table">
      <thead>
        <tr><th>Session</th><th>Actor</th><th>Label</th><th>Live</th></tr>
      </thead>
      <tbody></tbody>
    </table>
  </section>
</main>
<footer>
  Generated by <code>tools/export_static_board.py</code> -- see
  <code>docs/static-demo.md</code> in the source repository for how to
  rebuild this bundle. Source: <code>{source}</code>.
</footer>
<script type="application/json" id="board-data">{embedded_json}</script>
<script>
(function () {{
  "use strict";
  var data = JSON.parse(document.getElementById("board-data").textContent);
  var STATUS_CLASS = {{
    claimed: "running", running: "running",
    attention: "attention", blocked: "attention", failed: "attention", needs_verification: "attention", artifact_present: "attention",
    archived: "done", canceled: "done", cancelled: "done", closed: "done", complete: "done", completed: "done", done: "done", skipped: "done", superseded: "done", success: "done"
  }};
  function statusClass(status) {{ return STATUS_CLASS[status] || "next"; }}

  function tile(n, label) {{
    var el = document.createElement("div");
    el.className = "tile";
    var num = document.createElement("span");
    num.className = "n"; num.textContent = String(n);
    var lab = document.createElement("span");
    lab.className = "l"; lab.textContent = label;
    el.appendChild(num); el.appendChild(lab);
    return el;
  }}
  var summary = data.summary || {{}};
  var tiles = document.getElementById("tiles");
  [["running", "Running"], ["attention", "Needs attention"], ["next", "Next up"], ["done", "Done"], ["total", "Total"]]
    .forEach(function (pair) {{ tiles.appendChild(tile(summary[pair[0]] || 0, pair[1])); }});

  function cell(text) {{ var td = document.createElement("td"); td.textContent = text || ""; return td; }}

  var rows = data.rows || [];
  var tbody = document.querySelector("#board-table tbody");
  function renderRows(filterText) {{
    var needle = (filterText || "").toLowerCase();
    tbody.textContent = "";
    var shown = 0;
    rows.forEach(function (row) {{
      var haystack = [row.id, row.title, row.owner, row.module].join(" ").toLowerCase();
      if (needle && haystack.indexOf(needle) === -1) return;
      shown += 1;
      var tr = document.createElement("tr");
      var statusTd = document.createElement("td");
      var wrap = document.createElement("span");
      wrap.className = "status";
      var dot = document.createElement("span");
      dot.className = "dot " + statusClass(row.status);
      var label = document.createElement("span");
      label.textContent = row.status || "";
      wrap.appendChild(dot); wrap.appendChild(label);
      statusTd.appendChild(wrap);
      tr.appendChild(statusTd);
      tr.appendChild(cell(row.title || row.id));
      tr.appendChild(cell(row.owner));
      tr.appendChild(cell(row.module));
      tr.appendChild(cell(row.priority != null ? String(row.priority) : ""));
      tr.appendChild(cell(row.current_step));
      tbody.appendChild(tr);
    }});
    if (shown === 0) {{
      var tr = document.createElement("tr");
      var td = document.createElement("td");
      td.colSpan = 6; td.className = "empty-row"; td.textContent = "No rows match that filter.";
      tr.appendChild(td); tbody.appendChild(tr);
    }}
  }}
  renderRows("");
  document.getElementById("filter").addEventListener("input", function (event) {{
    renderRows(event.target.value);
  }});

  var sessions = data.sessions || [];
  var sbody = document.querySelector("#sessions-table tbody");
  sessions.forEach(function (session) {{
    var tr = document.createElement("tr");
    tr.appendChild(cell(session.id));
    tr.appendChild(cell(session.actor));
    tr.appendChild(cell(session.label));
    tr.appendChild(cell(session.live ? "yes" : "no"));
    sbody.appendChild(tr);
  }});
  if (!sessions.length) {{
    var tr = document.createElement("tr");
    var td = document.createElement("td");
    td.colSpan = 4; td.className = "empty-row"; td.textContent = "No sessions in this snapshot.";
    tr.appendChild(td); sbody.appendChild(tr);
  }}
}})();
</script>
</body>
</html>
"""


def _is_job_row(row: dict[str, Any]) -> bool:
    """True for a row synthesized from a job-progress sidecar, not a work item.

    `board.snapshot.build_snapshot` gives every such row an id of the form
    `job:<identity>` (see the `jobs` loop in `snapshot.py`); a plain work
    item keeps its own declared work id. `bucket` cannot make this
    distinction -- it is an independent per-work-item surface label (some
    ordinary work items are themselves bucket "job"), so id shape is the
    only reliable signal.
    """
    return str(row.get("id", "")).startswith("job:")


def render_index_html(snapshot: dict[str, Any], exported_at: str) -> str:
    rows = snapshot.get("rows", [])
    # `rows` mixes two different units under one list: work items (initiatives
    # and their tasks) and tracked local-job sidecars. A bare row count reads
    # as a contradiction next to the README's work-item seed count -- name the
    # units separately instead of collapsing them into one ambiguous number.
    job_rows = [row for row in rows if _is_job_row(row)]
    work_item_count = len(rows) - len(job_rows)
    return _PAGE_TEMPLATE.format(
        exported_at=exported_at,
        generated_at=snapshot.get("generated_at", ""),
        source=snapshot.get("source", ""),
        work_item_count=work_item_count,
        job_count=len(job_rows),
        row_count=len(rows),
        session_count=len(snapshot.get("sessions", [])),
        embedded_json=_embed_json(snapshot),
    )


def export(out_dir: Path) -> dict[str, Any]:
    """Build the bundle into `out_dir`. Returns the snapshot that was written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    snapshot = _scrub(_build_snapshot())
    (out_dir / "snapshot.json").write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out_dir / "index.html").write_text(
        render_index_html(snapshot, exported_at), encoding="utf-8"
    )
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", required=True, type=Path, help="directory to write the static bundle into"
    )
    args = parser.parse_args(argv)
    snapshot = export(args.out)
    rows = snapshot.get("rows", [])
    job_count = sum(1 for row in rows if _is_job_row(row))
    work_item_count = len(rows) - job_count
    print(
        f"wrote static demo board to {args.out} "
        f"({work_item_count} work items, {job_count} tracked jobs, {len(rows)} rows total)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
