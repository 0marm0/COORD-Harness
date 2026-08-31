# Static demo board

Evaluating the board normally means installing the package, seeding a
database, and running a server. `tools/export_static_board.py` skips all of
that: it seeds the same fictional scenario `coord demo` does in a throwaway
temporary directory, reads back the board's own snapshot read model, and
writes a small static site -- nothing to install, nothing to run.

## Build it

```bash
python tools/export_static_board.py --out _site
```

This writes two files into `_site/`:

- `index.html` -- the board itself. It reads its data from a
  `<script type="application/json">` block embedded in the page, so opening
  the file directly from disk (`file://.../index.html`, no server, no
  network) renders the full board.
- `snapshot.json` -- the same data on its own, for anything that wants to
  read it without parsing it back out of the HTML.

Open `_site/index.html` in a browser to look at it.

## What it shows, and what it does not

Every row, agent session, and title in the export comes from
`coordharness.demo`'s fictional scenario -- a made-up team porting a made-up
payments service. Nothing on the page is a real project, a real person, or a
real credential, and the page says so at the top, with the date it was
exported.

The text filter above the table works -- it runs against the embedded JSON
in the browser, so it needs no server. Nothing else on the page claims to be
live: there is no button that looks like it claims work, runs an action, or
talks to a database, because none of that is possible without the
coordination server this export does not have. The one such control shown
(`Live actions`) is rendered disabled, with a note explaining why.

## Publishing

`.github/workflows/pages.yml` builds this bundle (and runs its tests) on
every push to `main` and every pull request that touches it, and uploads the
result as a Pages artifact. It never deploys on its own -- deployment runs
only from a manual `workflow_dispatch`, and the repository's Settings ->
Pages source has to be set to "GitHub Actions" first. See the comment at the
top of that workflow for the exact steps; publishing a live URL is a
decision for whoever owns the repository, not something this tool or its CI
does automatically.
