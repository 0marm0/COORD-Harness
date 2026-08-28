#!/usr/bin/env python3

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple, Optional

from coordharness import config as _harness_config

REPO_ROOT = _harness_config.project_root()
SRC_ROOT = REPO_ROOT
ALLOWLIST_PATH = _harness_config.state_dir() / "fail_loud_lint_allowlist.json"

DATA_LOAD_CALL_NAMES = frozenset(
    {
        "open",
        "read_parquet",
        "read_csv",
        "read_json",
        "read_excel",
        "read_sql",
        "read_sql_query",
        "read_table",
        "read_pickle",
        "read_feather",
        "read_html",
        "connect",
        "execute",
        "executemany",
        "fetchone",
        "fetchall",
        "fetchmany",
        "urlopen",
        "urlretrieve",
        "read_text",
        "read_bytes",
        "load_rows",
        "load_summary",
    }
)

DENY_RECEIVER_SUBSTR = (
    "environ",
    "request",
    "kwargs",
    "kwarg",
    "headers",
    "query_params",
    "cookies",
    "session",
    "argv",
    "getenv",
)

CACHE_DECORATOR_NAMES = frozenset({"lru_cache", "cache"})
SERVE_PATH_MARKERS = ("/ui/", "/api/")
COALESCE_ZERO_RE = re.compile(r"coalesce\s*\([^)]*,\s*0(?:\.0)?\s*\)", re.IGNORECASE)

PATTERN_LABELS = {
    "P1": "swallowed except wrapping a data-load call",
    "P2": ".get(key, <plausible default>) on a non-config receiver",
    "P3": "measured-quantity zero-fill (fillna/replace/COALESCE)",
    "P4": "cached/memoized result with no load-success check",
    "P5": "if not X: return <empty> on a render/serve path",
}


class Finding(NamedTuple):
    file: str
    line: int
    col: int
    pattern: str
    rule: str
    detail: str
    allowlisted: bool
    allow_reason: str


def _handler_has_raise(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(n, ast.Raise) for n in ast.walk(handler))


def _handler_exits_unconditionally(handler: ast.ExceptHandler) -> bool:
    if not handler.body:
        return False
    return isinstance(handler.body[-1], (ast.Return, ast.Continue, ast.Break))


WRITE_SQL_PREFIXES = (
    "insert",
    "update",
    "delete",
    "create",
    "drop",
    "alter",
    "replace",
)


def _open_call_is_write_mode(call: ast.Call) -> bool:
    mode_node: ast.expr | None = None
    if len(call.args) >= 2:
        mode_node = call.args[1]
    for kw in call.keywords:
        if kw.arg == "mode":
            mode_node = kw.value
    if mode_node is None:
        return False
    if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
        mode = mode_node.value
        return bool(mode) and mode[0] in ("w", "a", "x")
    return False


def _execute_call_is_write_sql(call: ast.Call) -> bool:
    if not call.args:
        return False
    arg0 = call.args[0]
    text: str | None = None
    if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
        text = arg0.value
    elif isinstance(arg0, ast.JoinedStr) and arg0.values:
        first = arg0.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            text = first.value
    if text is None:
        return False
    return text.strip().lower().startswith(WRITE_SQL_PREFIXES)


def _contains_dataload_call(stmts: list[ast.stmt]) -> bool:
    for stmt in stmts:
        for node in ast.walk(stmt):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            else:
                continue
            if name not in DATA_LOAD_CALL_NAMES:
                continue
            if name == "open" and _open_call_is_write_mode(node):
                continue
            if name in ("execute", "executemany") and _execute_call_is_write_sql(
                node
            ):
                continue
            return True
    return False


def _dotted(node: ast.expr) -> str:
    parts: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _returns_plausible_empty(stmt: ast.stmt) -> bool:
    if not isinstance(stmt, ast.Return) or stmt.value is None:
        return False
    v = stmt.value
    if isinstance(v, ast.Constant) and (v.value is None or v.value == ""):
        return True
    if isinstance(v, ast.List) and not v.elts:
        return True
    if isinstance(v, ast.Dict) and not v.keys:
        return True
    if isinstance(v, ast.Tuple) and not v.elts:
        return True
    return False


def _is_falsy_guard(test: ast.expr) -> bool:
    return isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)


