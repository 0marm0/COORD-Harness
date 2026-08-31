from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordharness.lints import fail_loud_patterns as lint


def _write_and_scan(tmp_path: Path, monkeypatch, source: str, *, rel: str = "mod.py"):
    monkeypatch.setattr(lint, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lint, "SRC_ROOT", tmp_path)
    monkeypatch.setattr(lint, "ALLOWLIST_PATH", tmp_path / ".coordharness" / "allowlist.json")
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return lint.scan_file(path)


# --- P1: swallowed except wrapping a data-load call -----------------------


def test_p1_flags_bare_except_around_data_load(tmp_path: Path, monkeypatch) -> None:
    source = (
        "def f():\n"
        "    try:\n"
        "        data = open('x').read()\n"
        "    except Exception:\n"
        "        data = None\n"
        "    return data\n"
    )
    findings = _write_and_scan(tmp_path, monkeypatch, source)

    p1 = [f for f in findings if f.pattern == "P1"]
    assert len(p1) == 1
    assert p1[0].rule == "except_exception"


def test_p1_does_not_flag_handler_that_reraises(tmp_path: Path, monkeypatch) -> None:
    source = (
        "def f():\n"
        "    try:\n"
        "        data = open('x').read()\n"
        "    except Exception:\n"
        "        raise\n"
        "    return data\n"
    )
    findings = _write_and_scan(tmp_path, monkeypatch, source)

    assert [f for f in findings if f.pattern == "P1"] == []


# --- P2: .get(key, <plausible default>) on a non-config receiver ----------


def test_p2_flags_get_with_zero_default_on_plain_receiver(tmp_path: Path, monkeypatch) -> None:
    source = "def f(result):\n    return result.get('n', 0)\n"
    findings = _write_and_scan(tmp_path, monkeypatch, source)

    p2 = [f for f in findings if f.pattern == "P2"]
    assert len(p2) == 1
    assert p2[0].rule == "zero_int"


def test_p2_does_not_flag_environ_receiver(tmp_path: Path, monkeypatch) -> None:
    source = "import os\ndef f():\n    return os.environ.get('PATH', '')\n"
    findings = _write_and_scan(tmp_path, monkeypatch, source)

    assert [f for f in findings if f.pattern == "P2"] == []


def test_p2_does_not_flag_non_plausible_default(tmp_path: Path, monkeypatch) -> None:
    source = "def f(result):\n    return result.get('n', -1)\n"
    findings = _write_and_scan(tmp_path, monkeypatch, source)

    assert [f for f in findings if f.pattern == "P2"] == []


# --- P3: measured-quantity zero-fill ---------------------------------------


def test_p3_flags_fillna_zero(tmp_path: Path, monkeypatch) -> None:
    source = "def f(df):\n    return df['x'].fillna(0)\n"
    findings = _write_and_scan(tmp_path, monkeypatch, source)

    p3 = [f for f in findings if f.pattern == "P3"]
    assert len(p3) == 1
    assert p3[0].rule == "fillna_zero"


def test_p3_does_not_flag_fillna_nonzero(tmp_path: Path, monkeypatch) -> None:
    source = "def f(df):\n    return df['x'].fillna(-1)\n"
    findings = _write_and_scan(tmp_path, monkeypatch, source)

    assert [f for f in findings if f.pattern == "P3"] == []


def test_p3_flags_sql_coalesce_zero_literal(tmp_path: Path, monkeypatch) -> None:
    source = 'def f():\n    return "SELECT COALESCE(amount, 0) FROM t"\n'
    findings = _write_and_scan(tmp_path, monkeypatch, source)

    p3 = [f for f in findings if f.pattern == "P3"]
    assert len(p3) == 1
    assert p3[0].rule == "sql_coalesce_zero"


# --- P4: cached/memoized result with no load-success check ----------------


def test_p4_flags_lru_cache_falsy_guard_returning_empty(tmp_path: Path, monkeypatch) -> None:
    source = (
        "from functools import lru_cache\n\n"
        "@lru_cache\n"
        "def get_data(cond, real_data):\n"
        "    if not cond:\n"
        "        return []\n"
        "    return real_data\n"
    )
    findings = _write_and_scan(tmp_path, monkeypatch, source)

    p4 = [f for f in findings if f.pattern == "P4"]
    assert len(p4) == 1
    assert p4[0].rule == "lru_cache_falsy_guard"


def test_p4_does_not_flag_falsy_guard_without_cache_decorator(tmp_path: Path, monkeypatch) -> None:
    source = (
        "def get_data(cond, real_data):\n"
        "    if not cond:\n"
        "        return []\n"
        "    return real_data\n"
    )
    findings = _write_and_scan(tmp_path, monkeypatch, source)

    assert [f for f in findings if f.pattern == "P4"] == []


# --- P5: if not X: return <empty> on a render/serve path ------------------


def test_p5_flags_falsy_guard_empty_return_under_api_path(tmp_path: Path, monkeypatch) -> None:
    source = "def handler(x):\n    if not x:\n        return None\n    return x\n"
    findings = _write_and_scan(tmp_path, monkeypatch, source, rel="api/handler.py")

    p5 = [f for f in findings if f.pattern == "P5"]
    assert len(p5) == 1
    assert p5[0].rule == "falsy_guard_empty_return"


def test_p5_does_not_flag_same_pattern_outside_serving_path(tmp_path: Path, monkeypatch) -> None:
    source = "def handler(x):\n    if not x:\n        return None\n    return x\n"
    findings = _write_and_scan(tmp_path, monkeypatch, source, rel="lib/handler.py")

    assert [f for f in findings if f.pattern == "P5"] == []


# --- scan_file / scan_tree plumbing ----------------------------------------


def test_scan_file_on_empty_file_returns_nothing(tmp_path: Path, monkeypatch) -> None:
    findings = _write_and_scan(tmp_path, monkeypatch, "")

    assert findings == []


def test_scan_file_on_nonexistent_path_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(lint, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lint, "SRC_ROOT", tmp_path)

    with pytest.raises(FileNotFoundError):
        lint.scan_file(tmp_path / "missing.py")


def test_scan_tree_on_nonexistent_root_is_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(lint, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lint, "SRC_ROOT", tmp_path)
    monkeypatch.setattr(lint, "ALLOWLIST_PATH", tmp_path / ".coordharness" / "allowlist.json")

    assert lint.scan_tree(tmp_path / "does-not-exist") == []


def test_scan_tree_marks_allowlisted_entries(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(lint, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(lint, "SRC_ROOT", tmp_path)
    allowlist_path = tmp_path / ".coordharness" / "allowlist.json"
    monkeypatch.setattr(lint, "ALLOWLIST_PATH", allowlist_path)
    (tmp_path / "mod.py").write_text("def f(df):\n    return df['x'].fillna(0)\n")
    allowlist_path.parent.mkdir(parents=True, exist_ok=True)
    allowlist_path.write_text(
        json.dumps({"entries": [{"file": "mod.py", "pattern": "P3", "line": 2, "reason": "reviewed"}]})
    )

    findings = lint.scan_tree(tmp_path)

    assert len(findings) == 1
    assert findings[0].allowlisted is True
    assert findings[0].allow_reason == "reviewed"
