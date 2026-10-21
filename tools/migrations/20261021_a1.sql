-- REVIEW ONLY: not executed by application startup or by the authoring task.
-- Stop ALL old API/worker/publisher processes and back up MySQL/ES first.
-- MySQL DDL auto-commits. Run once on the pre-A1 schema, after a staging rehearsal.
-- Do not resume old code after this migration. Keep the original ES index as backup.

ALTER TABLE knowledge_bases
  ADD COLUMN embedding_model VARCHAR(255) NULL,
  ADD COLUMN embedding_dimension INT NULL,
  ADD COLUMN embedding_base_url VARCHAR(512) NULL;

ALTER TABLE generations
  ADD COLUMN file_version VARCHAR(64) NOT NULL DEFAULT '',
  ADD COLUMN file_location_snapshot VARCHAR(1024) NOT NULL DEFAULT '',
  ADD COLUMN chunk_count INT NOT NULL DEFAULT 0,
  ADD COLUMN published_at DATETIME NULL,
  ADD COLUMN artifacts_available BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE tasks
  ADD COLUMN kind VARCHAR(16) NOT NULL DEFAULT 'PARSE',
  ADD COLUMN source_task_id VARCHAR(36) NULL,
  ADD COLUMN attempt_id VARCHAR(36) NULL,
  ADD COLUMN output_fingerprint VARCHAR(64) NULL,
  ADD COLUMN attempt_count INT NOT NULL DEFAULT 0,
  ADD COLUMN max_attempts INT NOT NULL DEFAULT 4,
  ADD COLUMN dispatch_seq INT NOT NULL DEFAULT 1,
  ADD COLUMN lease_until DATETIME NULL,
  ADD COLUMN execution_deadline DATETIME NULL,
  ADD COLUMN next_attempt_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  ADD INDEX ix_tasks_source_task_id (source_task_id),
  ADD INDEX ix_tasks_lease_until (lease_until);

UPDATE generations g JOIN documents d ON d.id = g.document_id
SET g.file_version = d.file_hash, g.file_location_snapshot = d.object_key,
    g.artifacts_available = FALSE,
    g.config_snapshot = JSON_SET(g.config_snapshot,
      '$.file_type', LOWER(SUBSTRING_INDEX(d.filename, '.', -1)),
      '$.parser.version', 'legacy-unverified');

-- Old workers had no attempt fencing, so unfinished legacy work is cancelled,
-- never revived as if it were a safely owned A1 attempt. Reparse after migration.
UPDATE tasks SET
  max_attempts = max_retry + 1,
  attempt_count = retry_count + IF(status IN ('SUCCESS','FAILED','RUNNING'), 1, 0),
  status = CASE WHEN status = 'SUCCESS' THEN 'SUCCESS'
                WHEN status = 'FAILED' THEN 'FAILED' ELSE 'CANCELLED' END;

UPDATE tasks t JOIN documents d ON d.id = t.document_id
SET t.core_page_start = IF(LOWER(SUBSTRING_INDEX(d.filename,'.',-1)) = 'pdf', GREATEST(0,t.core_page_start-1), 0),
    t.core_page_end = IF(LOWER(SUBSTRING_INDEX(d.filename,'.',-1)) = 'pdf', t.core_page_end, 0),
    t.context_page_start = NULL, t.context_page_end = NULL;

UPDATE generations SET
  status = CASE WHEN status IN ('SUCCESS','FAILED') THEN status ELSE 'CANCELLED' END,
  published_at = IF(status = 'SUCCESS', finished_at, NULL);
UPDATE generations g SET
  completed_task_count = (SELECT COUNT(*) FROM tasks t WHERE t.generation_id=g.id AND t.status='SUCCESS'),
  failed_task_count = (SELECT COUNT(*) FROM tasks t WHERE t.generation_id=g.id AND t.status='FAILED'),
  canceled_task_count = (SELECT COUNT(*) FROM tasks t WHERE t.generation_id=g.id AND t.status='CANCELLED'),
  chunk_count = (SELECT COALESCE(SUM(t.chunk_count),0) FROM tasks t WHERE t.generation_id=g.id AND t.status='SUCCESS');
UPDATE task_dispatch_outbox SET status='DEAD', last_error='Legacy dispatch retired by A1 migration';

-- Preserve the old active pointer. The former building column now means latest run.
UPDATE documents d LEFT JOIN generations g ON g.document_id=d.id
  AND g.version_no=(SELECT MAX(g2.version_no) FROM generations g2 WHERE g2.document_id=d.id)
SET d.building_generation_id=g.id;

UPDATE generations g JOIN documents d ON d.id=g.document_id
SET g.finished_at=COALESCE(g.finished_at,UTC_TIMESTAMP()),
    g.reuse_expires_at=CASE WHEN g.id=d.current_generation_id THEN NULL
      ELSE COALESCE(g.reuse_expires_at,DATE_ADD(UTC_TIMESTAMP(),INTERVAL 1 DAY)) END;

-- Artifact conversion and KB embedding compatibility checks are intentionally
-- separate: tools/migrate_a1_artifacts.py (defaults to an offline plan).
-- Historical attempt counts are approximate; they are not A1 execution evidence.
