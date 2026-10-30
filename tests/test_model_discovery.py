from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app.api.routes import model_providers
from app.api.schemas_v2 import ModelDiscoveryRequest
from app.rag.model_discovery import discover_qwen_models, qwen_models_url


def test_qwen_model_catalog_uses_native_endpoint_for_the_selected_region():
    assert qwen_models_url("https://dashscope.aliyuncs.com/compatible-mode/v1") == (
        "https://dashscope.aliyuncs.com/api/v1/models"
    )
    assert qwen_models_url("https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1") == (
        "https://workspace.cn-beijing.maas.aliyuncs.com/api/v1/models"
    )
    with pytest.raises(ValueError):
        qwen_models_url("http://localhost:9000/compatible-mode/v1")


@pytest.mark.asyncio
async def test_live_qwen_catalog_paginates_and_filters_to_usable_models():
    requested = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        capability = request.url.params["capabilities"]
        page = int(request.url.params["page_no"])
        if capability == "TG":
            start, end = (0, 100) if page == 1 else (100, 101)
            rows = [
                {"model": f"qwen-{index}", "name": f"千问 {index}", "capabilities": ["TG"]}
                for index in range(start, end)
            ]
            if page == 2:
                rows.append({"model": "tongyi-intent-detect-v3", "name": "意图分类", "capabilities": ["TG"]})
            total = 102
        else:
            rows = [
                {"model": "text-embedding-v4", "name": "文本向量", "capabilities": ["TR"]},
                {"model": "qwen3-rerank", "name": "重排序", "capabilities": ["TR"]},
                {"model": "text-embedding-async-v2", "name": "异步向量", "capabilities": ["TR"]},
            ]
            total = 3
        return httpx.Response(200, json={"success": True, "output": {"total": total, "models": rows}})

    choices = await discover_qwen_models(
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "secret-key",
        transport=httpx.MockTransport(respond),
    )

    assert len(choices.chat_models) == 101
    assert choices.chat_models[0].id == "qwen-0"
    assert [model.id for model in choices.embedding_models] == ["text-embedding-v4"]
    assert len(requested) == 3
    assert all(request.headers["Authorization"] == "Bearer secret-key" for request in requested)
    assert all(request.url.path == "/api/v1/models" for request in requested)


@pytest.mark.asyncio
async def test_saved_qwen_configuration_discovers_with_server_side_key(monkeypatch):
    saved = SimpleNamespace(
        id="saved", provider_key="qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="saved-secret",
    )
    monkeypatch.setattr(
        model_providers.repository,
        "get_model_configuration",
        AsyncMock(return_value=saved),
    )
    discover = AsyncMock(return_value=SimpleNamespace(chat_models=[], embedding_models=[]))
    monkeypatch.setattr(model_providers, "discover_qwen_models", discover)

    result = await model_providers.discover_models(
        ModelDiscoveryRequest(
            provider_key="qwen", base_url=saved.base_url, configuration_id="saved"
        ),
        None,
    )

    discover.assert_awaited_once_with(saved.base_url, "saved-secret")
    assert result.chat_models == []
    assert "api_key" not in result.model_dump()


@pytest.mark.asyncio
async def test_saved_key_is_not_sent_to_a_changed_base_url(monkeypatch):
    saved = SimpleNamespace(
        id="saved", provider_key="qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="saved-secret",
    )
    monkeypatch.setattr(
        model_providers.repository,
        "get_model_configuration",
        AsyncMock(return_value=saved),
    )

    with pytest.raises(HTTPException) as error:
        await model_providers.discover_models(
            ModelDiscoveryRequest(
                provider_key="qwen",
                base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                configuration_id="saved",
            ),
            None,
        )

    assert error.value.status_code == 422
