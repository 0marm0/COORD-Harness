# Visual assets

The maintained index is the [visual atlas](../visual-atlas.md). It explains the question each diagram answers, how overlapping diagrams differ, and which source modules define the relationship.

All SVGs in this directory are source-controlled, accessible vector markup:

- each has a non-empty `<title>` and `<desc>` referenced by `aria-labelledby`;
- labels do not rely on color alone;
- light and dark GitHub rendering are supported;
- no vendor logo, person, customer data, copied artwork, or external binary asset is embedded.

Machine-readable authorship and source-truth pointers are in [`provenance.json`](provenance.json). Run `python ../../.github/scripts/validate_docs.py` from this directory, or `python .github/scripts/validate_docs.py` from the repository root, after adding or editing an asset.

The `screens/` directory is deliberately stricter than ordinary documentation imagery: only declared fixed-clock synthetic web captures and clean-room native captures are allowed. Every PNG must have exact hash, dimensions, capture method, viewport or device, source-truth pointers, and a fixed source clock or deterministic fixture in `provenance.json`; the publication gate parses PNG chunks and rejects metadata-bearing or malformed files. These records verify the bytes and synthetic custody of the checked-in captures; they are not a claim that an unpinned browser, simulator, font stack, or operating system will reproduce the same PNG byte-for-byte.

The browser capture helper is `tools/capture_board_screens.py`. It fixes browser-visible capture
settings and rejects an empty graph, while `provenance.json` remains the authority for the exact
accepted image bytes.