def _all_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _scan_p1(tree: ast.Module, rel: str) -> list[Finding]:
    hits: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if not _contains_dataload_call(node.body):
            continue
        for h in node.handlers:
            is_bare = h.type is None
            is_exception = (
                isinstance(h.type, ast.Name) and h.type.id == "Exception"
            ) or (
                isinstance(h.type, ast.Tuple)
                and any(
                    isinstance(e, ast.Name) and e.id == "Exception" for e in h.type.elts
                )
            )
            if not (is_bare or is_exception):
                continue
            if _handler_has_raise(h):
                continue
            hits.append(
                Finding(
                    file=rel,
                    line=h.lineno,
                    col=h.col_offset,
                    pattern="P1",
                    rule="bare_except" if is_bare else "except_exception",
                    detail=(
                        f"except {'<bare>' if is_bare else 'Exception'} at line {h.lineno} "
                        f"wraps a data-load call (try body starts line {node.body[0].lineno}) "
                        "with no re-raise"
                    ),
                    allowlisted=False,
                    allow_reason="",
                )
            )
    return hits


def _classify_default(node: ast.expr) -> Optional[str]:
    if isinstance(node, ast.Constant):
        if node.value is None:
            return "none"
        if isinstance(node.value, bool):
            return None
        if isinstance(node.value, int) and node.value == 0:
            return "zero_int"
        if isinstance(node.value, float) and node.value == 0.0:
            return "zero_float"
        if isinstance(node.value, str) and node.value == "":
            return "empty_str"
        return None
    if isinstance(node, ast.List) and not node.elts:
        return "empty_list"
    if isinstance(node, ast.Dict) and not node.keys:
        return "empty_dict"
    return None


def _scan_p2(tree: ast.Module, rel: str) -> list[Finding]:
    hits: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "get"):
            continue
        if len(node.args) != 2 or node.keywords:
            continue
        key_arg, default_arg = node.args
        kind = _classify_default(default_arg)
        if kind is None:
            continue
        receiver = _dotted(func.value).lower()
        if any(s in receiver for s in DENY_RECEIVER_SUBSTR):
            continue
        try:
            key_repr = (
                repr(ast.literal_eval(key_arg))
                if isinstance(key_arg, ast.Constant)
                else "<dynamic key>"
            )
        except (ValueError, TypeError):
            key_repr = "<dynamic key>"
        hits.append(
            Finding(
                file=rel,
                line=node.lineno,
                col=node.col_offset,
                pattern="P2",
                rule=kind,
                detail=f"{receiver or '<expr>'}.get({key_repr}, <{kind}>) — verify the field is genuinely optional",
                allowlisted=False,
                allow_reason="",
            )
        )
    return hits


def _scan_p3(tree: ast.Module, rel: str) -> list[Finding]:
    hits: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "fillna":
                val: ast.expr | None = node.args[0] if node.args else None
                if val is None:
                    for kw in node.keywords:
                        if kw.arg == "value":
                            val = kw.value
                if (
                    isinstance(val, ast.Constant)
                    and not isinstance(val.value, bool)
                    and val.value in (0, 0.0)
                ):
                    hits.append(
                        Finding(
                            file=rel,
                            line=node.lineno,
                            col=node.col_offset,
                            pattern="P3",
                            rule="fillna_zero",
                            detail="fillna(0) — confirm the receiver is a measured quantity, not a legitimate zero-count",
                            allowlisted=False,
                            allow_reason="",
                        )
                    )
            elif node.func.attr == "replace" and len(node.args) >= 2:
                first_is_nan = "nan" in ast.dump(node.args[0]).lower()
                second = node.args[1]
                second_is_zero = isinstance(second, ast.Constant) and (
                    not isinstance(second.value, bool)
                ) and second.value in (0, 0.0)
                if first_is_nan and second_is_zero:
                    hits.append(
                        Finding(
                            file=rel,
                            line=node.lineno,
                            col=node.col_offset,
                            pattern="P3",
                            rule="replace_nan_zero",
                            detail=".replace(nan, 0) — confirm the receiver is a measured quantity",
                            allowlisted=False,
                            allow_reason="",
                        )
                    )
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and COALESCE_ZERO_RE.search(node.value)
        ):
            hits.append(
                Finding(
                    file=rel,
                    line=node.lineno,
                    col=node.col_offset,
                    pattern="P3",
                    rule="sql_coalesce_zero",
                    detail="SQL COALESCE(x, 0) literal — confirm x is a measured quantity, not a legitimate zero-count",
                    allowlisted=False,
                    allow_reason="",
                )
            )
    return hits


def _decorator_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for d in fn.decorator_list:
        node = d.func if isinstance(d, ast.Call) else d
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    return names


