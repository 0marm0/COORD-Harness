#!/usr/bin/env python3
"""CI entry point for the repository documentation validator."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from validate_documentation import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main([str(ROOT)]))
