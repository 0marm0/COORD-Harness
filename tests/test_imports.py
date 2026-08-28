"""Every module in the package must import.

This exists because it did not, and nothing caught it. The extraction excluded two
packages that the MCP server imported at module scope, so that module raised
ModuleNotFoundError on every import while the rest of the suite stayed green --
the other tests simply never imported it.

An import test is cheap and catches a whole class of extraction damage: a module
left pointing at something that was not ported, a renamed package that only got
half-renamed, a data file that moved out from under a module-scope read.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
PACKAGE = "coordharness"


def _module_names() -> list[str]:
    names: list[str] = [PACKAGE]
    package = importlib.import_module(PACKAGE)
    for info in pkgutil.walk_packages(package.__path__, prefix=f"{PACKAGE}."):
        names.append(info.name)
    return sorted(names)


def test_package_has_modules() -> None:
    """Guard the guard: an empty list would make the import test vacuously pass."""
    names = _module_names()
    assert len(names) > 40, f"expected the full package, found only {names}"


@pytest.mark.parametrize("module_name", _module_names())
def test_module_imports(module_name: str) -> None:
    try:
        importlib.import_module(module_name)
    except ImportError as exc:
        if "mcp" in str(exc) and "coordharness" not in str(exc):
            pytest.skip("optional MCP dependency is not installed")
        raise


def test_no_module_references_an_unported_package() -> None:
    """Catch the failure earlier than import: a dangling reference in the source text.

    The extraction deliberately left several packages behind. A module that still
    names one of them will fail at import, but only if something imports it --
    so check the text directly. Names from the source project are covered by the
    publication gate's vocabulary rather than repeated here, since spelling them
    out in a shipped test would disclose what the extraction removed.
    """
    absent = ("coordharness.private_adapter", "coordharness.retrieval")
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for name in absent:
            if name in text:
                offenders.append(f"{path.relative_to(SRC)} references {name}")
    assert not offenders, "dangling references to packages that were not ported:\n" + "\n".join(offenders)
