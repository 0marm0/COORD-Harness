#!/usr/bin/env python3

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any, NamedTuple

from coordharness import config as _harness_config

REPO_ROOT = _harness_config.project_root()
SRC_ROOT = REPO_ROOT
ALLOWLIST_PATH = _harness_config.state_dir() / "cwd_path_allowlist.json"

READ_ATTR_NAMES = frozenset({"read_parquet", "read_csv", "read_json", "read_excel"})
CONNECT_MODULES = frozenset({"sqlite3", "duckdb"})
MEMORY_SENTINELS = frozenset({":memory:"})


class Finding(NamedTuple):
    file: str
    line: int
    col: int
    rule: str
    literal: str
    allowlisted: bool
    allow_class: str
    allow_reason: str


def _is_relative_data_literal(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value.startswith("/"):
        return False
    if value.startswith("~"):
        return False
    if value in MEMORY_SENTINELS:
        return False
    return True


def _call_kind(node: ast.Call) -> tuple[str, ast.expr] | None:
    if not node.args:
        return None
    first = node.args[0]
    func = node.func

    if isinstance(func, ast.Name):
        if func.id == "Path":
            return "path_constructor", first
        if func.id == "open":
            return "read_call", first
        return None

    if isinstance(func, ast.Attribute):
        if func.attr in READ_ATTR_NAMES:
            return "read_call", first
        if func.attr == "connect" and isinstance(func.value, ast.Name) and func.value.id in CONNECT_MODULES:
            return "connect_call", first
        return None

    return None


def _scan_file(path: Path) -> list[tuple[str, int, int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        print(f"[lint] WARN unparseable {path}: {exc}", file=sys.stderr)
        return []

    hits: list[tuple[str, int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kind = _call_kind(node)
        if kind is None:
            continue
        rule, arg = kind
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and _is_relative_data_literal(arg.value):
            hits.append((rule, node.lineno, node.col_offset, arg.value))
    return hits


def _load_allowlist() -> dict[tuple[str, str, str], dict[str, Any]]:
    if not ALLOWLIST_PATH.exists():
        ALLOWLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        ALLOWLIST_PATH.write_text(json.dumps({"entries": []}, indent=2))
    payload = json.loads(ALLOWLIST_PATH.read_text())
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise SystemExit(f"[lint] FATAL allowlist malformed (no entries[]): {ALLOWLIST_PATH}")
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for e in entries:
        key = (str(e["file"]), str(e["rule"]), str(e["literal"]))
        out[key] = e
    return out


def scan_tree(root: Path) -> list[Finding]:
    allowlist = _load_allowlist()
    findings: list[Finding] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for rule, line, col, literal in _scan_file(path):
            key = (rel, rule, literal)
            entry = allowlist.get(key)
            findings.append(
                Finding(
                    file=rel,
                    line=line,
                    col=col,
                    rule=rule,
                    literal=literal,
                    allowlisted=entry is not None,
                    allow_class=str(entry.get("class", "")) if entry else "",
                    allow_reason=str(entry.get("reason", "")) if entry else "",
                )
            )
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument(
        "--explain",
        action="store_true",
        help="print every scanned hit (allowlisted and not) and always exit 0",
    )
    args = ap.parse_args()

    if not SRC_ROOT.exists():
        print(f"[lint] FATAL scan root not found: {SRC_ROOT}", file=sys.stderr)
        return 2

    try:
        findings = scan_tree(SRC_ROOT)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"[lint] FATAL {exc}", file=sys.stderr)
        return 2

    violations = [f for f in findings if not f.allowlisted]
    modules_scanned = sum(1 for _ in SRC_ROOT.rglob("*.py"))
    shown = findings if args.explain else violations

    if args.json:
        print(
            json.dumps(
                {
                    "schema": "coordharness.cwd-relative-path-lint.v1",
                    "scan_root": str(SRC_ROOT.relative_to(REPO_ROOT)),
                    "modules_scanned": modules_scanned,
                    "total_hits": len(findings),
                    "allowlisted_count": len(findings) - len(violations),
                    "violation_count": len(violations),
                    "findings": [f._asdict() for f in shown],
                },
                indent=2,
            )
        )
    else:
        print(f"cwd-relative-path lint - {modules_scanned} modules scanned under {SRC_ROOT.relative_to(REPO_ROOT)}")
        print(
            f"{len(findings)} literal-relative-path hit(s) - "
            f"{len(findings) - len(violations)} allowlisted - {len(violations)} violation(s)"
        )
        for f in shown:
            tag = "ALLOWLISTED" if f.allowlisted else "VIOLATION"
            print(f"\n{tag} [{f.rule}] {f.file}:{f.line}:{f.col}")
            print(f"    literal : {f.literal!r}")
            if f.allowlisted:
                print(f"    class   : {f.allow_class}")
                print(f"    reason  : {f.allow_reason}")
        if not violations:
            print("\nPASS - no un-allowlisted cwd-relative literal path reads")
        else:
            print(
                f"\nFAIL - {len(violations)} un-allowlisted cwd-relative literal path read(s) - "
                "review each site and either anchor the path or add a reviewed allowlist entry"
            )

    if args.explain:
        return 0
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
