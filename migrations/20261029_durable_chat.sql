-- Durable chat execution tables. Additive only; existing chat data is preserved.
CREATE TABLE chat_turns (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  conversation_id VARCHAR(36) NOT NULL,
  user_message_id VARCHAR(36) NOT NULL,
  turn_no INT NOT NULL,
  status VARCHAR(32) NOT NULL,
  latest_run_id VARCHAR(36) NULL,
  active_run_id VARCHAR(36) NULL,
  run_count INT NOT NULL DEFAULT 1,
  max_run_attempts INT NOT NULL DEFAULT 3,
  client_request_id VARCHAR(100) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  closed_at DATETIME(6) NULL,
  CONSTRAINT fk_chat_turn_conversation FOREIGN KEY (conversation_id)
    REFERENCES chat_conversations(id),
  CONSTRAINT fk_chat_turn_user_message FOREIGN KEY (user_message_id)
    REFERENCES chat_messages(id),
  CONSTRAINT uk_chat_turn_number UNIQUE (conversation_id, turn_no),
  CONSTRAINT uk_chat_turn_request UNIQUE (conversation_id, client_request_id),
  INDEX ix_chat_turn_status (conversation_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE chat_runs (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  turn_id VARCHAR(36) NOT NULL,
  attempt_no INT NOT NULL,
  retry_of_run_id VARCHAR(36) NULL,
  trigger_reason VARCHAR(32) NOT NULL,
  status VARCHAR(32) NOT NULL,
  agent_version VARCHAR(100) NOT NULL,
  checkpoint_thread_id VARCHAR(255) NOT NULL,
  latest_checkpoint_id VARCHAR(255) NULL,
  checkpoint_schema_version VARCHAR(50) NOT NULL,
  lease_owner VARCHAR(100) NULL,
  lease_expires_at DATETIME(6) NULL,
  last_heartbeat_at DATETIME(6) NULL,
  lease_generation BIGINT NOT NULL DEFAULT 0,
  recovery_count INT NOT NULL DEFAULT 0,
  max_recovery_count INT NOT NULL DEFAULT 3,
  next_event_seq BIGINT NOT NULL DEFAULT 1,
  error_code VARCHAR(100) NULL,
  error_message TEXT NULL,
  queued_at DATETIME(6) NOT NULL,
  started_at DATETIME(6) NULL,
  finished_at DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  CONSTRAINT fk_chat_run_turn FOREIGN KEY (turn_id) REFERENCES chat_turns(id),
  CONSTRAINT fk_chat_run_retry FOREIGN KEY (retry_of_run_id) REFERENCES chat_runs(id),
  CONSTRAINT uk_chat_run_attempt UNIQUE (turn_id, attempt_no),
  INDEX ix_chat_run_lease (status, lease_expires_at),
  INDEX ix_chat_run_queue (status, queued_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

ALTER TABLE chat_turns
  ADD CONSTRAINT fk_chat_turn_latest_run FOREIGN KEY (latest_run_id) REFERENCES chat_runs(id),
  ADD CONSTRAINT fk_chat_turn_active_run FOREIGN KEY (active_run_id) REFERENCES chat_runs(id);

ALTER TABLE chat_messages
  ADD COLUMN run_id VARCHAR(36) NULL AFTER turn_id,
  ADD COLUMN source_key VARCHAR(200) NULL AFTER run_id,
  ADD CONSTRAINT fk_chat_message_run FOREIGN KEY (run_id) REFERENCES chat_runs(id),
  ADD CONSTRAINT uk_chat_message_projection UNIQUE (run_id, source_key),
  ADD INDEX ix_chat_message_run (run_id, sequence);

CREATE TABLE chat_tool_executions (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  run_id VARCHAR(36) NOT NULL,
  model_response_id VARCHAR(100) NOT NULL,
  assistant_message_id VARCHAR(36) NULL,
  tool_batch_id VARCHAR(100) NOT NULL,
  tool_call_id VARCHAR(200) NOT NULL,
  call_index INT NOT NULL,
  tool_name VARCHAR(100) NOT NULL,
  tool_version VARCHAR(50) NOT NULL,
  arguments_json JSON NOT NULL,
  arguments_hash VARCHAR(64) NOT NULL,
  status VARCHAR(32) NOT NULL,
  result_json JSON NULL,
  result_ref VARCHAR(500) NULL,
  result_hash VARCHAR(64) NULL,
  idempotency_key VARCHAR(200) NULL,
  attempt_count INT NOT NULL DEFAULT 0,
  max_attempts INT NOT NULL DEFAULT 1,
  reused_from_execution_id VARCHAR(36) NULL,
  error_code VARCHAR(100) NULL,
  error_message TEXT NULL,
  started_at DATETIME(6) NULL,
  finished_at DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  CONSTRAINT fk_chat_tool_run FOREIGN KEY (run_id) REFERENCES chat_runs(id),
  CONSTRAINT fk_chat_tool_message FOREIGN KEY (assistant_message_id)
    REFERENCES chat_messages(id),
  CONSTRAINT fk_chat_tool_reused FOREIGN KEY (reused_from_execution_id)
    REFERENCES chat_tool_executions(id),
  CONSTRAINT uk_chat_tool_call UNIQUE (run_id, tool_call_id),
  INDEX ix_chat_tool_batch (run_id, tool_batch_id, call_index)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE chat_run_events (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  run_id VARCHAR(36) NOT NULL,
  seq BIGINT NOT NULL,
  event_type VARCHAR(64) NOT NULL,
  payload_json JSON NOT NULL,
  model_response_id VARCHAR(100) NULL,
  response_segment_id VARCHAR(100) NULL,
  created_at DATETIME(6) NOT NULL,
  expires_at DATETIME(6) NULL,
  CONSTRAINT fk_chat_event_run FOREIGN KEY (run_id) REFERENCES chat_runs(id),
  CONSTRAINT uk_chat_run_event_seq UNIQUE (run_id, seq),
  INDEX ix_chat_run_event_created (run_id, created_at),
  INDEX ix_chat_run_event_expiry (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE chat_outbox_events (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  aggregate_type VARCHAR(32) NOT NULL,
  aggregate_id VARCHAR(36) NOT NULL,
  event_type VARCHAR(64) NOT NULL,
  payload_json JSON NOT NULL,
  deduplication_key VARCHAR(200) NOT NULL,
  status VARCHAR(16) NOT NULL,
  attempt_count INT NOT NULL DEFAULT 0,
  available_at DATETIME(6) NOT NULL,
  locked_by VARCHAR(100) NULL,
  locked_at DATETIME(6) NULL,
  last_error TEXT NULL,
  created_at DATETIME(6) NOT NULL,
  published_at DATETIME(6) NULL,
  CONSTRAINT uk_chat_outbox_dedup UNIQUE (deduplication_key),
  INDEX ix_chat_outbox_available (status, available_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

