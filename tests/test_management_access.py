import httpx
import pytest
from fastapi import FastAPI

from app.api.routes import (
    customers,
    documents_v2,
    knowledge_bases,
    model_providers,
    retrieval,
    retrieval_experiments,
    system,
)
from app.auth.dependencies import get_current_principal, require_admin
from app.business.principal import Principal

ORIGIN = "http://testserver"

ROUTERS = [
    (knowledge_bases.router, "GET", "/api/v1/knowledge-bases"),
    (documents_v2.router, "GET", "/api/v1/documents/00000000-0000-0000-0000-000000000000"),
    (model_providers.router, "GET", "/api/v1/model-providers"),
    (retrieval.router, "GET", "/api/v1/search?q=x&knowledge_base_id=y"),
    (retrieval_experiments.router, "GET", "/api/v1/retrieval-experiments/session"),
    (customers.router, "GET", "/api/v1/admin/customers"),
    (documents_v2.router, "PATCH", "/api/v1/documents/00000000-0000-0000-0000-000000000000/support"),
    (system.router, "GET", "/api/v1/admin/system/status"),
]


def customer() -> Principal:
    return Principal.for_user(
        user_id="customer-id", username="customer", display_name="客户", role="customer"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("router", "method", "path"), ROUTERS)
async def test_admin_routers_require_login_without_database(router, method, path):
    app = FastAPI()
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        response = await client.request(method, path)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "UNAUTHENTICATED"


@pytest.mark.asyncio
@pytest.mark.parametrize(("router", "method", "path"), ROUTERS)
async def test_admin_routers_reject_customer_principal(router, method, path):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_principal] = customer
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        response = await client.request(method, path)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "ADMIN_REQUIRED"


def test_every_management_router_has_the_admin_dependency():
    for router, _method, _path in ROUTERS:
        assert any(dep.dependency is require_admin for dep in router.dependencies), router.prefix
