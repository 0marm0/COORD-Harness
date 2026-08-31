from __future__ import annotations

import logging
import re
from typing import Mapping

from coordharness.jobs.status import canonical_id, dedup_key

__all__ = ["canonical_id", "dedup_key", "norm_name",
           "bind_sidecar_to_backlog", "dedup_and_merge"]

_logger = logging.getLogger(__name__)

_NOISE_RE = re.compile(
    r"\b(gpu|cpu|ram|job|run|launch|build|the|of|a|an|with|via|tracked|relabel|relabeling)\b")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

_GENERIC_PROC_PATTERNS = {"py", "python", "python3", ".py", "run", "sh", "bash", "job", "venv"}

# proc_pattern comes from board rows (roadmap_backlog.json), not a trusted
# source of regex -- cap it and refuse obviously catastrophic-backtracking
# shapes before handing it to re.search.
_MAX_PROC_PATTERN_LEN = 256
# A quantified group `(...)+`/`(...)*`/`(...){n,}` whose own body already
# contains a quantifier -- the classic ReDoS shape (e.g. "(x+)+", "(a*)*").
# Deliberately simple/conservative: it only looks one group deep and does
# not attempt to parse the full regex grammar.
_NESTED_QUANTIFIER_RE = re.compile(
    r"\([^()]*[+*][^()]*\)[+*]|\([^()]*[+*][^()]*\)\{\d*,"
)


def _proc_pattern_is_catastrophic(pattern: str) -> bool:
    return bool(_NESTED_QUANTIFIER_RE.search(pattern))


def _proc_pattern_matches(pattern: str, text: str) -> bool:
    if len(pattern) > _MAX_PROC_PATTERN_LEN:
        _logger.warning(
            "identity: refusing proc_pattern match, pattern length %d exceeds "
            "cap %d",
            len(pattern), _MAX_PROC_PATTERN_LEN,
        )
        return False
    if _proc_pattern_is_catastrophic(pattern):
        _logger.warning(
            "identity: refusing proc_pattern match, pattern looks like "
            "catastrophic backtracking (nested quantifier): %r",
            pattern,
        )
        return False
    try:
        return re.search(pattern, text) is not None
    except re.error:
        return pattern in text


def norm_name(text: str | None) -> str:
    s = _NON_ALNUM.sub(" ", str(text or "").lower())
    stripped = _NOISE_RE.sub(" ", s)
    toks = sorted(t for t in stripped.split() if len(t) >= 2)
    if toks:
        return " ".join(toks)
    return " ".join(sorted(t for t in s.split() if t))


def _roadmap_id(item: Mapping) -> str:
    return str(item.get("roadmap_id") or "").strip()


def _is_name_echo(sidecar: Mapping) -> bool:
    rid = _roadmap_id(sidecar)
    jid = str(sidecar.get("job_id") or "").strip()
    return bool(rid) and rid == jid


def bind_sidecar_to_backlog(sidecar: Mapping, rows: list[Mapping]) -> Mapping | None:
    if not rows:
        return None

    def _ids(r: Mapping) -> set[str]:
        return {str(r.get(k) or "").strip() for k in ("roadmap_id", "id", "job_id")} - {""}

    if not _is_name_echo(sidecar):
        rid = _roadmap_id(sidecar)
        if rid:
            for r in rows:
                if rid in _ids(r):
                    return r

    script = str(sidecar.get("script") or "")
    if script:
        matches: list[tuple[int, Mapping]] = []
        for r in rows:
            pat = str(r.get("proc_pattern") or "").strip()
            if not pat or len(pat) < 4 or pat.lower() in _GENERIC_PROC_PATTERNS:
                continue
            if _proc_pattern_matches(pat, script):
                matches.append((len(pat), r))
        if matches:
            matches.sort(key=lambda m: m[0], reverse=True)
            if len(matches) == 1 or matches[0][0] != matches[1][0]:
                return matches[0][1]

    jid = str(sidecar.get("job_id") or "").strip()
    if jid:
        jslug = canonical_id({"name": jid})
        for r in rows:
            if jslug and jslug in {canonical_id({"name": x}) for x in _ids(r)}:
                return r

    sc_name = norm_name(sidecar.get("name") or sidecar.get("job_id"))
    if sc_name:
        for r in rows:
            if not _roadmap_id(r) and norm_name(r.get("name") or r.get("title")) == sc_name:
                return r
    return None


def _completeness(row: Mapping) -> int:
    score = 0
    if _roadmap_id(row):
        score += 8
    if str(row.get("done_signal") or "").strip():
        score += 4
    if str(row.get("display") or "").strip():
        score += 2
    if str(row.get("proc_pattern") or "").strip():
        score += 1
    return score


def dedup_and_merge(rows: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    name_key_to_group: dict[str, str] = {}

    for r in rows:
        cid = dedup_key(r) or norm_name(r.get("name") or r.get("title")) or id(r)
        gkey = str(cid)
        if not _roadmap_id(r):
            nk = norm_name(r.get("name") or r.get("title"))
            if nk:
                if nk in name_key_to_group:
                    gkey = name_key_to_group[nk]
                else:
                    name_key_to_group[nk] = gkey
        if gkey not in groups:
            groups[gkey] = []
            order.append(gkey)
        groups[gkey].append(r)

    out: list[dict] = []
    for gkey in order:
        members = groups[gkey]
        if len(members) == 1:
            out.append(members[0])
            continue
        rep = max(members, key=_completeness)
        merged = dict(rep)
        merged["merged_from"] = sorted(
            {str(m.get("id") or m.get("roadmap_id") or "") for m in members} - {""}
        )
        out.append(merged)
    return out
