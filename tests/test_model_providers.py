from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.routes.model_providers import PROVIDER_CATALOGS, _mask_key, _provider
from app.core.config import settings


@pytest.mark.asyncio
async def test_saved_rerank_connection_test_never_calls_chat(monkeypatch):
    from app.api.routes import model_providers as routes
    from app.api.schemas_v2 import ModelProviderTestRequest

    item = SimpleNamespace(
        id="rank",
        provider_key="rerank",
        base_url="https://provider.test",
        api_key="private",
        chat_model="ranker",
    )
    monkeypatch.setattr(routes.repository, "get_model_configuration", AsyncMock(return_value=item))
    ranker = AsyncMock()
    monkeypatch.setattr(routes, "RerankClient", lambda *args: ranker)

    def no_chat(*args, **kwargs):
        raise AssertionError("Rerank should never call chat/completions")

    monkeypatch.setattr(routes.httpx, "AsyncClient", no_chat)
    result = await routes.test_saved_model_configuration(
        "rank", ModelProviderTestRequest(text="q"), None
    )
    assert result.success
    ranker.score.assert_awaited_once()


def test_provider_key_is_masked() -> None:
    masked = _mask_key("abcd-secret-value-wxyz")

    assert masked == "abcd••••••••wxyz"
    assert "secret" not in masked


def test_rerank_catalog_is_independent_of_chat_and_embedding():
    assert PROVIDER_CATALOGS["rerank"].capabilities == ["Rerank"]


def test_qwen_provider_exposes_embedding_and_pipeline_status(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_api_key", "abcd-secret-value-wxyz")
    monkeypatch.setattr(settings, "llm_base_url", "https://example.test/compatible-mode/v1")
    monkeypatch.setattr(settings, "llm_model", "qwen-plus")
    monkeypatch.setattr(settings, "embedding_model", "text-embedding-v4")
    monkeypatch.setattr(settings, "embedding_dimension", 2048)

    provider = _provider("qwen")

    assert provider.configured is True
    assert provider.embedding_model == "text-embedding-v4"
    assert provider.current_pipeline_compatible is True
    assert provider.api_key_masked != "abcd-secret-value-wxyz"


def test_provider_catalog_exposes_initial_qwen_choices() -> None:
    catalog = PROVIDER_CATALOGS["qwen"]

    assert catalog.chat_models == ["qwen-plus"]
    assert catalog.embedding_models == ["text-embedding-v4"]
    assert catalog.embedding_dimensions["text-embedding-v4"] == 2048


def test_saved_configuration_response_contract_does_not_include_plain_api_key() -> None:
    from app.api.schemas_v2 import ModelConfigurationResponse

    fields = ModelConfigurationResponse.model_fields

    assert "api_key" not in fields
    assert "api_key_masked" in fields
