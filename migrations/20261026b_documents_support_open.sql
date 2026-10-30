-- 售后资料开放开关（2026-10-26 调整）
-- 背景：取消 support_knowledge_documents 关联表与「资料开放」页面。
-- 型号维度不再存库；由 Agent 在提问前用设备型号限定或向用户澄清。
-- 本脚本只做三件事：documents 增加 support_open、从旧关联表回填、删除旧表。
-- 用 information_schema 做守卫，可重复执行；不删除任何 documents 记录。

SET @has_column := (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE() AND table_name = 'documents' AND column_name = 'support_open'
);
SET @ddl := IF(@has_column = 0,
  'ALTER TABLE documents ADD COLUMN support_open TINYINT(1) NOT NULL DEFAULT 0',
  'DO 0');
PREPARE support_stmt FROM @ddl;
EXECUTE support_stmt;
DEALLOCATE PREPARE support_stmt;

SET @has_mapping := (
  SELECT COUNT(*) FROM information_schema.tables
  WHERE table_schema = DATABASE() AND table_name = 'support_knowledge_documents'
);
SET @ddl := IF(@has_mapping > 0,
  'UPDATE documents d SET d.support_open = 1 WHERE EXISTS (SELECT 1 FROM support_knowledge_documents m WHERE m.document_id = d.id AND m.enabled = 1)',
  'DO 0');
PREPARE support_stmt FROM @ddl;
EXECUTE support_stmt;
DEALLOCATE PREPARE support_stmt;

SET @ddl := IF(@has_mapping > 0,
  'DROP TABLE support_knowledge_documents',
  'DO 0');
PREPARE support_stmt FROM @ddl;
EXECUTE support_stmt;
DEALLOCATE PREPARE support_stmt;