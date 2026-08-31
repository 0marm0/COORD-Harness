# Agent skill packages

Status: **Shipped repository integration**. The operating protocol is packaged
twice because Codex and Claude discover project skills from different roots.
The instruction bytes are intentionally identical and a test rejects drift.

| Client | Repository location | Discovery |
|---|---|---|
| Codex CLI, IDE, and desktop | `.agents/skills/operating-coordharness/` | Codex scans `.agents/skills` from the working directory to the repository root |
| Claude Code | `.claude/skills/operating-coordharness/` | Claude loads project skills from `.claude/skills` |
| Any compatible skill host | either package directory | The package follows the open Agent Skills `SKILL.md` layout |

Each package contains:

- `SKILL.md` — trigger description and operating protocol;
- `references/lifecycle.md` — claims, leases, proof, review, and handoff;
- `references/jobs.md` — tracked processes, sidecars, GPU authority, and cleanup;
- `references/context.md` — exact, bounded, retrieval, and recall planes;
- `agents/openai.yaml` — optional Codex and ChatGPT presentation metadata.

This page covers only the skill package. Alongside it, a mirrored slash-command
tree — `coord-start`, `coord-claim`, `coord-close`, `coord-handoff`, and
`coord-recover` — ships at `.claude/commands/` and `.agents/commands/`, the same
per-client discovery split as the skill, kept byte-identical by the same mirror
test. See [Claude Code plugin](claude-code-plugin.md) for how the two packages
install together.

## Use

From anywhere in this repository, explicitly invoke
`$operating-coordharness` in Codex or ask Claude to use
`operating-coordharness`. Both clients may also select the skill implicitly when
a request matches its description.

The normal sequence is:

1. resolve the coordinated project root and database explicitly;
2. orient with MCP `preflight` or the narrow CLI board/work view;
3. claim one existing assignable work item before substantive work;
4. attach bounded subagents and durable local jobs to that owner;
5. expand exact context before retrieval or accepted memory;
6. complete only when the declared proof validates; otherwise park, block, or release.

The skill does not create authorization. It never turns a dashboard, sidecar,
memory record, or retrieval hit into lifecycle authority. It also preserves a
fail-closed MCP refusal; a skill cannot weaken deployment-profile checks.

## Keep the mirrors honest

`tests/test_skill_packages.py` compares the complete relative file set and
every byte in the two packages. Edit one package, mirror the exact change, and
run:

```bash
pytest -q tests/test_skill_packages.py
```

The generic MCP profile is available for a fresh local repository. Distribution
or deployment still requires its own release, rights, and security review; the skill
itself never grants those authorities.
