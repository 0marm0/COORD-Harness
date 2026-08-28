
PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = OFF;

CREATE TABLE IF NOT EXISTS coord_provenance_policy (
  policy_id          TEXT PRIMARY KEY CHECK (policy_id='artifact_causal_trace'),
  schema_epoch       TEXT NOT NULL,
  active_batch_id    TEXT,
  updated_at         REAL NOT NULL
) WITHOUT ROWID;

INSERT OR IGNORE INTO coord_provenance_policy(
  policy_id,schema_epoch,active_batch_id,updated_at
) VALUES (
  'artifact_causal_trace','coord-provenance-causal.r1',NULL,
  (julianday('now')-2440587.5)*86400.0
);

CREATE TABLE IF NOT EXISTS coord_provenance_batches (
  batch_id                    TEXT PRIMARY KEY,
  manifest_sha256             TEXT NOT NULL UNIQUE CHECK(length(manifest_sha256)=64),
  source_database_path        TEXT NOT NULL,
  source_schema_version       INTEGER NOT NULL,
  source_artifact_rows        INTEGER NOT NULL CHECK(source_artifact_rows>=0),
  source_row_subject_sha256   TEXT NOT NULL CHECK(length(source_row_subject_sha256)=64),
  source_id                   TEXT NOT NULL CHECK(length(source_id)=32),
  source_change_seq           INTEGER NOT NULL CHECK(source_change_seq>=0),
  policy_json                 TEXT NOT NULL CHECK(json_valid(policy_json)),
  summary_json                TEXT NOT NULL CHECK(json_valid(summary_json)),
  imported_by                 TEXT NOT NULL,
  imported_at                 REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS coord_artifact_object_versions (
  object_version_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  object_id           TEXT NOT NULL CHECK(length(object_id)=64),
  object_version      INTEGER NOT NULL CHECK(object_version>=1),
  batch_id            TEXT NOT NULL REFERENCES coord_provenance_batches(batch_id),
  resolved_path       TEXT NOT NULL,
  sha256              TEXT NOT NULL CHECK(length(sha256)=64),
  device              INTEGER NOT NULL,
  inode               INTEGER NOT NULL,
  size_bytes          INTEGER NOT NULL CHECK(size_bytes>=0),
  mtime_ns            INTEGER NOT NULL CHECK(mtime_ns>=0),
  evidence_json       TEXT NOT NULL CHECK(json_valid(evidence_json)),
  created_at          REAL NOT NULL,
  UNIQUE(object_id,object_version),
  UNIQUE(object_id,batch_id)
);
CREATE INDEX IF NOT EXISTS ix_coord_artifact_versions_batch
  ON coord_artifact_object_versions(batch_id,object_id);

CREATE TABLE IF NOT EXISTS coord_artifact_object_heads (
  object_id           TEXT PRIMARY KEY CHECK(length(object_id)=64),
  object_version_id   INTEGER NOT NULL REFERENCES coord_artifact_object_versions(object_version_id),
  batch_id            TEXT NOT NULL REFERENCES coord_provenance_batches(batch_id),
  sha256              TEXT NOT NULL CHECK(length(sha256)=64),
  updated_at          REAL NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS coord_artifact_import_rows (
  import_row_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id            TEXT NOT NULL REFERENCES coord_provenance_batches(batch_id),
  artifact_id         TEXT NOT NULL,
  source_row_sha256   TEXT NOT NULL CHECK(length(source_row_sha256)=64),
  object_id           TEXT NOT NULL CHECK(length(object_id)=64),
  object_version_id   INTEGER REFERENCES coord_artifact_object_versions(object_version_id),
  disposition         TEXT NOT NULL CHECK(disposition IN ('admitted','quarantined')),
  reason_code         TEXT,
  evidence_json       TEXT NOT NULL CHECK(json_valid(evidence_json)),
  created_at          REAL NOT NULL,
  UNIQUE(batch_id,artifact_id)
);
CREATE INDEX IF NOT EXISTS ix_coord_artifact_import_object
  ON coord_artifact_import_rows(batch_id,object_id,disposition);

CREATE TABLE IF NOT EXISTS coord_provenance_quarantine (
  quarantine_id      TEXT PRIMARY KEY,
  batch_id           TEXT NOT NULL REFERENCES coord_provenance_batches(batch_id),
  subject_kind       TEXT NOT NULL,
  subject_id         TEXT NOT NULL,
  reason_code        TEXT NOT NULL,
  evidence_json      TEXT NOT NULL CHECK(json_valid(evidence_json)),
  created_at         REAL NOT NULL,
  UNIQUE(batch_id,subject_kind,subject_id,reason_code)
);
CREATE INDEX IF NOT EXISTS ix_coord_provenance_quarantine_batch
  ON coord_provenance_quarantine(batch_id,reason_code,subject_kind);

CREATE TABLE IF NOT EXISTS coord_causal_nodes (
  node_id             TEXT PRIMARY KEY CHECK(length(node_id)=64),
  batch_id            TEXT NOT NULL REFERENCES coord_provenance_batches(batch_id),
  node_kind           TEXT NOT NULL CHECK(node_kind IN
                       ('session','span','work','run','local_job','artifact')),
  source_table        TEXT NOT NULL,
  source_key          TEXT NOT NULL,
  source_row_sha256   TEXT NOT NULL CHECK(length(source_row_sha256)=64),
  payload_json        TEXT NOT NULL CHECK(json_valid(payload_json)),
  created_at          REAL NOT NULL,
  UNIQUE(batch_id,node_kind,source_key)
);
CREATE INDEX IF NOT EXISTS ix_coord_causal_nodes_batch_kind
  ON coord_causal_nodes(batch_id,node_kind,source_key);

CREATE TABLE IF NOT EXISTS coord_causal_edges (
  edge_id             TEXT PRIMARY KEY CHECK(length(edge_id)=64),
  batch_id            TEXT NOT NULL REFERENCES coord_provenance_batches(batch_id),
  parent_node_id      TEXT NOT NULL REFERENCES coord_causal_nodes(node_id),
  child_node_id       TEXT NOT NULL REFERENCES coord_causal_nodes(node_id),
  edge_kind           TEXT NOT NULL CHECK(edge_kind IN
                       ('parent_session_span','session_run','span_run','work_run',
                        'run_local_job','run_artifact','work_artifact')),
  authority_kind      TEXT NOT NULL CHECK(authority_kind='explicit_source_field'),
  evidence_json       TEXT NOT NULL CHECK(json_valid(evidence_json)),
  created_at          REAL NOT NULL,
  UNIQUE(batch_id,parent_node_id,child_node_id,edge_kind),
  CHECK(parent_node_id<>child_node_id)
);
CREATE INDEX IF NOT EXISTS ix_coord_causal_edges_parent
  ON coord_causal_edges(batch_id,parent_node_id,edge_kind);
CREATE INDEX IF NOT EXISTS ix_coord_causal_edges_child
  ON coord_causal_edges(batch_id,child_node_id,edge_kind);

CREATE TABLE IF NOT EXISTS coord_wake_tokens (
  wake_token_id       TEXT PRIMARY KEY CHECK(length(wake_token_id)=64),
  batch_id            TEXT NOT NULL REFERENCES coord_provenance_batches(batch_id),
  root_node_id        TEXT NOT NULL REFERENCES coord_causal_nodes(node_id),
  source_id           TEXT NOT NULL CHECK(length(source_id)=32),
  source_change_seq   INTEGER NOT NULL CHECK(source_change_seq>=0),
  closure_sha256      TEXT NOT NULL CHECK(length(closure_sha256)=64),
  node_count          INTEGER NOT NULL CHECK(node_count>=1),
  edge_count          INTEGER NOT NULL CHECK(edge_count>=0),
  payload_json        TEXT NOT NULL CHECK(json_valid(payload_json)),
  created_at          REAL NOT NULL,
  UNIQUE(batch_id,root_node_id)
);

CREATE TABLE IF NOT EXISTS coord_provenance_receipts (
  receipt_id          TEXT PRIMARY KEY,
  receipt_kind        TEXT NOT NULL CHECK(receipt_kind IN
                       ('dark_import','activation','rollback','verification')),
  batch_id            TEXT REFERENCES coord_provenance_batches(batch_id),
  previous_batch_id   TEXT,
  source_id           TEXT NOT NULL CHECK(length(source_id)=32),
  source_change_seq   INTEGER NOT NULL CHECK(source_change_seq>=0),
  receipt_json        TEXT NOT NULL CHECK(json_valid(receipt_json)),
  receipt_sha256      TEXT NOT NULL CHECK(length(receipt_sha256)=64),
  created_at          REAL NOT NULL
);

CREATE TRIGGER IF NOT EXISTS coord_provenance_policy_no_insert
BEFORE INSERT ON coord_provenance_policy
WHEN EXISTS(SELECT 1 FROM coord_provenance_policy WHERE policy_id='artifact_causal_trace')
BEGIN SELECT RAISE(ABORT,'provenance policy singleton already exists'); END;
CREATE TRIGGER IF NOT EXISTS coord_provenance_policy_no_delete
BEFORE DELETE ON coord_provenance_policy
BEGIN SELECT RAISE(ABORT,'provenance policy singleton cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS coord_artifact_heads_consistent_insert
BEFORE INSERT ON coord_artifact_object_heads BEGIN
  SELECT CASE WHEN NOT EXISTS(
    SELECT 1 FROM coord_artifact_object_versions v
     WHERE v.object_version_id=NEW.object_version_id
       AND v.object_id=NEW.object_id AND v.batch_id=NEW.batch_id
       AND v.sha256=NEW.sha256
  ) THEN RAISE(ABORT,'artifact head/version mismatch') END;
END;
CREATE TRIGGER IF NOT EXISTS coord_artifact_heads_consistent_update
BEFORE UPDATE ON coord_artifact_object_heads BEGIN
  SELECT CASE WHEN NOT EXISTS(
    SELECT 1 FROM coord_artifact_object_versions v
     WHERE v.object_version_id=NEW.object_version_id
       AND v.object_id=NEW.object_id AND v.batch_id=NEW.batch_id
       AND v.sha256=NEW.sha256
  ) THEN RAISE(ABORT,'artifact head/version mismatch') END;
END;

CREATE TRIGGER IF NOT EXISTS coord_provenance_batches_no_update BEFORE UPDATE ON coord_provenance_batches
BEGIN SELECT RAISE(ABORT,'provenance batches are immutable'); END;
CREATE TRIGGER IF NOT EXISTS coord_provenance_batches_no_delete BEFORE DELETE ON coord_provenance_batches
BEGIN SELECT RAISE(ABORT,'provenance batches are immutable'); END;
CREATE TRIGGER IF NOT EXISTS coord_artifact_versions_no_update BEFORE UPDATE ON coord_artifact_object_versions
BEGIN SELECT RAISE(ABORT,'artifact object versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS coord_artifact_versions_no_delete BEFORE DELETE ON coord_artifact_object_versions
BEGIN SELECT RAISE(ABORT,'artifact object versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS coord_artifact_import_rows_no_update BEFORE UPDATE ON coord_artifact_import_rows
BEGIN SELECT RAISE(ABORT,'artifact import rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS coord_artifact_import_rows_no_delete BEFORE DELETE ON coord_artifact_import_rows
BEGIN SELECT RAISE(ABORT,'artifact import rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS coord_provenance_quarantine_no_update BEFORE UPDATE ON coord_provenance_quarantine
BEGIN SELECT RAISE(ABORT,'provenance quarantine is immutable'); END;
CREATE TRIGGER IF NOT EXISTS coord_provenance_quarantine_no_delete BEFORE DELETE ON coord_provenance_quarantine
BEGIN SELECT RAISE(ABORT,'provenance quarantine is immutable'); END;
CREATE TRIGGER IF NOT EXISTS coord_causal_nodes_no_update BEFORE UPDATE ON coord_causal_nodes
BEGIN SELECT RAISE(ABORT,'causal nodes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS coord_causal_nodes_no_delete BEFORE DELETE ON coord_causal_nodes
BEGIN SELECT RAISE(ABORT,'causal nodes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS coord_causal_edges_no_update BEFORE UPDATE ON coord_causal_edges
BEGIN SELECT RAISE(ABORT,'causal edges are immutable'); END;
CREATE TRIGGER IF NOT EXISTS coord_causal_edges_no_delete BEFORE DELETE ON coord_causal_edges
BEGIN SELECT RAISE(ABORT,'causal edges are immutable'); END;
CREATE TRIGGER IF NOT EXISTS coord_wake_tokens_no_update BEFORE UPDATE ON coord_wake_tokens
BEGIN SELECT RAISE(ABORT,'wake tokens are immutable'); END;
CREATE TRIGGER IF NOT EXISTS coord_wake_tokens_no_delete BEFORE DELETE ON coord_wake_tokens
BEGIN SELECT RAISE(ABORT,'wake tokens are immutable'); END;
CREATE TRIGGER IF NOT EXISTS coord_provenance_receipts_no_update BEFORE UPDATE ON coord_provenance_receipts
BEGIN SELECT RAISE(ABORT,'provenance receipts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS coord_provenance_receipts_no_delete BEFORE DELETE ON coord_provenance_receipts
BEGIN SELECT RAISE(ABORT,'provenance receipts are immutable'); END;

DROP VIEW IF EXISTS v_coord_active_causal_edges;
CREATE VIEW v_coord_active_causal_edges AS
SELECT e.edge_id,e.batch_id,e.edge_kind,e.authority_kind,e.parent_node_id,e.child_node_id,
       p.node_kind AS parent_kind,p.source_key AS parent_key,
       c.node_kind AS child_kind,c.source_key AS child_key,e.evidence_json
  FROM coord_causal_edges e
  JOIN coord_causal_nodes p ON p.node_id=e.parent_node_id
  JOIN coord_causal_nodes c ON c.node_id=e.child_node_id
  JOIN coord_provenance_policy x ON x.policy_id='artifact_causal_trace'
 WHERE e.batch_id=x.active_batch_id;
