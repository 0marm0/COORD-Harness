# Component map

This map answers “where does that capability live?” Maturity comes from [`feature-status.json`](feature-status.json), not from file count or documentation alone.

<p align="center">
  <img src="assets/system-architecture.svg" alt="Local actors use three control surfaces against one lifecycle authority, with read-only projection clients." width="100%">
</p>

## Orient and coordinate

| Capability | Source home | Status |
|---|---|---|
| Database bootstrap and migrations | `coord/create_schema.py`, `coord/schema.sql`, `coord/migrations/` | Shipped |
| Lifecycle, claims, leases, proof, events | `coord/coord_db.py` | Shipped |
| CLI | `entry.py`, `coord/cli.py` | Shipped |
| MCP stdio tools | `coord/mcp_coord_server.py` | Preview |
| Policy pipeline | `coord/policy/pipeline.py` plus boundary checks | Shipped |
| Derived board and run models | `coord/projection.py`, `coord/work_query_v2.py` | Shipped |
| Codex and Claude project skills | `.agents/skills/`, `.claude/skills/` | Shipped |

## Retrieve and remember

| Capability | Source home | Status |
|---|---|---|
| Bounded board lenses and boot context | `coord/board_context.py` | Preview; no accepted standalone profile yet |
| Facts and full-text knowledge | `knowledge/facts.py`, `knowledge/kfts.py` | Shipped |
| Federated context packs | `knowledge/context_federator.py` | Shipped |
| Accepted-memory processing | `knowledge/accepted_memory_r4.py`, `knowledge/memory_proposals.py` | Shipped |
| Source-bound dependency/evidence views | `coord/exact_query_core.py`, `coord/r4_plane_authority.py` | Preview |
| Freeform shared whiteboard | No source implementation | Planned |

## Run and observe

| Capability | Source home | Status |
|---|---|---|
| Local process identity, launch, sidecars, status | `jobs/` | Shipped; CLI Preview |
| Local model catalog, preflight, and resource lock | `coord/model_cli.py`, `coord/modeld_lite.py`, `jobs/resource_lock.py` | Preview |
| Usage and cost ledger | `usage/` | Shipped |
| Loopback web snapshot | `board/` | Preview |
| macOS and iOS snapshot clients | `apps/` | Preview |

## Verify and publish

| Capability | Source home | Status |
|---|---|---|
| Test cleanrooms and pytest gates | `testing/` | Shipped |
| Repository and path lints | `lints/` | Shipped |
| Release/process guards | `runtime/` | Shipped |
| Read-only safety doctor and reusable guards | `safety/` | Shipped |
| Extraction and publication gate | `tools/extract/` | Shipped internal release control |
| Public release or hosted service | No release is claimed | Excluded until explicitly authorized |

For diagrams and source-truth notes, use the [visual atlas](visual-atlas.md).
