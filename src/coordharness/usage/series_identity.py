"""Opaque account scope for retained usage; never returns or persists credentials."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Sequence


def codex_source_identity(home: Path | str, command: Sequence[str] = ()) -> str | None:
    """Bind cache to config directory and stable account metadata, not rotating tokens.

    A missing/malformed auth file is unknown: it cannot authorize disk reuse.
    Only auth mode and tokens.account_id metadata are inspected; token values are
    never inspected, logged, hashed, or returned. API-key accounts have no
    provider daily series and therefore cannot reuse a ChatGPT account cache.
    """
    config = Path(os.environ.get("CODEX_HOME", str(Path(home) / ".codex")))
    for arg in command:
        if arg.startswith("CODEX_HOME="):
            config = Path(arg.split("=", 1)[1])
    try:
        path = config / "auth.json"
        if path.is_symlink() or path.stat().st_size > 256 * 1024:
            return None
        raw = json.loads(path.read_bytes())
        mode = raw.get("auth_mode") if isinstance(raw, dict) else None
        if mode is not None and (not isinstance(mode, str) or mode.lower() != "chatgpt"):
            return None
        tokens = raw.get("tokens") if isinstance(raw, dict) else None
        account_id = tokens.get("account_id") if isinstance(tokens, dict) else None
        if not isinstance(account_id, str) or not account_id.strip() or len(account_id) > 1024:
            return None
        scope = "codex-account-usage-v1\0" + str(config.resolve()) + "\0" + account_id
        return hashlib.sha256(scope.encode()).hexdigest()
    except (OSError, ValueError, RecursionError):
        return None
