# Public roadmap

This roadmap covers the standalone repository only. It is directional, not a
claim that an item is already assigned or running. Live work belongs in the
local coordination database.

## 1. Installation and first-run confidence

- keep the one-command macOS setup path reproducible;
- expand clean-install tests across supported Python versions;
- make client configuration validation actionable and reversible;
- preserve a deterministic demo that contains only fictional work.

## 2. Provider usage and routing

- harden persistent provider authentication and explicit account switching;
- keep provider quotas, costs, histories, and freshness separate;
- improve routing recommendations using declared quota thresholds;
- never infer account state from another application or copy credentials.

## 3. System telemetry

- keep CPU, GPU, memory, and disk collection bounded and low overhead;
- document platform-specific sampling semantics;
- retain user-configurable warning and critical thresholds;
- expose detailed history only on demand.

## 4. Context and memory

- extend source-aware retrieval over operator-selected repositories;
- keep accepted memory separate from proposals;
- add export and backup receipts for local knowledge stores;
- preserve clear absence, stale, truncated, and unavailable states.

## 5. Coordination surfaces

- improve compact board navigation and accessible graph inspection;
- keep Fleet and Pulse summaries source-backed and collapsible;
- align web and native clients on one snapshot schema;
- add regression screenshots only from fictional fixtures.

## 6. Reliability and security

- fuzz lifecycle and review boundaries;
- verify recovery from interrupted database and sidecar writes;
- keep publication privacy checks mandatory in CI;
- require a fresh private vocabulary during official releases;
- review dependency updates manually before bot automation is enabled.

## Release readiness

A release candidate is ready only when:

1. the full test and lint suites pass in a clean environment;
2. packaging installs and runs outside the source checkout;
3. browser and applicable native tests pass;
4. documentation, assets, privacy, secret, and reachable-history gates pass;
5. the exact provider commit and advertised refs match the reviewed candidate.
