-- 004_host_identity: a host identity on the two tables that carry a pid.
--
-- Purpose (docs/roadmap.md "Near", docs/ideas.md "Multi-machine
-- coordination"): `runs.pid` and `agent_sessions.pid` only mean something on
-- the machine that recorded them. Without a host identity beside them a pid
-- probe run on machine A answers "dead" for a healthy run on machine B, and
-- the board reports a crash that did not happen. host_id is nullable: a row
-- written before this migration has no host recorded, and NULL means "not
-- stated", which the liveness path reads as "this is a local row" -- the
-- single-machine behaviour every existing database already has.
--
-- Where the ALTER lives: the two `host_id` columns are added by the additive
-- column pattern in create_schema.py (`_ADD_COLUMNS`), not by an ALTER here.
-- That is deliberate and not a style choice. `bootstrap_database` runs
-- `apply_schema` before any migration, so an ALTER in this file would hit a
-- column that already exists; and `apply_schema` is also called directly --
-- without the migration runner -- by coord/modeld_lite.py and
-- coord/runners/mlx_runner.py, so a column added only here would be missing
-- from the databases those two create and every run write against them would
-- fail. The additive pattern is the one path that reaches both. This file
-- carries the indexes, and the schema_migrations row that records the change.

PRAGMA foreign_keys = ON;

CREATE INDEX IF NOT EXISTS ix_runs_host     ON runs(host_id, state);
CREATE INDEX IF NOT EXISTS ix_sessions_host ON agent_sessions(host_id, state);