def _scan_p4(tree: ast.Module, rel: str) -> list[Finding]:
    hits: list[Finding] = []
    for fn in _all_functions(tree):
        body = fn.body

        deco = _decorator_names(fn)
        if deco & CACHE_DECORATOR_NAMES:
            tag = "/".join(sorted(deco & CACHE_DECORATOR_NAMES))
            for stmt in ast.walk(fn):
                if (
                    isinstance(stmt, ast.If)
                    and _is_falsy_guard(stmt.test)
                    and stmt.body
                ):
                    if _returns_plausible_empty(stmt.body[0]):
                        hits.append(
                            Finding(
                                file=rel,
                                line=stmt.lineno,
                                col=stmt.col_offset,
                                pattern="P4",
                                rule="lru_cache_falsy_guard",
                                detail=(
                                    f"{fn.name}() is @{tag}-memoized; its falsy-guard branch returns a "
                                    "plausible-empty value that is then cached as if real"
                                ),
                                allowlisted=False,
                                allow_reason="",
                            )
                        )
                if isinstance(stmt, ast.Try):
                    for h in stmt.handlers:
                        if _handler_has_raise(h):
                            continue
                        if any(_returns_plausible_empty(s) for s in h.body):
                            hits.append(
                                Finding(
                                    file=rel,
                                    line=h.lineno,
                                    col=h.col_offset,
                                    pattern="P4",
                                    rule="lru_cache_swallowed_except",
                                    detail=(
                                        f"{fn.name}() is @{tag}-memoized; its except handler swallows and "
                                        "returns a plausible-empty value that is then cached as if real"
                                    ),
                                    allowlisted=False,
                                    allow_reason="",
                                )
                            )

        global_names: set[str] = set()
        for stmt in body:
            if isinstance(stmt, ast.Global):
                global_names.update(stmt.names)
        if not global_names:
            continue

        guard_names: set[str] = set()
        for stmt in body:
            if not (isinstance(stmt, ast.If) and isinstance(stmt.test, ast.Compare)):
                continue
            test = stmt.test
            if not (isinstance(test.left, ast.Name) and test.left.id in global_names):
                continue
            if not (len(test.ops) == 1 and isinstance(test.ops[0], ast.IsNot)):
                continue
            if stmt.body and isinstance(stmt.body[0], ast.Return):
                rv = stmt.body[0].value
                if isinstance(rv, ast.Name) and rv.id == test.left.id:
                    guard_names.add(test.left.id)

        if not guard_names:
            continue

        for idx, stmt in enumerate(body):
            if not isinstance(stmt, ast.Try):
                continue
            swallow = any(
                (not _handler_has_raise(h)) and (not _handler_exits_unconditionally(h))
                for h in stmt.handlers
            )
            if not swallow:
                continue
            for later in body[idx + 1 :]:
                if isinstance(later, ast.Assign):
                    for tgt in later.targets:
                        names_in_tgt: list[str] = []
                        if isinstance(tgt, ast.Name):
                            names_in_tgt = [tgt.id]
                        elif isinstance(tgt, ast.Tuple):
                            names_in_tgt = [
                                e.id for e in tgt.elts if isinstance(e, ast.Name)
                            ]
                        hit_names = guard_names & set(names_in_tgt)
                        for name in hit_names:
                            hits.append(
                                Finding(
                                    file=rel,
                                    line=later.lineno,
                                    col=later.col_offset,
                                    pattern="P4",
                                    rule="manual_guard_swallow_then_cache",
                                    detail=(
                                        f"'{name}' memoization guard in {fn.name}() is reassigned "
                                        f"unconditionally after a swallowed try/except (try at line {stmt.lineno}) "
                                        "with no check that the load succeeded"
                                    ),
                                    allowlisted=False,
                                    allow_reason="",
                                )
                            )
                if isinstance(later, (ast.Return, ast.Raise)):
                    break
    return hits


def _is_serving_file(rel: str) -> bool:
    probe = "/" + rel
    return any(marker in probe for marker in SERVE_PATH_MARKERS)


def _scan_p5(tree: ast.Module, rel: str) -> list[Finding]:
    if not _is_serving_file(rel):
        return []
    hits: list[Finding] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If) and _is_falsy_guard(node.test) and node.body):
            continue
        if not _returns_plausible_empty(node.body[0]):
            continue
        hits.append(
            Finding(
                file=rel,
                line=node.lineno,
                col=node.col_offset,
                pattern="P5",
                rule="falsy_guard_empty_return",
                detail="if not <cond>: return <empty> on a file under ui/ or api/ — verify this is genuinely empty, not a failed load",
                allowlisted=False,
                allow_reason="",
            )
        )
    return hits


