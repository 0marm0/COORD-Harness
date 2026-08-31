# Safety primitives and `coord doctor`

Status: implemented as a portable, read-only public check surface.

`coord doctor` emits one JSON document using the stable
`coordharness.doctor.v1` schema. Its overall status and every finding are either
`PASS` or `BLOCKED`; a blocked report exits with status 2. The command never
creates a state directory, bootstraps or migrates a database, repairs rows,
stages files, or writes projections.

If the database has a non-empty live SQLite WAL, the doctor blocks instead of
opening the source and potentially creating or changing shared-memory sidecars. Run it
against a checkpointed snapshot when a source-current ruling is required.

## Running it

The shortest form takes no arguments at all and uses the same defaults every other
`coord` verb uses — `.coordharness/coord.db` under the current project root:

```console
$ coord doctor
{"schema": "coordharness.doctor.v1", "status": "PASS", "read_only": true, ...}
$ echo $?
0
```

It exits 0 on a board freshly seeded by `python -m coordharness.demo`, and it still
exits 0 after a full `claim` → `done` cycle on that board.

Each root can be named explicitly. `--mcp-config` is optional and only worth passing
when the project actually has a client configuration to inventory; this repository's
file is `.mcp.json`:

```console
coord --db .coordharness/coord.db doctor \
  --project-root . \
  --state-root .coordharness \
  --mcp-config .mcp.json
```

Naming a config path that does not exist is itself a blocking condition
(`mcp.untrusted_config_path`), so do not pass the flag speculatively.

### How `--db` is resolved

The global `--db` is resolved the same way every other verb resolves it: a relative
path is taken against the **working directory**, not against the state root. It is
then normalised before any containment test, so a path reaching the database through
`..` is judged on where it actually lands rather than on how it was spelled.

The containment test still fails closed. A database that is genuinely absent is
reported as absent. A database that exists but sits outside the trusted state root is
reported as exactly that, and is not opened. This is a real finding, captured from a
`coord doctor` run against a database deliberately pointed outside its state root —
not hand-written:

```json
{
  "id": "doctor.schema",
  "status": "BLOCKED",
  "summary": "coordination database is outside the trusted state root",
  "details": {
    "database_present": true,
    "database_outside_state_root": true,
    "state_root_ref": "project://.coordharness"
  },
  "code": "doctor.schema.outside_state_root",
  "remediation": "pass a matching `--state-root`, or keep the database at the default `.coordharness/coord.db` under the project root",
  "remediations": [
    {
      "code": "doctor.schema.outside_state_root",
      "summary": "the database exists but sits outside the state root doctor was given",
      "action": "pass a matching `--state-root`, or keep the database at the default `.coordharness/coord.db` under the project root"
    }
  ]
}
```

`state_root_ref` is a public reference, never an absolute host path. The distinction
matters because "outside the state root" and "does not exist" call for different
repairs, and a report that conflates them sends the reader looking for a missing file
that is sitting right there.

Every finding carries a stable `code` under the same `coordharness.doctor.v1` schema
identifier — `<finding-id>.ok` (for example `doctor.schema.ok`) when the finding is
`PASS`, and a specific `<finding-id>.<reason>` code such as
`doctor.schema.outside_state_root` above when it is `BLOCKED`. `remediation` (a single
string) and `remediations` (the full, possibly multi-entry list each finding can carry)
are present only on a non-`PASS` finding; a `PASS` finding always reports
`"remediation": null` and `"remediations": []`.

## Findings

The stable finding IDs are:

- `doctor.schema`: SQLite integrity, required objects, migration presence, and
  packaged migration checksums, plus the database-location conditions above.
- `doctor.lifecycle_writers`: static inventory of direct lifecycle SQL writers
  against the public package allowlist.
- `doctor.leases_reviews`: expired held claims/sessions, running work without a
  held claim, and unresolved review requests on terminal work.
- `doctor.jobs_projection`: sidecar identity/binding, symlink and JSON safety,
  live-run sidecar containment, and readable projection views. Every job sidecar
  must name a work row that exists, which is what `coord-jobs launch` produces.
- `doctor.public_paths`: containment for every context, completion, and artifact
  pointer, and existence for every pointer that should already have been produced.
- `doctor.mcp_security`: opt-in MCP configuration inventory. All argument and
  environment values are redacted. Literal secret-like values, shell commands,
  unpinned package launchers, invalid files, and config paths outside the
  project/state roots block the report.
- `doctor.db_file_modes`: the database and its `-wal`/`-shm` sidecars are not
  readable by other accounts (`0600`); a group- or world-readable mode blocks
  the report. Read-only: this check stats and reports, it never chmods.
- `doctor.board_port`: a cheap bind probe of the configured `coord-board` port
  on the loopback interface. The port is read the same way `coord-board`
  reads it (`COORD_BOARD_PORT`, falling back to its packaged default) — never
  hardcoded. Only a bind failure whose errno is address-in-use blocks the
  report; any other bind failure (a permission-denied low port, no loopback
  route) is not treated as a port conflict.

### What `doctor.public_paths` counts

Containment is the property being proven, and it is proven whether or not the file
exists yet: the nearest existing ancestor is resolved through symlinks before the
missing suffix is re-appended. So a pointer falls into one of three counted buckets.

| Bucket | Meaning | Blocks |
|---|---|---|
| `invalid_pointer_fields` | traversal, an unknown URI scheme, a path escaping the project root, or a **terminal** row whose declared proof is missing | yes |
| `pending_pointer_fields` | a non-terminal row that declares a contained proof it has not produced yet | no |
| `absolute_pointer_fields` | an artifact path stored as an absolute path by an earlier writer, whose containment and existence are both still proven | no |

Declaring where a report will land before the directory exists is the normal case —
`coord create --done-signal docs/reports/x.md` in a project with no `docs/reports/`
yet — and it is pending, not invalid. The doctor never creates that directory; it
writes nothing.

The absolute bucket exists for recovery, not tolerance. Completion proofs are now
stored as the repository-relative path the row declared, but a database that
completed work under the earlier writer holds absolute artifact paths, and those rows
are read as contained-and-present rather than leaving the check permanently blocked.
An absolute path that escapes the project root still blocks, and `done_signal` and
`context_pack_ref` remain absolute-intolerant.

## The rest of `coordharness.safety`

The reusable `coordharness.safety` package also provides traversal/symlink-safe
path resolution and an exact-path shared-index commit guard. The commit guard
refuses a pre-populated index, stages only explicit file paths, verifies the
exact staged set, commits with `--only`, and on failure unstages only its own
allowlist.

Deployment-specific authority activation and generic-versus-strict MCP writer
profiles are intentionally excluded from this packet. The doctor inventories
configuration and existing lifecycle writers; it does not grant or change
writer authority.
