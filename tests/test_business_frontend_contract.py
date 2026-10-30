from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "web"


def read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def test_login_registration_and_identity_are_present() -> None:
    html = read("index.html")
    auth = read("auth.js")
    for marker in (
        'id="login-form"',
        'id="register-form"',
        'id="identity-name"',
        'id="logout-button"',
        "data-admin-only",
    ):
        assert marker in html
    assert "/api/v1/auth/login" in auth
    assert "/api/v1/auth/register" in auth
    assert "/api/v1/auth/me" in auth
    assert "/api/v1/auth/logout" in auth


def test_login_and_registration_are_separate_views() -> None:
    html = read("index.html")
    auth = read("auth.js")
    assert 'id="login-view"' in html
    assert 'id="register-view"' in html
    login_view = html.split('id="login-view"', 1)[1].split('id="register-view"', 1)[0]
    register_view = html.split('id="register-view"', 1)[1].split('id="business-view"', 1)[0]
    assert 'id="login-form"' in login_view
    assert 'id="register-form"' not in login_view
    assert 'id="register-form"' in register_view
    assert 'id="login-form"' not in register_view
    assert 'data-auth-target="register"' in html
    assert 'data-auth-target="login"' in html
    assert "showRegister" in auth
    assert "register-password-confirm" in html and "register-password-confirm" in auth
    assert "#/register" in auth and "#/login" in auth


def test_list_pages_navigate_to_separate_detail_views() -> None:
    html = read("index.html")
    business = read("business.js")
    for list_view, detail_view, container, back in (
        ("orders-view", "order-detail-view", "order-detail", "back-to-orders"),
        ("devices-view", "device-detail-view", "device-detail", "back-to-devices"),
        ("tickets-view", "ticket-detail-view", "ticket-detail", "back-to-tickets"),
    ):
        assert f'id="{detail_view}"' in html
        assert f'id="{back}"' in html
        list_section = html.split(f'id="{list_view}"', 1)[1].split(f'id="{detail_view}"', 1)[0]
        assert f'id="{container}"' not in list_section
        assert f"showView('{detail_view.removesuffix('-view')}')" in business
    assert "back-to-orders" in business
    assert "back-to-devices" in business
    assert "back-to-tickets" in business


def test_support_open_toggle_lives_in_the_document_list() -> None:
    html = read("index.html")
    app = read("app.js")
    # 独立的「售后资料」「资料开放」页面已删除，能力收敛到知识库文档列表。
    assert 'id="support-view"' not in html
    assert 'id="admin-support-view"' not in html
    assert 'data-view="support"' not in html
    assert 'data-view="admin-support"' not in html
    assert "/api/v1/admin/support-documents" not in app
    assert 'data-toggle-support' in app
    assert "/support`" in app
    assert "method: 'PATCH'" in app
    assert "support_open" in app


def test_csrf_is_sent_for_writes_and_cookie_is_not_readable_by_js() -> None:
    auth = read("auth.js")
    assert "X-CSRF-Token" in auth
    assert "business_csrf" in auth
    assert "business_session" not in auth
    assert "credentials: 'same-origin'" in auth


def test_401_stops_polling_and_clears_sensitive_dom() -> None:
    auth = read("auth.js")
    app = read("app.js")
    business = read("business.js")
    tool = read("tool-lab.js")
    assert "auth:unauthorized" in auth
    assert "clearBusinessDom" in auth
    assert "stopDocumentPolling" in app
    assert "auth:unauthorized" in business
    assert "auth:unauthorized" in tool
    assert "tool-result" in auth


def test_admin_only_navigation_is_hidden_for_customers() -> None:
    html = read("index.html")
    auth = read("auth.js")
    assert html.count("data-admin-only") >= 6
    assert "querySelectorAll('[data-admin-only]')" in auth
    assert "querySelectorAll('[data-customer-only]')" in auth


def test_ticket_page_submits_a_real_ticket_and_reuses_the_operation_key() -> None:
    html = read("index.html")
    business = read("business.js")
    assert "会真实创建工单" in html
    assert "/api/v1/operations" in business
    assert "reserveTicketOperation" in business
    assert "operation_id: pending.id" in business
    assert "OPERATION_RESULT_UNKNOWN" in business
    assert "ticket-reconcile" in html
    assert "state.pendingOperation.signature" not in business


def test_tool_lab_loads_server_catalog_and_never_accepts_an_execution_user() -> None:
    html = read("index.html")
    tool = read("tool-lab.js")
    assert "/api/v1/tool-lab/tools" in tool
    assert "/api/v1/tool-lab/execute" in tool
    assert "当前登录" in tool
    assert "tool-parameters" in html
    assert "operation_id" in tool
    assert "user_id" not in tool.split("tool-execute-form")[0]


def test_user_visible_values_are_escaped_or_text_content() -> None:
    business = read("business.js")
    tool = read("tool-lab.js")
    assert "escapeHtml" in business and "escapeHtml" in tool
    assert "textContent = JSON.stringify" in tool
    assert "innerHTML = `" in business
    assert "escapeHtml(" in business


def test_scripts_load_in_dependency_order() -> None:
    html = read("index.html")
    assert html.index("/static/auth.js") < html.index("/static/app.js")
    assert html.index("/static/app.js") < html.index("/static/business.js")
    assert html.index("/static/business.js") < html.index("/static/tool-lab.js")
    for asset in ("business.css", "business.js", "tool-lab.js", "auth.js"):
        assert asset in html
