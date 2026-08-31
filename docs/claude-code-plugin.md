# Claude Code plugin

Status: **Shipped repository integration**. This repository serves as its own
single-plugin marketplace, so a fresh Claude Code install can add the
`operating-coordharness` skill in one command instead of copying files by hand.

## What it gives you

The plugin manifest (`.claude-plugin/plugin.json`) declares the skill package
at `.claude/skills/` — the same files documented in
[Agent skills](skills.md). Installing the plugin makes `operating-coordharness`
available in Claude Code without cloning the repository into a project's
`.claude/skills/` directory yourself.

The manifest also declares the command package at `.claude/commands/`, so the
five slash commands (`/coord-start`, `/coord-claim`, `/coord-close`,
`/coord-handoff`, `/coord-recover`) install with it. They live outside the
plugin-root `commands/` default the loader would otherwise look in, so the
declaration is what makes a plugin-only install carry them.

## Install

From inside Claude Code:

```
/plugin marketplace add 0marm0/COORD-Harness
/plugin install coordharness@coord-harness
```

Or from a shell:

```bash
claude plugin marketplace add 0marm0/COORD-Harness
claude plugin install coordharness@coord-harness
```

Both forms read `.claude-plugin/marketplace.json` from this repository's
default branch and install the single `coordharness` plugin it lists.

## What it does not do

The plugin only ships the skill and command instructions. It does **not**:

- install the `coordharness` Python package or its console scripts;
- create or seed a `.coordharness/coord.db`;
- register MCP server entries, native clients, or shell integrations.

For a working local board — the venv, the database, and (optionally) native
and MCP client registration — run [`scripts/setup.sh`](../scripts/setup.sh)
in a clone of this repository. The plugin and the setup script are
independent: the plugin gets the operating protocol into an agent's hands,
the setup script gets a coordinated project running.

## Uninstall

```
/plugin uninstall coordharness@coord-harness
/plugin marketplace remove coord-harness
```

Removing the plugin only removes the skill from Claude Code's skill
discovery; it has no effect on any `.coordharness/coord.db` a project may
already have, since the plugin never created one.
