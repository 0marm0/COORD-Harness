from __future__ import annotations

from coordharness.knowledge import query_scoring


def test_query_tokens_expand_shared_aliases_and_camelcase() -> None:
    tokens = query_scoring.query_tokens("RunEventStore")

    assert {"run", "event", "store"} <= set(tokens)


def test_query_tokens_expand_context_routing_aliases() -> None:
    tokens = set(query_scoring.query_tokens("session brief bloat and pillar 4 demotion"))

    assert {"session", "brief", "context", "budget", "pillar", "tertiary", "search", "only"} <= tokens


def test_field_terms_normalize_paths_hyphens_and_camelcase() -> None:
    terms = query_scoring.field_terms("tool-call/RunEventStore.md")

    assert {"tool", "call", "run", "event", "store", "md"} <= terms
