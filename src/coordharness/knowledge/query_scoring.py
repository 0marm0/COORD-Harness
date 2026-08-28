from __future__ import annotations

import re

from coordharness.knowledge.query_aliases import QUERY_ALIAS_GROUPS

TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def normalize_search_text(value: str | None) -> str:
    spaced = CAMEL_BOUNDARY_RE.sub(" ", str(value or ""))
    return " ".join(re.sub(r"[_/.\-]+", " ", spaced.lower()).split())


def query_phrases(raw: str | None) -> list[str]:
    normalized = normalize_search_text(raw or "")
    compact = normalized.replace(" ", "")
    phrases = [normalized] if normalized else []
    for group in QUERY_ALIAS_GROUPS:
        if any(trigger in normalized or trigger.replace(" ", "") in compact for trigger in group.triggers):
            phrases.extend(normalize_search_text(expansion) for expansion in group.expansions)
    return unique(phrases)


def query_tokens(
    raw: str | None,
    *,
    stopwords: frozenset[str] | set[str] | tuple[str, ...] = (),
    max_terms: int | None = None,
) -> list[str]:
    stop = set(stopwords or ())
    tokens: list[str] = []
    for phrase in query_phrases(raw or ""):
        for token in TOKEN_RE.findall(phrase):
            token = token.lower()
            if token in stop:
                continue
            if len(token) < 2 and not token.isdigit():
                continue
            tokens.append(token)
    out = unique(tokens)
    return out[:max_terms] if max_terms is not None else out


def field_terms(value: str | None) -> set[str]:
    return set(TOKEN_RE.findall(normalize_search_text(value)))


def term_matches(term: str, terms: set[str]) -> bool:
    if term in terms:
        return True
    if len(term) < 3:
        return False
    return any(term in candidate or candidate in term for candidate in terms if len(candidate) >= 3)
