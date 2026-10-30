from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_frontend_contains_knowledge_base_and_document_workflows() -> None:
    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")

    for label in ("知识库", "上传文档", "开始解析", "重新解析", "取消解析", "Chunk"):
        assert label in html or label in script
    assert "/api/v1/knowledge-bases" in script
    assert "/api/v1/documents/" in script


def test_knowledge_base_list_supports_batch_delete_and_refetches_after_acceptance() -> None:
    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")

    assert 'id="delete-selected-kbs"' in html
    assert "data-select-all-kbs" in script
    assert "data-select-kb" in script
    assert "/api/v1/knowledge-bases/batch-delete" in script
    assert "await loadKnowledgeBases()" in script


def test_document_table_owns_parse_progress_and_delete_actions() -> None:
    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")

    for marker in (
        "data-parse-doc",
        "data-cancel-doc",
        "data-delete-doc",
        "document-progress",
        "progress-bar",
        "DELETE",
        "解析进度",
        "删除",
    ):
        assert marker in html or marker in script

    detail = html.split('id="document-detail"', 1)[1]
    assert "generation-status" not in detail
    assert "document-actions" not in detail


def test_frontend_connects_chat_ui_for_logged_in_customers() -> None:
    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")

    assert "聊天" in html
    assert '<script src="/static/chat.js"></script>' in html
    assert 'id="chat-conversations"' in html
    assert 'id="chat-messages"' in html
    assert 'data-view="chat" data-admin-only' not in html


def test_frontend_contains_model_provider_settings() -> None:
    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")

    for label in ("模型配置", "data-test-provider", "测试连接"):
        assert label in html or label in script
    assert "/api/v1/model-providers" in script


def test_frontend_model_settings_are_persistent_and_switchable() -> None:
    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "app" / "web" / "app.js").read_text(encoding="utf-8")

    for label in ("新增模型配置", "供应商", "Chat 模型", "保存配置", "设为默认"):
        assert label in html or label in script
    assert "/api/v1/model-providers/catalog" in script
    assert "/api/v1/model-providers/configurations" in script
    assert "api_key_masked" in script
    assert "/api/v1/model-providers/discover-models" in script
    assert 'class="model-dialog-body"' in html
    assert 'id="model-refresh-models"' in html
    assert 'id="model-discovery-status"' in html
