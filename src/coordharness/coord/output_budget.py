
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any


ENV_FLAG = "COORD_OUTPUT_BUDGET"
INLINE_OUTPUT_LIMIT = 12_000


def output_budget_enabled(env: dict[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    raw = str(source.get(ENV_FLAG, "1") or "").strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def apply_output_budget(
    text: str,
    *,
    inline_limit: int = INLINE_OUTPUT_LIMIT,
    artifact_dir: str | Path | None = None,
    artifact_prefix: str = "policy-output",
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    content = str(text or "")
    encoded = content.encode("utf-8")
    enabled = output_budget_enabled(env)
    if not enabled or len(encoded) <= inline_limit:
        return {
            "text": content,
            "truncated": False,
            "bytes": len(encoded),
            "inline_limit": inline_limit,
            "artifact_ref": None,
            "enabled": enabled,
            "env_flag": ENV_FLAG,
        }
    digest = hashlib.sha256(encoded).hexdigest()[:16]
    artifact_ref = None
    if artifact_dir is not None:
        path = Path(artifact_dir) / f"{artifact_prefix}-{digest}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        artifact_ref = str(path)
    clipped = encoded[: max(0, inline_limit)].decode("utf-8", errors="ignore")
    return {
        "text": clipped,
        "truncated": True,
        "bytes": len(encoded),
        "inline_limit": inline_limit,
        "artifact_ref": artifact_ref,
        "enabled": enabled,
        "env_flag": ENV_FLAG,
    }
