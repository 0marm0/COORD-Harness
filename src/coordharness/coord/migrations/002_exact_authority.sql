
PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = OFF;

CREATE TABLE IF NOT EXISTS coord_authority_policy (
  policy_id          TEXT PRIMARY KEY CHECK (policy_id = 'exact_authority'),
  schema_epoch       TEXT NOT NULL,
  enforcement_mode   TEXT NOT NULL CHECK (enforcement_mode IN ('audit', 'enforce')),
  active_generation  TEXT,
  updated_at         REAL NOT NULL
) WITHOUT ROWID;

INSERT OR IGNORE INTO coord_authority_policy (
  policy_id, schema_epoch, enforcement_mode, active_generation, updated_at
) VALUES (
  'exact_authority', 'coord-exact-authority.r1', 'audit', NULL,
  (julianday('now') - 2440587.5) * 86400.0
);

CREATE TABLE IF NOT EXISTS coord_authority_generations (
  generation_id       TEXT PRIMARY KEY,
  schema_version       TEXT NOT NULL,
  manifest_sha256      TEXT NOT NULL UNIQUE CHECK (length(manifest_sha256) = 64),
  sources_json         TEXT NOT NULL CHECK (json_valid(sources_json)),
  counts_json          TEXT NOT NULL CHECK (json_valid(counts_json)),
  published_by         TEXT NOT NULL,
  published_at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS coord_authority_receipts (
  receipt_id          TEXT PRIMARY KEY,
  receipt_kind        TEXT NOT NULL CHECK (receipt_kind IN ('activation', 'rollback')),
  generation_id       TEXT,
  source_id           TEXT NOT NULL,
  source_change_seq   INTEGER NOT NULL,
  receipt_json        TEXT NOT NULL CHECK (json_valid(receipt_json)),
  receipt_sha256      TEXT NOT NULL CHECK (length(receipt_sha256) = 64),
  created_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS coord_authority_versions (
  authority_version_id INTEGER PRIMARY KEY AUTOINCREMENT,
  authority_kind       TEXT NOT NULL CHECK (authority_kind IN ('plane', 'lineage', 'value_pin')),
  work_id              TEXT NOT NULL,
  head_version         INTEGER NOT NULL CHECK (head_version >= 1),
  generation_id        TEXT NOT NULL REFERENCES coord_authority_generations(generation_id),
  payload_json         TEXT NOT NULL CHECK (json_valid(payload_json)),
  content_sha256       TEXT NOT NULL CHECK (length(content_sha256) = 64),
  evidence_ref         TEXT NOT NULL,
  created_at           REAL NOT NULL,
  UNIQUE(authority_kind, work_id, head_version),
  UNIQUE(authority_kind, work_id, generation_id)
);
CREATE INDEX IF NOT EXISTS ix_coord_authority_versions_generation
  ON coord_authority_versions(generation_id, authority_kind, work_id);

CREATE TABLE IF NOT EXISTS coord_authority_heads (
  authority_kind       TEXT NOT NULL CHECK (authority_kind IN ('plane', 'lineage', 'value_pin')),
  work_id              TEXT NOT NULL,
  authority_version_id INTEGER NOT NULL REFERENCES coord_authority_versions(authority_version_id),
  generation_id        TEXT NOT NULL REFERENCES coord_authority_generations(generation_id),
  content_sha256       TEXT NOT NULL CHECK (length(content_sha256) = 64),
  updated_at           REAL NOT NULL,
  PRIMARY KEY(authority_kind, work_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_coord_authority_heads_generation
  ON coord_authority_heads(generation_id, authority_kind, work_id);

CREATE TABLE IF NOT EXISTS coord_source_state (
  source_name          TEXT PRIMARY KEY CHECK (source_name = 'context_traversal'),
  source_id            TEXT NOT NULL CHECK (length(source_id) = 32),
  source_schema_epoch  TEXT NOT NULL,
  source_change_seq    INTEGER NOT NULL DEFAULT 0
                       CHECK (typeof(source_change_seq) = 'integer'
                              AND source_change_seq >= 0
                              AND source_change_seq < 9223372036854775807),
  changed_at           REAL NOT NULL,
  last_table           TEXT NOT NULL,
  last_operation       TEXT NOT NULL
) WITHOUT ROWID;

INSERT OR IGNORE INTO coord_source_state (
  source_name, source_id, source_schema_epoch, source_change_seq,
  changed_at, last_table, last_operation
) VALUES (
  'context_traversal', lower(hex(randomblob(16))), 'coord-source-seq.r1', 0,
  (julianday('now') - 2440587.5) * 86400.0, 'migration', 'install'
);

ALTER TABLE work_items ADD COLUMN authority_declaration_json TEXT;

CREATE TRIGGER IF NOT EXISTS coord_exact_new_work_declaration
BEFORE INSERT ON work_items BEGIN
  SELECT CASE WHEN (SELECT COUNT(*) FROM coord_authority_policy
                     WHERE policy_id='exact_authority') != 1
    THEN RAISE(ABORT, 'exact-authority policy singleton missing') END;
  SELECT CASE WHEN
    (SELECT enforcement_mode FROM coord_authority_policy
      WHERE policy_id='exact_authority')='enforce'
    AND (
      NEW.authority_declaration_json IS NULL
      OR json_valid(NEW.authority_declaration_json) != 1
      OR json_extract(NEW.authority_declaration_json, '$.classification_state')
           NOT IN ('adjudicated', 'needs_review')
      OR length(COALESCE(json_extract(NEW.authority_declaration_json,
                                      '$.authority_source_sha256'), '')) != 64
      OR lower(json_extract(NEW.authority_declaration_json, '$.authority_source_sha256'))
           GLOB '*[^0-9a-f]*'
      OR lower(json_extract(NEW.authority_declaration_json, '$.authority_source_sha256'))
           != coord_declaration_core_sha256(NEW.authority_declaration_json)
      OR json_extract(NEW.authority_declaration_json, '$.record_kind') != 'work'
      OR json_extract(NEW.authority_declaration_json, '$.work_id') != NEW.work_id
      OR (
        json_extract(NEW.authority_declaration_json, '$.classification_state')='adjudicated'
        AND (
          json_extract(NEW.authority_declaration_json, '$.subject_plane')
            NOT IN ('product','harness','infrastructure','shared')
          OR length(COALESCE(json_extract(NEW.authority_declaration_json, '$.domain'),''))=0
          OR length(COALESCE(json_extract(NEW.authority_declaration_json, '$.program_id'),''))=0
          OR length(COALESCE(json_extract(NEW.authority_declaration_json, '$.workstream_id'),''))=0
          OR length(COALESCE(json_extract(NEW.authority_declaration_json, '$.episode_id'),''))=0
          OR length(COALESCE(json_extract(NEW.authority_declaration_json, '$.span_id'),''))=0
          OR json_type(NEW.authority_declaration_json, '$.pinned') NOT IN ('true','false')
          OR json_extract(NEW.authority_declaration_json, '$.semantic_value_state') != 'unrated'
        )
      )
      OR (
        json_extract(NEW.authority_declaration_json, '$.classification_state')='needs_review'
        AND json_extract(NEW.authority_declaration_json, '$.authority_source_kind')
              NOT IN ('legacy_projection_quarantine','legacy_grouping_quarantine')
      )
    )
    THEN RAISE(ABORT, 'explicit typed exact-authority declaration required for new work')
  END;
END;

CREATE TRIGGER IF NOT EXISTS coord_authority_versions_no_update
BEFORE UPDATE ON coord_authority_versions BEGIN
  SELECT RAISE(ABORT, 'coord authority versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS coord_authority_versions_no_delete
BEFORE DELETE ON coord_authority_versions BEGIN
  SELECT RAISE(ABORT, 'coord authority versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS coord_authority_generations_no_update
BEFORE UPDATE ON coord_authority_generations BEGIN
  SELECT RAISE(ABORT, 'coord authority generations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS coord_authority_generations_no_delete
BEFORE DELETE ON coord_authority_generations BEGIN
  SELECT RAISE(ABORT, 'coord authority generations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS coord_authority_policy_no_delete
BEFORE DELETE ON coord_authority_policy BEGIN
  SELECT RAISE(ABORT, 'coord authority policy singleton cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS coord_authority_policy_no_insert
BEFORE INSERT ON coord_authority_policy
WHEN EXISTS (SELECT 1 FROM coord_authority_policy WHERE policy_id='exact_authority')
BEGIN
  SELECT RAISE(ABORT, 'coord authority policy singleton already exists');
END;
CREATE TRIGGER IF NOT EXISTS coord_authority_receipts_no_update
BEFORE UPDATE ON coord_authority_receipts BEGIN
  SELECT RAISE(ABORT, 'coord authority receipts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS coord_authority_receipts_no_delete
BEFORE DELETE ON coord_authority_receipts BEGIN
  SELECT RAISE(ABORT, 'coord authority receipts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS coord_authority_heads_consistent_insert
BEFORE INSERT ON coord_authority_heads BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM coord_authority_versions v
     WHERE v.authority_version_id=NEW.authority_version_id
       AND v.authority_kind=NEW.authority_kind
       AND v.work_id=NEW.work_id
       AND v.generation_id=NEW.generation_id
       AND v.content_sha256=NEW.content_sha256
  ) THEN RAISE(ABORT, 'coord authority head/version mismatch') END;
END;
CREATE TRIGGER IF NOT EXISTS coord_authority_heads_consistent_update
BEFORE UPDATE ON coord_authority_heads BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM coord_authority_versions v
     WHERE v.authority_version_id=NEW.authority_version_id
       AND v.authority_kind=NEW.authority_kind
       AND v.work_id=NEW.work_id
       AND v.generation_id=NEW.generation_id
       AND v.content_sha256=NEW.content_sha256
  ) THEN RAISE(ABORT, 'coord authority head/version mismatch') END;
END;
CREATE TRIGGER IF NOT EXISTS coord_source_state_no_delete
BEFORE DELETE ON coord_source_state BEGIN
  SELECT RAISE(ABORT, 'coord source singleton cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS coord_source_state_guard_update
BEFORE UPDATE ON coord_source_state BEGIN
  SELECT CASE WHEN NEW.source_name != OLD.source_name
    OR NEW.source_id != OLD.source_id
    OR NEW.source_schema_epoch != OLD.source_schema_epoch
    OR NEW.source_change_seq != OLD.source_change_seq + 1
    THEN RAISE(ABORT, 'coord source state update violates monotonic custody') END;
END;

CREATE TRIGGER IF NOT EXISTS coord_exact_new_work_materialize_heads
AFTER INSERT ON work_items
WHEN (SELECT enforcement_mode FROM coord_authority_policy
       WHERE policy_id='exact_authority')='enforce'
BEGIN
  INSERT INTO coord_authority_versions(
    authority_kind,work_id,head_version,generation_id,payload_json,
    content_sha256,evidence_ref,created_at
  ) VALUES (
    'plane', NEW.work_id, 1, 'coord-live-declarations-r1',
    NEW.authority_declaration_json,
    coord_canonical_json_sha256(NEW.authority_declaration_json),
    json_extract(NEW.authority_declaration_json,'$.authority_source_kind') || ':' ||
      json_extract(NEW.authority_declaration_json,'$.authority_source_sha256'),
    (julianday('now')-2440587.5)*86400.0
  );
  INSERT INTO coord_authority_heads(
    authority_kind,work_id,authority_version_id,generation_id,content_sha256,updated_at
  ) SELECT authority_kind,work_id,authority_version_id,generation_id,content_sha256,
           (julianday('now')-2440587.5)*86400.0
      FROM coord_authority_versions
     WHERE authority_kind='plane' AND work_id=NEW.work_id AND head_version=1;

  INSERT INTO coord_authority_versions(
    authority_kind,work_id,head_version,generation_id,payload_json,
    content_sha256,evidence_ref,created_at
  ) SELECT
    'lineage', NEW.work_id, 1, 'coord-live-declarations-r1',
    NEW.authority_declaration_json,
    coord_canonical_json_sha256(NEW.authority_declaration_json),
    json_extract(NEW.authority_declaration_json,'$.authority_source_kind') || ':' ||
      json_extract(NEW.authority_declaration_json,'$.authority_source_sha256'),
    (julianday('now')-2440587.5)*86400.0
  WHERE json_extract(NEW.authority_declaration_json,'$.classification_state')='adjudicated';
  INSERT INTO coord_authority_heads(
    authority_kind,work_id,authority_version_id,generation_id,content_sha256,updated_at
  ) SELECT authority_kind,work_id,authority_version_id,generation_id,content_sha256,
           (julianday('now')-2440587.5)*86400.0
      FROM coord_authority_versions
     WHERE authority_kind='lineage' AND work_id=NEW.work_id AND head_version=1;

  INSERT INTO coord_authority_versions(
    authority_kind,work_id,head_version,generation_id,payload_json,
    content_sha256,evidence_ref,created_at
  ) SELECT
    'value_pin', NEW.work_id, 1, 'coord-live-declarations-r1',
    NEW.authority_declaration_json,
    coord_canonical_json_sha256(NEW.authority_declaration_json),
    json_extract(NEW.authority_declaration_json,'$.authority_source_kind') || ':' ||
      json_extract(NEW.authority_declaration_json,'$.authority_source_sha256'),
    (julianday('now')-2440587.5)*86400.0
  WHERE json_extract(NEW.authority_declaration_json,'$.classification_state')='adjudicated';
  INSERT INTO coord_authority_heads(
    authority_kind,work_id,authority_version_id,generation_id,content_sha256,updated_at
  ) SELECT authority_kind,work_id,authority_version_id,generation_id,content_sha256,
           (julianday('now')-2440587.5)*86400.0
      FROM coord_authority_versions
     WHERE authority_kind='value_pin' AND work_id=NEW.work_id AND head_version=1;
END;

DROP VIEW IF EXISTS v_coord_exact_authority_heads;
CREATE VIEW v_coord_exact_authority_heads AS
SELECT h.authority_kind, h.work_id, h.generation_id, h.content_sha256,
       h.updated_at, v.head_version, v.payload_json, v.evidence_ref,
       v.created_at
  FROM coord_authority_heads h
  JOIN coord_authority_versions v
    ON v.authority_version_id = h.authority_version_id;

CREATE TRIGGER IF NOT EXISTS coord_seq_work_items_ai AFTER INSERT ON work_items BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='work_items', last_operation='insert'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_work_items_au AFTER UPDATE ON work_items BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='work_items', last_operation='update'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_work_items_ad AFTER DELETE ON work_items BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='work_items', last_operation='delete'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_claims_ai AFTER INSERT ON claims BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='claims', last_operation='insert'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_claims_au AFTER UPDATE ON claims BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='claims', last_operation='update'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_claims_ad AFTER DELETE ON claims BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='claims', last_operation='delete'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_events_ai AFTER INSERT ON events BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='events', last_operation='insert'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_events_au AFTER UPDATE ON events BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='events', last_operation='update'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_events_ad AFTER DELETE ON events BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='events', last_operation='delete'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_authority_heads_ai AFTER INSERT ON coord_authority_heads BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='coord_authority_heads', last_operation='insert'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_authority_heads_au AFTER UPDATE ON coord_authority_heads BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='coord_authority_heads', last_operation='update'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_authority_heads_ad AFTER DELETE ON coord_authority_heads BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='coord_authority_heads', last_operation='delete'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_agent_sessions_ai AFTER INSERT ON agent_sessions BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='agent_sessions', last_operation='insert'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_agent_sessions_au AFTER UPDATE ON agent_sessions BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='agent_sessions', last_operation='update'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_agent_sessions_ad AFTER DELETE ON agent_sessions BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='agent_sessions', last_operation='delete'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_runs_ai AFTER INSERT ON runs BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='runs', last_operation='insert'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_runs_au AFTER UPDATE ON runs BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='runs', last_operation='update'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_runs_ad AFTER DELETE ON runs BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='runs', last_operation='delete'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_artifacts_ai AFTER INSERT ON artifacts BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='artifacts', last_operation='insert'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_artifacts_au AFTER UPDATE ON artifacts BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='artifacts', last_operation='update'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_artifacts_ad AFTER DELETE ON artifacts BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='artifacts', last_operation='delete'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_display_titles_ai AFTER INSERT ON display_titles BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='display_titles', last_operation='insert'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_display_titles_au AFTER UPDATE ON display_titles BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='display_titles', last_operation='update'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_display_titles_ad AFTER DELETE ON display_titles BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='display_titles', last_operation='delete'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_authority_versions_ai AFTER INSERT ON coord_authority_versions BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='coord_authority_versions', last_operation='insert'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_authority_generations_ai AFTER INSERT ON coord_authority_generations BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='coord_authority_generations', last_operation='insert'
    WHERE source_name='context_traversal';
END;
CREATE TRIGGER IF NOT EXISTS coord_seq_authority_policy_au AFTER UPDATE ON coord_authority_policy BEGIN
  UPDATE coord_source_state SET source_change_seq=source_change_seq+1,
    changed_at=(julianday('now')-2440587.5)*86400.0,
    last_table='coord_authority_policy', last_operation='update'
    WHERE source_name='context_traversal';
END;
