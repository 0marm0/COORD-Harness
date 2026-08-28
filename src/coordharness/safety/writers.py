"""Static inventory of direct lifecycle SQL writer call sites."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable


LIFECYCLE_TABLES = frozenset(
    {
        "agent_sessions",
        "artifacts",
        "claims",
        "events",
        "inbox_cursors",
        "request_consumption",
        "runs",
        "work_items",
    }
)
_WRITE_RE = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)
_TABLE_RE = re.compile(
    r"\b(" + "|".join(sorted(LIFECYCLE_TABLES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WriterSite:
    module: str
    line: int
    operations: tuple[str, ...]
    tables: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "module": self.module,
            "line": self.line,
            "operations": list(self.operations),
            "tables": list(self.tables),
        }


def _literal_text(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            str(part.value) for part in node.values if isinstance(part, ast.Constant)
        )
    if isinstance(node, (ast.List, ast.Tuple)):
        return " ".join(_literal_text(item) for item in node.elts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_text(node.left) + _literal_text(node.right)
    return ""


def inventory_lifecycle_writers(package_root: Path) -> tuple[list[WriterSite], list[str]]:
    sites: list[WriterSite] = []
    parse_errors: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package_root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError, UnicodeError):
            parse_errors.append(relative)
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            function = node.func
            if not isinstance(function, ast.Attribute) or function.attr not in {
                "execute",
                "executemany",
                "executescript",
            }:
                continue
            sql = _literal_text(node.args[0])
            operations = tuple(sorted({item.upper() for item in _WRITE_RE.findall(sql)}))
            tables = tuple(sorted({item.lower() for item in _TABLE_RE.findall(sql)}))
            if operations and tables:
                sites.append(WriterSite(relative, int(node.lineno), operations, tables))
    sites.sort(key=lambda item: (item.module, item.line, item.tables, item.operations))
    return sites, sorted(parse_errors)


def unexpected_writer_modules(
    sites: Iterable[WriterSite], *, allowed_modules: Iterable[str]
) -> list[str]:
    allowed = set(allowed_modules)
    return sorted({site.module for site in sites if site.module not in allowed})
