#!/usr/bin/env python3
"""Find capabilities that exist and that nothing calls.

Four turned up in a single night: an event kind with no producer, a work-in-progress
cap that lived only in a failing test, a review-quorum module whose own docstring
said nothing called it, and a reaper that released a thousand claims without
recording one. None was untested. Three were fully tested and green, which is why
the predicate here is **referenced only by its own module and its tests** rather
than anything about coverage. A test is not a caller.

Two checks, because the census that motivated this had to correct its own map:

* **Dark modules.** A module no other module imports, tests excluded. Each one
  must be allowlisted with a written reason, so a deliberate pause is
  distinguishable from an oversight — the distinction that goes missing when
  nothing schedules the re-decision.
* **Stale markers.** A module that says ``NOT WIRED`` while being called. That is
  worse than silence: it hands the next reader a false map of what is enforced.
  One existed the moment the quorum module was wired.

The marker phrase is machine-read, so it must not appear in prose that merely
DISCUSSES it: writing "this used to say the marker" in a docstring makes the
module report itself. That happened on the first run of this check, to the very
module whose marker had just been corrected.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from pathlib import Path

ALLOWLIST_NAME = "dark_capability_allowlist.json"
_NOT_WIRED = re.compile(r"\bNOT[\s_-]WIRED\b", re.I)


# Build metadata and provenance manifests LIST every file; they are not callers.
# Counting them made two genuinely unwired lint modules look reachable on the
# first run of this check.
_NOT_A_CALLER = ("egg-info", "tools/extract/", ".venv", "__pycache__")
# Top-level packaging output. A wheel build leaves build/lib/<pkg>/... holding a
# COPY of every module, so a driver that exists only in that copy read as a live
# caller and an ablation that removed the real driver stayed green.
_NOT_A_CALLER_PREFIXES = ("build/", "dist/")


def _is_caller_file(path: Path, repo: Path) -> bool:
    rel = path.relative_to(repo).as_posix()
    if rel.startswith(_NOT_A_CALLER_PREFIXES):
        return False
    return not any(marker in rel for marker in _NOT_A_CALLER)


def _imported_module_names(path: Path) -> set[str]:
    """Module names this file imports, however the import is spelled."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.update(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.update(node.module.split("."))
            for alias in node.names:
                names.add(alias.name)
    return names


def _importers_by_module(repo: Path) -> dict[str, set[Path]]:
    """Map each imported module name to the files importing it, in ONE pass.

    Built once and shared by both checks. The first version re-walked the whole
    repository for every module and took three minutes, which is how a guard
    gets deleted rather than fixed.
    """
    index: dict[str, set[Path]] = {}
    for path in sorted(repo.rglob("*.py")):
        rel = path.relative_to(repo).as_posix()
        if not _is_caller_file(path, repo) or "tests" in Path(rel).parts:
            continue
        for name in _imported_module_names(path):
            index.setdefault(name, set()).add(path)
    return index


def declared_entry_text(repo: Path) -> str:
    """The parts of ``pyproject.toml`` that actually make a module load.

    Console scripts, entry points, and pytest ``-p`` plugin registrations --
    read structurally, not as a substring of the whole file. The first version
    matched the raw file text, so a COMMENT naming a module was enough to make
    it read as wired: the ablation that removed a plugin's registration left
    the sentence explaining the registration behind, and the check stayed green
    on a module nothing loaded. A guard whose own ablation cannot turn it red
    is a claim, not a check.
    """
    path = repo / "pyproject.toml"
    if not path.is_file():
        return ""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    parts: list[str] = []
    project = data.get("project") or {}
    parts.extend(str(v) for v in (project.get("scripts") or {}).values())
    for group in (project.get("entry-points") or {}).values():
        if isinstance(group, dict):
            parts.extend(str(v) for v in group.values())
    if isinstance(project.get("gui-scripts"), dict):
        parts.extend(str(v) for v in project["gui-scripts"].values())
    pytest_options = ((data.get("tool") or {}).get("pytest") or {}).get("ini_options") or {}
    addopts = pytest_options.get("addopts")
    if isinstance(addopts, str):
        parts.append(addopts)
    elif isinstance(addopts, list):
        parts.extend(str(v) for v in addopts)
    plugins = pytest_options.get("plugins")
    if isinstance(plugins, list):
        parts.extend(str(v) for v in plugins)
    return "\n".join(parts)


def dark_modules(repo: Path, index: dict[str, set[Path]] | None = None) -> list[tuple[str, str]]:
    """Modules that nothing outside the test tree imports.

    Module scope, not symbol scope, and deliberately so. Enumerating every
    public top-level name produced 395 candidates against 816 definitions -- a
    48% rate that is almost entirely functions used only inside their own file,
    which are internal helpers that happen to lack an underscore rather than
    capabilities nobody can reach. A module nothing imports is unambiguous: no
    caller exists anywhere, by any spelling. That is the shape all four of the
    findings this check exists for actually had.
    """
    source_root = repo / "src"
    if index is None:
        index = _importers_by_module(repo)
    entry_text = declared_entry_text(repo)

    findings: list[tuple[str, str]] = []
    for path in sorted(source_root.rglob("*.py")):
        if (
            "__pycache__" in path.parts
            or path.name == "__init__.py"
            or path.name.startswith("_")
        ):
            continue
        stem = path.stem
        # A console-script entry point, or a pytest ``-p`` plugin registration,
        # is a caller even though nothing imports it. Read from the declared
        # values only -- see declared_entry_text.
        if stem in entry_text:
            continue
        if any(other != path for other in index.get(stem, ())):
            continue
        findings.append((stem, path.relative_to(repo).as_posix()))
    return findings


def stale_markers(repo: Path, index: dict[str, set[Path]] | None = None) -> list[tuple[str, str]]:
    """Modules that claim to be unwired while something calls them."""
    if index is None:
        index = _importers_by_module(repo)
    findings: list[tuple[str, str]] = []
    for path in sorted((repo / "src").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not _NOT_WIRED.search(text):
            continue
        if any(other != path for other in index.get(path.stem, ())):
            findings.append((path.stem, path.relative_to(repo).as_posix()))
    return findings


def load_allowlist(repo: Path) -> dict[str, str]:
    path = repo / "tools" / ALLOWLIST_NAME
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("dark", {})
    return {k: str(v).strip() for k, v in entries.items() if str(v).strip()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="repository root")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()

    allowed = load_allowlist(repo)
    index = _importers_by_module(repo)
    dark = [(n, w) for n, w in dark_modules(repo, index) if n not in allowed]
    stale = stale_markers(repo, index)

    for name, where in dark:
        print(f"DARK  {name}  ({where}) -- no module outside the tests imports it")
    for module, where in stale:
        print(f"STALE {module}  ({where}) -- says NOT WIRED, but something calls it")
    if not dark and not stale:
        print(
            f"no unexplained dark capabilities "
            f"({len(allowed)} allowlisted with a reason)"
        )
        return 0
    print(
        "\nEither wire it, delete it, or add it to tools/"
        f"{ALLOWLIST_NAME} with a reason someone can act on later."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
