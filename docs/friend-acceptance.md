# Friend acceptance

Use this on a fresh macOS account or a machine that has Python 3.11+, Git, Xcode,
XcodeGen, and network access for package dependencies. The script creates its own
temporary HOME, virtual environment, application staging directory, project, and
SQLite database. It never reads an existing COORD database and seeds synthetic
sample data only.

## One command

From the candidate checkout:

```bash
./scripts/friend_acceptance.sh --human
```

Set `COORD_FRIEND_ROOT` to an empty, dedicated directory if the receipt should
survive a reboot. Never point it at a home directory, repository, or existing
project.

## Automated preflight

Before asking the friend to judge appearance, the command must report that it:

- built both wheel and sdist and installed the wheel outside the checkout;
- found the installed `coord`, `coord-board`, `coord-jobs`, `coord-models`,
  and `coord-mcp` entry points under the fresh HOME;
- initialized a new SQLite database and populated only the fictional demo;
- ran CLI board and doctor checks;
- generated portable project config, idempotently registered every installed Codex and
  Claude client under a fresh HOME, and distinguished Claude's interactive approval
  gate from missing registration;
- completed a real MCP initialize, list-tools, and `preflight` handshake;
- started the board service and fetched the snapshot, web board, cockpit,
  Operations Atlas, and Swarm Mesh over loopback;
- built the release configurations of `CoordCockpitMac`, `CoordMenuBar`,
  `CoordCockpitWindow`, and the `CoordCockpitIOS` simulator scheme without
  signing; and
- uninstalled the Python distribution while preserving the database with the
  same SHA-256.

Any skipped requirement is not a pass. On a non-macOS host the script explicitly
skips native builds, so that run can support diagnosis but cannot accept the
macOS release.

## Human visual pass

The `--human` run pauses before uninstall. The friend should inspect, rather
than infer from the automated probes:

- The menu-bar mark appears, a normal click opens the menu, and the Cockpit
  action opens a usable window.
- Menu, native Cockpit, and web board show the same fictional rows and coherent
  Running, Attention, and Planned groupings.
- Search, row selection, filters, drawer/detail navigation, and keyboard focus
  remain visible and reversible.
- The web board, embedded/native cockpit, Operations Atlas, and Swarm Mesh are
  readable at the default window size and after resizing narrower.
- Empty, loading, disconnected, and error states do not expose stack traces,
  machine paths, tokens, or private data.
- Every visible person, project, work item, provider, and metric is plainly
  synthetic. Stop immediately if any real account, customer, repository, or
  prior project data appears.
- Closing the staged apps does not delete the synthetic database.

Type `PASS` only after those observations. The script then uninstalls the
Python package and prints the preserved database path, database SHA-256, run
root, and log path. The staged native applications are moved out of the isolated `Applications`
install location into a recoverable `uninstalled-native-apps` archive under the
run root; no copy is made to `/Applications`.

## Acceptance record

Record the candidate commit, candidate-manifest SHA-256, script output, host and
OS version, friend name or review identity, automated result, human result, and
any screenshots. Keep this record outside the candidate repository. A friend
pass is product acceptance evidence; it does not replace the signed external
history receipt or publication authorization.
