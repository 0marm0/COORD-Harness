# Governance

`coordharness` is maintained as a public, local-first open-source product. Governance is intentionally lightweight, but authority boundaries are not.

## Decision ownership

- Maintainers own releases, security response, compatibility promises, and the feature-status vocabulary.
- Contributors own the correctness of their patch and its tests, documentation, provenance, and migration impact.
- Lifecycle truth belongs to `coord.db`; documentation, screenshots, memory, and client caches cannot override it.
- A native or web viewer is never a second writer. A proposal that changes that boundary requires an explicit architecture and security review.

## Change classes

| Change | Minimum evidence |
|---|---|
| Documentation or authored SVG | Link check, public-safety scan, SVG/XML validation where applicable |
| Core lifecycle or schema | Focused tests, full Python suite, migration/rollback analysis |
| MCP tool or executable | Tool-catalog/transport tests, CLI or MCP smoke test, compatibility update |
| Snapshot schema or native consumer | Producer and consumer tests, versioning note, read-only-boundary check |
| Security boundary or publication gate | Dedicated review; do not self-approve a release exception |

## Status claims

Product pages use only `Shipped`, `Preview`, `Planned`, and `Excluded`. The canonical list is [`feature-status.json`](feature-status.json). A feature may move to `Shipped` only when source, packaging, and a usable local contract are all present. A green prototype or design document is not sufficient.

## Proposals

Open an issue for user-visible behavior, schema changes, new network exposure, or a new long-lived dependency. Small internal refactors may go directly to a pull request. Security reports follow [the security policy](../.github/SECURITY.md), never a public issue.
