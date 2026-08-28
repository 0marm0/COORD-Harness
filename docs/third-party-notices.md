# Third-party notices

The core `coordharness` package has no runtime dependency beyond Python's standard library. Optional and development dependencies are declared in `pyproject.toml` and retain their respective upstream licenses.

| Dependency | Purpose | Distribution |
|---|---|---|
| `setuptools>=83` | Python wheel build backend | Build tool only |
| `build>=1.2,<2` | Isolated wheel and source-distribution frontend | Development and release tool only |
| `mcp` | Optional Model Context Protocol stdio server | Optional `[mcp]` extra |
| `mlx-lm` | Optional Apple-silicon local inference backend | Optional `[mlx]` extra |
| `pytest` | Test runner | Development only |
| `pytest-timeout` | Test timeout enforcement | Development only |
| Ruff 0.15.4 | Reviewed Python lint rule set | Development only; exactly pinned |
| XcodeGen 2.45.4 | Generates the unsigned macOS and iOS Xcode project in native CI | Build tool only; official release asset is SHA-256 pinned |

The SVG diagrams under `docs/assets/` are original, source-controlled works created for this repository. The PNGs are declared fixed-clock synthetic captures of this repository's own web and clean-room native clients. They contain no copied logos, vendor artwork, people, customer data, or private product state. Exact accepted hashes, capture methods, and environment details are in [`assets/provenance.json`](assets/provenance.json); an unpinned browser, simulator, font stack, or operating system is not expected to reproduce the same PNG bytes.

The compact provider identifiers in `apps/brand/Assets/` are project-drawn artwork, not vendor-supplied assets. Provider names are used only to identify compatible agent runtimes; no affiliation or endorsement is claimed. The COORD wordmark was supplied by the maintainer. Exact provenance and hashes are recorded in [`assets/provenance.json`](assets/provenance.json).

GitHub Actions used for CI are pinned to immutable commit SHAs in workflow files and execute only repository tests, builds, and safety checks. Review their upstream terms and licenses before mirroring or redistributing action code.
