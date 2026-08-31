#!/bin/bash
# MCP launcher for a genuinely fresh clone. Claude Code and Codex launch this before
# either agent's instructions file is read, so a missing .venv must fail with a plain
# instruction here rather than a raw ENOENT from the interpreter itself.
set -euo pipefail

if [ -x "./.venv/bin/python" ]; then
  exec ./.venv/bin/python -m coordharness.coord.mcp_coord_server "$@"
fi

echo "coordharness: this clone is not set up yet. Run ./scripts/setup.sh, then restart this session." >&2
exit 1
