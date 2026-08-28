from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from coordharness.jobs.identity import canonical_id
from coordharness.jobs.status import parse_updated_at
from coordharness.jobs import diagnostic_marker as job_authority
from coordharness import config as harness_config

__all__ = ["SidecarSnapshot", "load_snapshot", "clear_cache", "default_job_progress_dir"]

_COUNTER_KEYS = (
    "state", "step", "pct", "done", "total", "rate", "eta_s",
    "updated_at", "last_progress_at", "runtime_s", "pid", "exit_code",
    "terminal_at", "backoff_s", "attempt",
)

_CACHE: dict[
    tuple[str, frozenset[tuple], tuple[tuple, ...]],
    "SidecarSnapshot",
] = {}


@dataclass(frozen=True)
class SidecarSnapshot:

    items: tuple[dict, ...] = ()
    by_id: dict[str, dict] = field(default_factory=dict)
    skipped: tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.items)

    def get(self, cid: str) -> Optional[dict]:
        return self.by_id.get(cid)


def default_job_progress_dir() -> Path:
    return harness_config.job_progress_dir()


def clear_cache() -> None:
    _CACHE.clear()


def _scan_fingerprint(
    dir_path: Path,
) -> tuple[
    str,
    frozenset[tuple],
    tuple[tuple, ...],
]:
    dir_real = os.path.realpath(str(dir_path))
    entries: set[tuple] = set()
    try:
        with os.scandir(dir_real) as it:
            for entry in it:
                if not entry.name.endswith(".json"):
                    continue
                try:
                    lst = entry.stat(follow_symlinks=False)
                    try:
                        target = entry.stat(follow_symlinks=True)
                        target_fields = (
                            int(target.st_dev), int(target.st_ino), int(target.st_mode),
                            int(target.st_nlink), int(target.st_mtime_ns),
                            int(target.st_ctime_ns), int(target.st_size),
                        )
                    except OSError:
                        target_fields = (-1, -1, -1, -1, -1, -1, -1)
                    entries.add((
                        job_authority.canonical_sidecar_path(entry.path),
                        int(lst.st_dev), int(lst.st_ino), int(lst.st_mode),
                        int(lst.st_nlink), int(lst.st_mtime_ns),
                        int(lst.st_ctime_ns), int(lst.st_size),
                        *target_fields,
                    ))
                except OSError:
                    continue
    except (FileNotFoundError, NotADirectoryError):
        pass
    return dir_real, frozenset(entries), job_authority.authority_cache_stamp(dir_path)


def _read_sidecar(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _hidden_from_operator(sidecar: dict) -> bool:
    visibility = str(sidecar.get("visibility") or "").strip().lower()
    return (
        sidecar.get("hide_from_operator") is True
        or sidecar.get("diagnostic_only") is True
        or visibility in {"hidden", "diagnostic", "internal"}
    )


def _freshness(sidecar: dict) -> float:
    ts = parse_updated_at(sidecar.get("updated_at"))
    if ts is None:
        ts = parse_updated_at(sidecar.get("last_progress_at"))
    return ts if ts is not None else float("-inf")


def _merge_into(rep: dict, other: dict) -> dict:
    merged = dict(rep)
    for key, val in other.items():
        if key == "_source_path":
            continue
        if merged.get(key) in (None, "") and val not in (None, ""):
            merged[key] = val
    for ckey in ("done", "total", "pct", "rows_done"):
        a, b = rep.get(ckey), other.get(ckey)
        try:
            if a is not None and b is not None:
                merged[ckey] = max(float(a), float(b))
        except (TypeError, ValueError):
            pass
    sources = list(merged.get("merged_from") or [])
    for p in (rep.get("_source_path"), other.get("_source_path")):
        if p and p not in sources:
            sources.append(p)
    if sources:
        merged["merged_from"] = sources
    return merged


def _instance_key(sidecar: dict) -> str:
    jid = str(sidecar.get("job_id") or "").strip()
    if jid:
        return f"job:{jid}"
    cid = canonical_id(sidecar)
    if cid:
        return f"cid:{cid}"
    return f"path:{sidecar.get('_source_path') or ''}"


def _index_by_id(items) -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    for d in items:
        cid = canonical_id(d)
        if not cid:
            continue
        prev = by_id.get(cid)
        if prev is None or _freshness(d) >= _freshness(prev):
            by_id[cid] = d
    return by_id


def _copy_snapshot(snap: "SidecarSnapshot") -> "SidecarSnapshot":
    items = tuple(dict(d) for d in snap.items)
    return SidecarSnapshot(items=items, by_id=_index_by_id(items), skipped=snap.skipped)


def _build(dir_real: str, fingerprint: frozenset[tuple]) -> SidecarSnapshot:
    raw: list[dict] = []
    skipped: list[str] = []
    for record in sorted(fingerprint):
        logical_path = str(record[0])
        data, authority = job_authority.read_sidecar_with_authority(logical_path)
        if data is None or authority is None:
            skipped.append(logical_path)
            continue
        if authority.diagnostic_only or _hidden_from_operator(data):
            continue
        data["_source_path"] = logical_path
        raw.append(data)

    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for sc in raw:
        ikey = _instance_key(sc)
        if ikey not in groups:
            groups[ikey] = []
            order.append(ikey)
        groups[ikey].append(sc)

    items: list[dict] = []
    for ikey in order:
        members = groups[ikey]
        members.sort(key=lambda s: (_freshness(s), s.get("_source_path") or ""), reverse=True)
        rep = members[0]
        for other in members[1:]:
            rep = _merge_into(rep, other)
        sources = list(rep.get("merged_from") or [])
        source_path = rep.get("_source_path")
        if source_path and source_path not in sources:
            sources.append(source_path)
        if sources:
            rep["merged_from"] = sources
        rep.pop("_source_path", None)
        items.append(rep)

    return SidecarSnapshot(items=tuple(items), by_id=_index_by_id(items),
                           skipped=tuple(sorted(skipped)))


def load_snapshot(job_progress_dir: object = None) -> SidecarSnapshot:
    base = (
        Path(str(job_progress_dir))
        if job_progress_dir is not None
        else default_job_progress_dir()
    )
    dir_real, fingerprint, authority_stamp = _scan_fingerprint(base)
    key = (dir_real, fingerprint, authority_stamp)
    cached = _CACHE.get(key)
    if cached is not None:
        return _copy_snapshot(cached)
    snap = _build(dir_real, fingerprint)
    for stale in [k for k in _CACHE if k[0] == dir_real]:
        del _CACHE[stale]
    _CACHE[key] = snap
    return _copy_snapshot(snap)
