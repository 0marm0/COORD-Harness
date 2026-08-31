#!/usr/bin/env bash
# Renamed to scripts/setup.sh (OS-agnostic; --native/--register-clients are opt-in
# there now). This shim exists only so old doc references keep working.
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/setup.sh" "$@"
