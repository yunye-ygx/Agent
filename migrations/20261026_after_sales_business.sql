-- 打印机售后业务底座：增量建表（MySQL 8.4）
-- 目标库：由 .env 的 MYSQL_HOST/MYSQL_PORT/MYSQL_DATABASE 指定，当前本地为 rag_demo。
-- 本脚本只新增下列九张业务表，不修改、不清空、不删除任何既有表。
-- 售后资料开放改为 documents.support_open（见 20261026b_documents_support_open.sql）。
-- 执行入口：python -m app.cli.business_schema check / apply --yes
-- DDL 在 MySQL 中隐式提交，无法整体回滚；脚本用 IF NOT EXISTS 支持重复执行。

CREATE TABLE IF NOT EXISTS business_users (
  id VARCHAR(36) NOT NULL,
  username VARCHAR(32) NOT NULL,
  display_name VARCHAR(64) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  role VARCHAR(16) NOT NULL DEFAULT 'customer',
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uk_business_users_username (username),
  CONSTRAINT ck_business_users_role CHECK (role IN ('customer', 'admin')),
  CONSTRAINT ck_business_users_status CHECK (status IN ('active', 'disabled'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS auth_sessions (
  id VARCHAR(36) NOT NULL,
  user_id VARCHAR(36) NOT NULL,
  token_hash VARCHAR(64) NOT NULL,
  csrf_token_hash VARCHAR(64) NOT NULL,
  expires_at DATETIME(6) NOT NULL,
  revoked_at DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uk_auth_sessions_token (token_hash),
  KEY ix_auth_sessions_user_expires (user_id, expires_at),
  CONSTRAINT fk_auth_sessions_user FOREIGN KEY (user_id)
    REFERENCES business_users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS auth_rate_limits (
  bucket_key VARCHAR(200) NOT NULL,
  window_started_at DATETIME(6) NOT NULL,
  attempt_count INT NOT NULL DEFAULT 0,
  expires_at DATETIME(6) NOT NULL,
  PRIMARY KEY (bucket_key),
  KEY ix_auth_rate_limits_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS orders (
  id VARCHAR(36) NOT NULL,
  order_no VARCHAR(64) NOT NULL,
  user_id VARCHAR(36) NOT NULL,
  created_by VARCHAR(36) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'recorded',
  total_amount DECIMAL(14, 2) NOT NULL,
  currency VARCHAR(3) NOT NULL DEFAULT 'CNY',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uk_orders_order_no (order_no),
  KEY ix_orders_user_created (user_id, created_at),
  CONSTRAINT ck_orders_status CHECK (status IN ('recorded', 'completed', 'cancelled')),
  CONSTRAINT ck_orders_total_nonnegative CHECK (total_amount >= 0),
  CONSTRAINT fk_orders_user FOREIGN KEY (user_id)
    REFERENCES business_users (id) ON DELETE RESTRICT,
  CONSTRAINT fk_orders_created_by FOREIGN KEY (created_by)
    REFERENCES business_users (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS order_items (
  id VARCHAR(36) NOT NULL,
  order_id VARCHAR(36) NOT NULL,
  product_name VARCHAR(200) NOT NULL,
  product_model VARCHAR(16) NOT NULL,
  quantity INT NOT NULL,
  unit_price DECIMAL(12, 2) NOT NULL,
  line_amount DECIMAL(14, 2) NOT NULL,
  PRIMARY KEY (id),
  KEY ix_order_items_order (order_id),
  CONSTRAINT ck_order_items_quantity CHECK (quantity BETWEEN 1 AND 1000),
  CONSTRAINT ck_order_items_unit_price CHECK (unit_price >= 0),
  CONSTRAINT ck_order_items_line_amount CHECK (line_amount >= 0),
  CONSTRAINT fk_order_items_order FOREIGN KEY (order_id)
    REFERENCES orders (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS devices (
  id VARCHAR(36) NOT NULL,
  serial_no VARCHAR(64) NOT NULL,
  user_id VARCHAR(36) NOT NULL,
  order_item_id VARCHAR(36) NOT NULL,
  model_code VARCHAR(16) NOT NULL,
  firmware_version VARCHAR(32) NULL,
  purchased_on DATE NOT NULL,
  warranty_until DATE NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uk_devices_serial_no (serial_no),
  KEY ix_devices_user (user_id),
  KEY ix_devices_order_item (order_item_id),
  CONSTRAINT ck_devices_status CHECK (status IN ('active', 'retired')),
  CONSTRAINT fk_devices_user FOREIGN KEY (user_id)
    REFERENCES business_users (id) ON DELETE RESTRICT,
  CONSTRAINT fk_devices_order_item FOREIGN KEY (order_item_id)
    REFERENCES order_items (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS tickets (
  id VARCHAR(36) NOT NULL,
  ticket_no VARCHAR(32) NOT NULL,
  user_id VARCHAR(36) NOT NULL,
  device_id VARCHAR(36) NOT NULL,
  subject VARCHAR(120) NOT NULL,
  description VARCHAR(4000) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'open',
  version INT NOT NULL DEFAULT 1,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY uk_tickets_ticket_no (ticket_no),
  KEY ix_tickets_user_created (user_id, created_at),
  KEY ix_tickets_status_created (status, created_at),
  CONSTRAINT ck_tickets_status CHECK (status IN ('open', 'in_progress', 'resolved', 'closed')),
  CONSTRAINT ck_tickets_version CHECK (version >= 1),
  CONSTRAINT fk_tickets_user FOREIGN KEY (user_id)
    REFERENCES business_users (id) ON DELETE RESTRICT,
  CONSTRAINT fk_tickets_device FOREIGN KEY (device_id)
    REFERENCES devices (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ticket_events (
  id VARCHAR(36) NOT NULL,
  ticket_id VARCHAR(36) NOT NULL,
  actor_id VARCHAR(36) NOT NULL,
  event_type VARCHAR(32) NOT NULL,
  from_status VARCHAR(16) NULL,
  to_status VARCHAR(16) NULL,
  message VARCHAR(2000) NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  KEY ix_ticket_events_ticket_created (ticket_id, created_at),
  CONSTRAINT fk_ticket_events_ticket FOREIGN KEY (ticket_id)
    REFERENCES tickets (id) ON DELETE CASCADE,
  CONSTRAINT fk_ticket_events_actor FOREIGN KEY (actor_id)
    REFERENCES business_users (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS business_operations (
  id VARCHAR(36) NOT NULL,
  user_id VARCHAR(36) NOT NULL,
  action VARCHAR(64) NOT NULL,
  request_hash VARCHAR(64) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'reserved',
  result_resource_id VARCHAR(36) NULL,
  result_snapshot JSON NULL,
  expires_at DATETIME(6) NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  completed_at DATETIME(6) NULL,
  PRIMARY KEY (id),
  KEY ix_business_operations_user_action (user_id, action),
  CONSTRAINT ck_operations_status CHECK (status IN ('reserved', 'succeeded')),
  CONSTRAINT fk_business_operations_user FOREIGN KEY (user_id)
    REFERENCES business_users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
