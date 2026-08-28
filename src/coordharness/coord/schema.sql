

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version     INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  applied_at  REAL NOT NULL,
  checksum    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_sessions (
  session_id         TEXT PRIMARY KEY,
  actor              TEXT NOT NULL,
  actor_id           TEXT,
  parent_session_id  TEXT,
  runner_type        TEXT,
  human_label        TEXT,
  external_thread_id TEXT,
  conversation_title TEXT,
  worktree_id        TEXT,
  label_source       TEXT,
  label_updated_at   REAL,
  cwd                TEXT,
  pid                INTEGER,
  pid_started_at     REAL,
  started_at         REAL NOT NULL,
  last_heartbeat     REAL NOT NULL,
  lease_until        REAL NOT NULL,
  pause_at           REAL,
  state              TEXT NOT NULL DEFAULT 'active',
  ended_at           REAL,
  version            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_sessions_actor  ON agent_sessions(actor, state);
CREATE INDEX IF NOT EXISTS ix_sessions_parent ON agent_sessions(parent_session_id);

CREATE TABLE IF NOT EXISTS work_items (
  work_id             TEXT PRIMARY KEY,
  parent_id           TEXT REFERENCES work_items(work_id),
  surface             TEXT NOT NULL DEFAULT 'job',
  domain              TEXT,
  module              TEXT,
  lane                TEXT,
  sublane             TEXT,
  title               TEXT NOT NULL,
  display             TEXT,
  assignee            TEXT,
  assigned_by         TEXT,
  intent_state        TEXT NOT NULL DEFAULT 'planned',
  blocked_reason_class TEXT,
  completion_requested_at REAL,
  next_step           TEXT,
  resume_when         TEXT,
  resume_predicate_json TEXT,
  continuation_ready_at REAL,
  operator_ok_event_id INTEGER,
  tier_correction_event_id INTEGER,
  done_signal         TEXT,
  acceptance_json     TEXT NOT NULL DEFAULT '[]',
  rubric_verdict      TEXT,
  resource_class      TEXT,
  token_budget        INTEGER,
  priority            INTEGER NOT NULL DEFAULT 0,
  visibility          TEXT NOT NULL DEFAULT 'operator',
  context_pack_ref    TEXT,
  due_date            REAL,
  created_by_session_id TEXT,
  version             INTEGER NOT NULL DEFAULT 0,
  archived_at         REAL,
  created_at          REAL NOT NULL,
  updated_at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_work_module ON work_items(module);
CREATE INDEX IF NOT EXISTS ix_work_domain ON work_items(domain);
CREATE INDEX IF NOT EXISTS ix_work_parent ON work_items(parent_id);
CREATE INDEX IF NOT EXISTS ix_work_assignee ON work_items(assignee);

CREATE TABLE IF NOT EXISTS claims (
  claim_id      TEXT PRIMARY KEY,
  work_id       TEXT NOT NULL REFERENCES work_items(work_id),
  session_id    TEXT NOT NULL REFERENCES agent_sessions(session_id),
  lease_token   TEXT,
  status        TEXT NOT NULL DEFAULT 'running',
  step          TEXT,
  acquired_at   REAL NOT NULL,
  heartbeat_at  REAL NOT NULL,
  expires_at    REAL NOT NULL,
  release_reason TEXT,
  version       INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_one_held_claim ON claims(work_id)
  WHERE status IN ('running', 'paused', 'blocked');
CREATE INDEX IF NOT EXISTS ix_claims_session ON claims(session_id, status);

CREATE TABLE IF NOT EXISTS runs (
  run_id            TEXT PRIMARY KEY,
  work_id           TEXT REFERENCES work_items(work_id),
  session_id        TEXT REFERENCES agent_sessions(session_id),
  parent_session_id TEXT,
  runner_kind       TEXT NOT NULL,
  model             TEXT,
  progress_mode     TEXT NOT NULL DEFAULT 'indeterminate',
  sidecar_path      TEXT,
  pid               INTEGER,
  pid_started_at    REAL,
  pgid              INTEGER,
  resource_class    TEXT,
  started_at        REAL NOT NULL,
  heartbeat_at      REAL,
  finished_at       REAL,
  state             TEXT NOT NULL DEFAULT 'live',
  version           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_runs_work   ON runs(work_id);
CREATE INDEX IF NOT EXISTS ix_runs_parent ON runs(parent_session_id);

CREATE TABLE IF NOT EXISTS events (
  event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              REAL NOT NULL,
  kind            TEXT NOT NULL,
  actor           TEXT,
  session_id      TEXT,
  to_selector     TEXT,
  work_id         TEXT,
  run_id          TEXT,
  thread_id       TEXT,
  severity        TEXT,
  verdict         TEXT,
  trust           TEXT NOT NULL DEFAULT 'agent',
  title           TEXT,
  body            TEXT,
  refs_json       TEXT NOT NULL DEFAULT '[]',
  payload_json    TEXT NOT NULL DEFAULT '{}',
  idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_events_inbox ON events(to_selector, event_id);
CREATE INDEX IF NOT EXISTS ix_events_work  ON events(work_id, event_id);
CREATE INDEX IF NOT EXISTS ix_events_session ON events(session_id, event_id);

CREATE TABLE IF NOT EXISTS inbox_cursors (
  recipient          TEXT NOT NULL,
  session_id         TEXT NOT NULL DEFAULT '',
  last_seen_event_id INTEGER NOT NULL DEFAULT 0,
  updated_at         REAL NOT NULL,
  PRIMARY KEY (recipient, session_id)
);

CREATE TABLE IF NOT EXISTS request_consumption (
  recipient_lane     TEXT NOT NULL,
  work_id            TEXT NOT NULL,
  request_event_id   INTEGER NOT NULL REFERENCES events(event_id),
  consumed_event_id  INTEGER REFERENCES events(event_id),
  consumed_at        REAL,
  PRIMARY KEY (recipient_lane, work_id, request_event_id)
);
CREATE INDEX IF NOT EXISTS ix_request_consumption_open
  ON request_consumption(recipient_lane, consumed_at, request_event_id);

CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id     TEXT PRIMARY KEY,
  work_id         TEXT REFERENCES work_items(work_id),
  run_id          TEXT REFERENCES runs(run_id),
  path            TEXT NOT NULL,
  kind            TEXT,
  sha256          TEXT,
  validation_json TEXT NOT NULL DEFAULT '{}',
  created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_artifacts_work ON artifacts(work_id);

CREATE TABLE IF NOT EXISTS display_titles (
  key         TEXT PRIMARY KEY,
  display     TEXT NOT NULL,
  updated_at  REAL NOT NULL
);


DROP VIEW IF EXISTS v_session_claimcount;
CREATE VIEW v_session_claimcount AS
SELECT s.session_id, s.actor, s.parent_session_id, s.lease_until, s.state, s.pid,
       s.human_label, s.external_thread_id, s.conversation_title, s.worktree_id,
       s.label_source, s.label_updated_at,
       (SELECT COUNT(*) FROM claims c WHERE c.session_id = s.session_id AND c.status = 'running') AS live_claims
FROM agent_sessions s
WHERE s.state = 'active';

DROP VIEW IF EXISTS v_work_owner;
CREATE VIEW v_work_owner AS
SELECT w.*,
       c.session_id   AS owner_session_id,
       c.status       AS claim_status,
       c.expires_at   AS claim_expires_at,
       c.step         AS claim_step,
       s.actor        AS owner_session_actor,
       COALESCE(NULLIF(s.human_label, ''), NULLIF(s.conversation_title, ''), s.actor) AS owner_session_label,
       s.external_thread_id AS owner_external_thread_id,
       s.conversation_title AS owner_conversation_title,
       s.worktree_id AS owner_worktree_id,
       (SELECT COUNT(*) FROM runs r WHERE r.work_id = w.work_id AND r.state = 'live') AS live_run_count,
       (SELECT 1 FROM artifacts a
         WHERE a.work_id = w.work_id AND COALESCE(a.kind,'') NOT IN ('context_pack')
         LIMIT 1) AS has_artifact
FROM work_items w
LEFT JOIN claims c ON c.work_id = w.work_id AND c.status IN ('running', 'paused', 'blocked')
LEFT JOIN agent_sessions s ON s.session_id = c.session_id;

DROP VIEW IF EXISTS v_session_rollup;
CREATE VIEW v_session_rollup AS
SELECT p.session_id AS session_id, p.session_id AS parent_session_id, p.actor, p.runner_type, p.lease_until, p.pid,
       p.human_label, p.external_thread_id, p.conversation_title, p.worktree_id,
       p.label_source, p.label_updated_at,
       (SELECT COUNT(*) FROM agent_sessions ch WHERE ch.parent_session_id = p.session_id) AS child_sessions,
       (SELECT COUNT(*) FROM runs r WHERE r.parent_session_id = p.session_id) AS child_runs
FROM agent_sessions p
WHERE p.parent_session_id IS NULL AND p.state = 'active';

DROP VIEW IF EXISTS v_runs_read_model;
CREATE VIEW v_runs_read_model AS
SELECT r.run_id,
       r.work_id,
       w.title AS work_title,
       w.display AS work_display,
       w.intent_state AS work_intent_state,
       r.session_id,
       s.actor AS session_actor,
       r.parent_session_id,
       r.runner_kind,
       r.model,
       r.progress_mode,
       r.sidecar_path,
       r.pid,
       r.pid_started_at,
       r.pgid,
       r.resource_class,
       r.started_at,
       r.heartbeat_at,
       r.finished_at,
       CASE
         WHEN r.finished_at IS NOT NULL THEN MAX(r.finished_at - r.started_at, 0)
         ELSE NULL
       END AS duration_s,
       r.state,
       r.version
FROM runs r
LEFT JOIN work_items w ON w.work_id = r.work_id
LEFT JOIN agent_sessions s ON s.session_id = r.session_id;
