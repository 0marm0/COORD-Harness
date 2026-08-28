"""Merge agent-proposed port entries into the working manifest.

Discovery agents propose; nothing they return is trusted. Each proposed source
is resolved against the source tree, globs are rejected, destinations already in
the manifest are skipped, and anything unresolvable is reported rather than
silently dropped. The porter and the publication gate then decide what actually
survives.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "tools" / "extract" / "manifest.private.json"

# A proposal may spell its source relative to any sibling project in the source
# tree, so each candidate prefix is tried against the filesystem in turn. The
# prefixes name the private tree's layout, so they come from the gitignored
# vocabulary file rather than being written here.
def _prefixes() -> tuple[str, ...]:
    vocab = Path(__file__).resolve().parent / "vocabulary.json"
    if vocab.is_file():
        loaded = json.loads(vocab.read_text()).get("source_prefixes")
        if loaded:
            return tuple(loaded)
    return ("",)


PREFIXES = _prefixes()


def resolve(raw: str, source_root: Path) -> str | None:
    for prefix in PREFIXES:
        if (source_root / (prefix + raw)).is_file():
            return prefix + raw
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--proposals", type=Path, required=True)
    ap.add_argument("--source-root", type=Path, required=True)
    ap.add_argument("--include", action="append", default=[])
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    data = json.loads(args.proposals.read_text())
    entries = data.get("proposal_entries") or data.get("entries") or []
    manifest = json.loads(MANIFEST.read_text())
    have = {e["dest"] for e in manifest["files"]}

    merged, missing, dupes, out_of_scope = [], [], [], 0
    for entry in entries:
        dest = str(entry.get("dest", ""))
        if args.include and not any(dest.startswith(p) for p in args.include):
            out_of_scope += 1
            continue
        if any(dest.startswith(p) for p in args.exclude):
            out_of_scope += 1
            continue
        if any(ch in dest or ch in str(entry.get("source", "")) for ch in "*?["):
            missing.append(f"{dest} (glob rejected)")
            continue
        if dest in have:
            dupes.append(dest)
            continue
        source = resolve(str(entry.get("source", "")), args.source_root)
        if source is None:
            missing.append(f"{dest} <- {entry.get('source')}")
            continue
        clean = {"source": source, "dest": dest, "reason": entry.get("reason", "")}
        if entry.get("edits"):
            clean["edits"] = entry["edits"]
        if entry.get("drop_symbols"):
            clean["drop_symbols"] = entry["drop_symbols"]
        merged.append(clean)
        have.add(dest)

    print(f"  proposed     : {len(entries)}")
    print(f"  merged       : {len(merged)}")
    print(f"  duplicates   : {len(dupes)}")
    print(f"  unresolvable : {len(missing)}")
    print(f"  out of scope : {out_of_scope}")
    for item in missing[:20]:
        print(f"    UNRESOLVED {item}")

    if not args.dry_run and merged:
        manifest["files"].extend(merged)
        MANIFEST.write_text(json.dumps(manifest, indent=2))
        print(f"  manifest now : {len(manifest['files'])} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
