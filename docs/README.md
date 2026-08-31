# Documentation

This is the product and operator documentation for `coordharness`. Capability claims in these pages should match the machine-readable [`feature-status.json`](feature-status.json); implementation detail should point to a source module, test, or public contract rather than a private deployment.

## Choose a path

| If you want to… | Start with | Then read |
|---|---|---|
| Try the product | [Getting started](getting-started.md) | [Coordination model](coordination-model.md) |
| Connect an agent | [MCP integration](mcp-integration.md) | [Agent skills](skills.md), [MCP server reference](mcp-server.md) |
| Run several agents or jobs | [Multi-agent patterns](multi-agent-patterns.md) | [Jobs and runs](jobs-and-runs.md), [review tiers](review-tiers.md) |
| Run a local model | [Local models](local-models.md) | [Jobs and runs](jobs-and-runs.md), [safety doctor](safety-doctor.md) |
| Understand the design | [Architecture](architecture.md) | [Schema](schema.md), [policy pipeline](policy-pipeline.md) |
| Control context growth | [Context architecture](context-architecture.md) | [Graph and context](graph-and-context.md) |
| Use a visual overview | [Visual atlas](visual-atlas.md) | [Component map](component-map.md) |
| Explore the graph-first control room | [Swarm Mesh](swarm-mesh.md) | [Operations Atlas](operations-atlas.md), [web board](web-board.md) |
| Build or consume a viewer | [Web board](web-board.md) | [Native clients](native-clients.md), [compatibility](compatibility.md) |
| Audit standalone coverage | [Module coverage](module-coverage.md) | [Feature status](feature-status.json), [extraction](extraction.md) |
| Contribute or release | [Governance](governance.md) | [Releasing](releasing.md), [extraction](extraction.md) |
| Assess risk | [Safety doctor](safety-doctor.md) | [Security and privacy](security-and-privacy.md), [security policy](../.github/SECURITY.md) |

## Concepts and reference

- [Architecture](architecture.md) — components, authority boundary, and read/write split.
- [Coordination model](coordination-model.md) — work items, claims, leases, verbs, and proof.
- [Schema](schema.md) — tables, views, migrations, and invariants.
- [Policy pipeline](policy-pipeline.md) — ordered checks at lifecycle boundaries.
- [Review tiers](review-tiers.md) — risk-scaled review and author/reviewer separation.
- [Design notes](design-notes.md) — rationale and trade-offs.
- [Component map](component-map.md) — source modules by capability.
- [Feature status](feature-status.json) — machine-readable maturity contract.

## Operations and integrations

- [Getting started](getting-started.md) — install, synthetic demo, and troubleshooting.
- [MCP integration](mcp-integration.md) — Claude Code, Codex, and generic clients.
- [MCP server reference](mcp-server.md) — transport and tool groups.
- [Agent protocol](agent-protocol.md) — the session discipline agents should follow.
- [Agent skills](skills.md) — byte-identical Codex and Claude project packages.
- [Multi-agent patterns](multi-agent-patterns.md) — delegation, handoff, and collision avoidance.
- [Jobs and runs](jobs-and-runs.md) — long-process liveness and telemetry.
- [Local models](local-models.md) — explicit catalogs, preflight, and resource locking.
- [Web board](web-board.md) — local read-only projection.
- [Swarm Mesh](swarm-mesh.md) — deterministic spatial graph, typed motion, traversal, and surrounding receipts.
- [Operations Atlas](operations-atlas.md) — joined one-generation graph and operational analytics contract.
- [Native clients](native-clients.md) — macOS and iOS preview scope.
- [Operations graph assessment](operations-graph-assessment.md) — reviewed port/rewrite/leave boundary for the private source graph surfaces.

## Context and evidence

- [Context architecture](context-architecture.md) — bounded boot and targeted expansion.
- [Graph and context](graph-and-context.md) — source-bound dependency/evidence views, not a freeform whiteboard.
- [Visual atlas](visual-atlas.md) — diagrams and their source-of-truth boundaries.

## Project stewardship

- [Module coverage](module-coverage.md) — forensic source-to-standalone capability ledger.
- [Safety doctor](safety-doctor.md) — read-only schema, lifecycle, job, path, Git, and MCP checks.
- [Security and privacy](security-and-privacy.md) — local trust model and publication boundaries.
- [Governance](governance.md) — decision ownership and change classes.
- [Compatibility](compatibility.md) — Python, SQLite, MCP, snapshot, and native compatibility.
- [Releasing](releasing.md) — exact-candidate distribution gate and provenance checks.
- [Changelog](../CHANGELOG.md) — public preview release notes and explicit boundaries.
- [Extraction](extraction.md) — how the clean-room repository is separated safely.
- [Codex prompts](codex-prompts.md) — self-contained briefs for the visual and graph work still outstanding.
- [Release readiness checklist](next-steps.md) — the release gates, plus small tactical gaps tracked against the operator's handbook.
- [Roadmap](roadmap.md) — ordered outcomes and explicit non-goals.
- [Ideas](ideas.md) — exploratory work that is not a commitment.

## Visuals

Every source-controlled diagram is an authored SVG under [`assets/`](assets/), includes accessible title and description elements, and is accounted for in [`assets/provenance.json`](assets/provenance.json). UI screenshots are accepted as product evidence only when they use the deterministic fictional board and carry exact hash, dimensions, capture method, source clock, and synthetic-data provenance.
