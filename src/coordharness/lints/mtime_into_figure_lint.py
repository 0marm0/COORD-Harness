
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

from coordharness import config as _harness_config

UI_ROOT = _harness_config.project_root() / "src"

PRODUCERS = {"as_of"}
FIG_OWNERS = {"Figure", "UnknownAsOf"}
FIG_FUNCS = {"figure", "figure_for_key"}


def _calls_producer(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if name in PRODUCERS:
                return True
    return False


def scan_file(path: Path, root: Path) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    rows: list[dict[str, Any]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        tainted: set[str] = set()
        for _ in range(3):
            for sub in ast.walk(fn):
                if not isinstance(sub, ast.Assign):
                    continue
                val = sub.value
                if _calls_producer(val) or any(
                    isinstance(n, ast.Name) and n.id in tainted for n in ast.walk(val)
                ):
                    for target in sub.targets:
                        if isinstance(target, ast.Name):
                            tainted.add(target.id)
        for sub in ast.walk(fn):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            owner = (
                f.value.id
                if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                else None
            )
            fname = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            if owner not in FIG_OWNERS and fname not in FIG_FUNCS:
                continue
            for kw in sub.keywords:
                if kw.arg != "as_of":
                    continue
                expr = kw.value
                if _calls_producer(expr) or any(
                    isinstance(n, ast.Name) and n.id in tainted
                    for n in ast.walk(expr)
                ):
                    rows.append({
                        "file": str(path.relative_to(root)),
                        "line": sub.lineno,
                        "func": fn.name,
                        "call": f"{owner + '.' if owner else ''}{fname}",
                        "as_of_expr": ast.unparse(expr),
                    })
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", default="")
    ap.add_argument("--root", default=str(UI_ROOT))
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rows.extend(scan_file(path, root))
    rows.sort(key=lambda r: (r["file"], r["line"]))

    print(f"MTIME-INTO-FIGURE SITES: {len(rows)}")
    for r in rows:
        print(f"  {r['file']}:{r['line']}  {r['func']}  "
              f"{r['call']}(as_of={r['as_of_expr']})")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return 1 if rows else 0


if __name__ == "__main__":
    sys.exit(main())
