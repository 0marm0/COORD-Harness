from __future__ import annotations

import json
from pathlib import Path

from coordharness.lints import cwd_relative_paths as lint


def _scan(tmp_path: Path, monkeypatch, source: str) -> list:
    """Write `source` as a single scanned module and run scan_tree over it.

    REPO_ROOT/SRC_ROOT/ALLOWLIST_PATH are module globals bound at import
    time from the real project; tests redirect them into tmp_path so the
    scan never touches the real repo's allowlist file.
    """
    (tmp_path / "mod.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(lint, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lint, "SRC_ROOT", tmp_path)
    monkeypatch.setattr(lint, "ALLOWLIST_PATH", tmp_path / ".coordharness" / "allowlist.json")
    return lint.scan_tree(tmp_path)


def test_relative_path_constructor_is_flagged(tmp_path: Path, monkeypatch) -> None:
    findings = _scan(tmp_path, monkeypatch, 'from pathlib import Path\np = Path("data/file.txt")\n')

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule == "path_constructor"
    assert finding.literal == "data/file.txt"
    assert finding.allowlisted is False


def test_absolute_path_literal_is_not_flagged(tmp_path: Path, monkeypatch) -> None:
    findings = _scan(tmp_path, monkeypatch, 'from pathlib import Path\np = Path("/abs/data/file.txt")\n')

    assert findings == []


def test_home_relative_literal_is_not_flagged(tmp_path: Path, monkeypatch) -> None:
    findings = _scan(tmp_path, monkeypatch, 'from pathlib import Path\np = Path("~/file.txt")\n')

    assert findings == []


def test_sqlite_memory_sentinel_is_not_flagged(tmp_path: Path, monkeypatch) -> None:
    findings = _scan(tmp_path, monkeypatch, 'import sqlite3\nsqlite3.connect(":memory:")\n')

    assert findings == []


def test_read_csv_and_connect_calls_are_flagged(tmp_path: Path, monkeypatch) -> None:
    source = (
        "import pandas as pd\n"
        "import sqlite3\n"
        "pd.read_csv('rel/data.csv')\n"
        "sqlite3.connect('rel/coord.db')\n"
    )
    findings = _scan(tmp_path, monkeypatch, source)

    rules = sorted(f.rule for f in findings)
    assert rules == ["connect_call", "read_call"]


def test_allowlisted_hit_is_marked_allowlisted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(lint, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lint, "SRC_ROOT", tmp_path)
    allowlist_path = tmp_path / ".coordharness" / "allowlist.json"
    monkeypatch.setattr(lint, "ALLOWLIST_PATH", allowlist_path)
    (tmp_path / "mod.py").write_text('from pathlib import Path\np = Path("data/file.txt")\n')
    allowlist_path.parent.mkdir(parents=True, exist_ok=True)
    allowlist_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "file": "mod.py",
                        "rule": "path_constructor",
                        "literal": "data/file.txt",
                        "class": "fixture",
                        "reason": "test fixture path",
                    }
                ]
            }
        )
    )

    findings = lint.scan_tree(tmp_path)

    assert len(findings) == 1
    assert findings[0].allowlisted is True
    assert findings[0].allow_class == "fixture"
    assert findings[0].allow_reason == "test fixture path"


def test_scan_tree_on_directory_with_no_python_files_is_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(lint, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lint, "SRC_ROOT", tmp_path)
    monkeypatch.setattr(lint, "ALLOWLIST_PATH", tmp_path / ".coordharness" / "allowlist.json")

    assert lint.scan_tree(tmp_path) == []


def test_scan_tree_on_nonexistent_root_is_empty(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(lint, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lint, "SRC_ROOT", tmp_path)
    monkeypatch.setattr(lint, "ALLOWLIST_PATH", tmp_path / ".coordharness" / "allowlist.json")

    # Path.rglob on a nonexistent directory yields nothing rather than raising.
    assert lint.scan_tree(missing) == []
