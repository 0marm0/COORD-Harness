from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QueryAliasGroup:
    id: str
    triggers: tuple[str, ...]
    expansions: tuple[str, ...]


QUERY_ALIAS_GROUPS: tuple[QueryAliasGroup, ...] = (
    QueryAliasGroup(
        id="deerflow_runtime_journal",
        triggers=("runjournal", "run journal", "runeventstore", "run event store", "run events", "runstore", "run store"),
        expansions=("run journal", "run event store", "run event", "run events", "runstore", "run store"),
    ),
    QueryAliasGroup(
        id="toolcall_lifecycle",
        triggers=("toolcall", "tool call", "tool calls", "tool lifecycle", "tool call lifecycle"),
        expansions=("tool call", "tool calls", "toolcall", "tool lifecycle", "tool call lifecycle"),
    ),
    QueryAliasGroup(
        id="context_code_graph",
        triggers=("contextgraph", "context graph", "codegraph", "code graph"),
        expansions=("context graph", "contextgraph", "code graph", "codegraph"),
    ),
    QueryAliasGroup(
        id="provider_registry_vectors",
        triggers=("provider registry", "providers", "lancedb", "qdrant", "vector", "vector retrieval"),
        expansions=(
            "provider registry",
            "context provider registry",
            "context federator",
            "memory context",
            "vector retrieval",
            "lancedb",
            "qdrant",
        ),
    ),
    QueryAliasGroup(
        id="coord_claim_writer",
        triggers=(
            "agent lifecycle",
            "legacy codex sidecar",
            "legacy claude sidecar",
            "mechanical writer",
        ),
        expansions=(
            "coord.db claims",
            "coord lifecycle",
            "codex_coord",
            "claude_coord",
            "claim heartbeat done block",
            "mechanical helper",
            "coordination claim writer",
        ),
    ),
    QueryAliasGroup(
        id="gate_cadence",
        triggers=(
            "gate cadence",
            "gate cadence policy",
            "focused adjacent close gates",
            "full coordination suite",
            "batch boundary",
        ),
        expansions=(
            "gate cadence",
            "harness gate cadence",
            "focused tests",
            "adjacent tests",
            "close gates",
            "full coordination suite",
            "batch boundary",
        ),
    ),
    QueryAliasGroup(
        id="loop_doctor_loopspec",
        triggers=(
            "loop doctor",
            "loop contracts",
            "feedback cycle",
            "discover feedback",
            "loop-library",
            "loop library",
        ),
        expansions=(
            "LoopSpec",
            "loop spec",
            "loop contract",
            "Loop Doctor",
            "loop-library",
            "feedback cycle",
            "terminal states",
        ),
    ),
    QueryAliasGroup(
        id="context_tiers",
        triggers=("context tiers", "context tier", "source tiers", "source tier", "context routing policy"),
        expansions=("CONTEXT_TIERS", "context tiers", "source tier", "live canonical", "generated secondary", "tertiary search-only"),
    ),
    QueryAliasGroup(
        id="session_brief",
        triggers=("session brief", "session start brief", "brief bloat", "boot context", "context budget"),
        expansions=("session_brief.py", "session brief", "session start profile", "context budget", "brief cap", "orient profile"),
    ),
    QueryAliasGroup(
        id="kfts_retrieval",
        triggers=("kfts", "knowledge fts", "knowledge retrieval", "pointer search", "memory pointer"),
        expansions=("KFTS", "knowledge_fts", "pointer-first retrieval", "memory pointer", "read_note", "source tier"),
    ),
    QueryAliasGroup(
        id="generated_pillars",
        triggers=("generated pillars", "pillar 4", "pillars", "machine ledger pillar"),
        expansions=("PILLAR_4_MACHINE_LEDGER_STATUS", "generated pillar", "secondary context", "tertiary search-only", "CONTEXT_INDEX"),
    ),
    QueryAliasGroup(
        id="archive_report_demotion",
        triggers=("archive demotion", "report demotion", "stale docs", "old context", "audit reports"),
        expansions=("archive", "reports", "source_tier", "stale_source", "live canonical", "report evidence", "search-only"),
    ),
    QueryAliasGroup(
        id="section_bounded_retrieval",
        triggers=("bounded retrieval", "section bounded", "read section", "avoid full dump", "full dump"),
        expansions=("section cards", "heading slug", "read_context_pointer", "read_note fragment", "bounded section retrieval", "line_start", "line_end"),
    ),
)