PATTERN_SCANNERS = {
    "P1": _scan_p1,
    "P2": _scan_p2,
    "P3": _scan_p3,
    "P4": _scan_p4,
    "P5": _scan_p5,
}


def _load_allowlist() -> dict[tuple[str, str, int], dict[str, Any]]:
    if not ALLOWLIST_PATH.exists():
        ALLOWLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        ALLOWLIST_PATH.write_text(json.dumps({"entries": []}, indent=2))
    payload = json.loads(ALLOWLIST_PATH.read_text())
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise SystemExit(
            f"[lint] FATAL allowlist malformed (no entries[]): {ALLOWLIST_PATH}"
        )
    out: dict[tuple[str, str, int], dict[str, Any]] = {}
    for e in entries:
        key = (str(e["file"]), str(e["pattern"]), int(e["line"]))
        out[key] = e
    return out


def scan_file(path: Path, patterns: frozenset[str] | None = None) -> list[Finding]:
    rel = path.relative_to(REPO_ROOT).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        print(f"[lint] WARN unparseable {path}: {exc}", file=sys.stderr)
        return []
    active = patterns or frozenset(PATTERN_SCANNERS)
    raw: list[Finding] = []
    for tag in ("P1", "P2", "P3", "P4", "P5"):
        if tag in active:
            raw.extend(PATTERN_SCANNERS[tag](tree, rel))
    return raw


def scan_tree(root: Path, patterns: frozenset[str] | None = None) -> list[Finding]:
    allowlist = _load_allowlist()
    findings: list[Finding] = []
    for path in sorted(root.rglob("*.py")):
        for f in scan_file(path, patterns):
            entry = allowlist.get((f.file, f.pattern, f.line))
            findings.append(
                f._replace(
                    allowlisted=entry is not None,
                    allow_reason=str(entry.get("reason", "")) if entry else "",
                )
            )
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument(
        "--pattern",
        action="append",
        choices=sorted(PATTERN_SCANNERS),
        help="restrict to one or more of P1..P5 (repeatable)",
    )
    ap.add_argument(
        "--explain",
        action="store_true",
        help="print every hit including allowlisted ones",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="reserved for the ratchet wave — NOT implemented in v1; always a no-op today",
    )
    args = ap.parse_args()

    if not SRC_ROOT.exists():
        print(f"[lint] FATAL scan root not found: {SRC_ROOT}", file=sys.stderr)
        return 2

    patterns = frozenset(args.pattern) if args.pattern else None
    try:
        findings = scan_tree(SRC_ROOT, patterns)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"[lint] FATAL {exc}", file=sys.stderr)
        return 2

    unallowlisted = [f for f in findings if not f.allowlisted]
    modules_scanned = sum(1 for _ in SRC_ROOT.rglob("*.py"))
    by_pattern: dict[str, int] = {}
    for f in unallowlisted:
        by_pattern[f.pattern] = by_pattern.get(f.pattern, 0) + 1
    shown = findings if args.explain else unallowlisted

    if args.json:
        print(
            json.dumps(
                {
                    "schema": "coordharness.fail-loud-lint.v1",
                    "scan_root": str(SRC_ROOT.relative_to(REPO_ROOT)),
                    "modules_scanned": modules_scanned,
                    "total_hits": len(findings),
                    "allowlisted_count": len(findings) - len(unallowlisted),
                    "warn_count": len(unallowlisted),
                    "warn_count_by_pattern": by_pattern,
                    "mode": "WARN-ONLY (not wired to any gate)",
                    "findings": [f._asdict() for f in shown],
                },
                indent=2,
            )
        )
    else:
        print(
            f"fail-loud lint (WARN-ONLY) - {modules_scanned} modules scanned under {SRC_ROOT.relative_to(REPO_ROOT)}"
        )
        print(
            f"{len(findings)} hit(s) - {len(findings) - len(unallowlisted)} allowlisted - {len(unallowlisted)} WARN"
        )
        for tag in sorted(PATTERN_SCANNERS):
            print(f"  {tag} ({PATTERN_LABELS[tag]}): {by_pattern.get(tag, 0)}")
        for f in shown:
            tag = "ALLOWLISTED" if f.allowlisted else "WARN"
            print(f"\n{tag} [{f.pattern}/{f.rule}] {f.file}:{f.line}:{f.col}")
            print(f"    {f.detail}")
            if f.allowlisted:
                print(f"    reason  : {f.allow_reason}")
        print(
            "\nWARN-ONLY - not wired to any check-registry gate by default. "
            "Populate the allowlist and promote a pattern to enforce mode when it is ready."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
