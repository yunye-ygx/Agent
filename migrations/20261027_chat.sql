-- Additive migration only. No changes to business or offline ingestion tables.
CREATE TABLE IF NOT EXISTS chat_conversations (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  user_id VARCHAR(36) NOT NULL,
  title VARCHAR(120) NOT NULL,
  next_sequence INT NOT NULL DEFAULT 1,
  active_turn VARCHAR(36) NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  CONSTRAINT fk_chat_owner FOREIGN KEY (user_id) REFERENCES business_users(id),
  INDEX ix_chat_owner_updated (user_id, updated_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS chat_messages (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  conversation_id VARCHAR(36) NOT NULL,
  sequence INT NOT NULL,
  turn_id VARCHAR(36) NOT NULL,
  role VARCHAR(16) NOT NULL,
  content MEDIUMTEXT NOT NULL,
  tool_calls JSON NULL,
  tool_call_id VARCHAR(200) NULL,
  tool_name VARCHAR(100) NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'pending',
  created_at DATETIME NOT NULL,
  CONSTRAINT fk_chat_message_conversation FOREIGN KEY (conversation_id)
    REFERENCES chat_conversations(id),
  CONSTRAINT uk_chat_message_sequence UNIQUE (conversation_id, sequence),
  INDEX ix_chat_turn (conversation_id, turn_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
