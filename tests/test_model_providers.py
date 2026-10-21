from app.api.routes.model_providers import PROVIDER_CATALOGS, _mask_key, _provider
from app.core.config import settings


def test_provider_key_is_masked() -> None:
    masked = _mask_key("abcd-secret-value-wxyz")

    assert masked == "abcd••••••••wxyz"
    assert "secret" not in masked


def test_zhipu_provider_exposes_embedding_and_pipeline_status(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_api_key", "abcd-secret-value-wxyz")
    monkeypatch.setattr(settings, "llm_base_url", "https://example.test/v4")
    monkeypatch.setattr(settings, "embedding_model", "embedding-3")
    monkeypatch.setattr(settings, "embedding_dimension", 2048)

    provider = _provider("zhipu")

    assert provider.configured is True
    assert provider.embedding_model == "embedding-3"
    assert provider.current_pipeline_compatible is True
    assert provider.api_key_masked != "abcd-secret-value-wxyz"


def test_gpt_provider_is_visible_but_unconfigured_without_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "gpt_api_key", None)

    provider = _provider("gpt")

    assert provider.configured is False
    assert provider.embedding_model == "text-embedding-3-small"
    assert provider.api_key_masked is None


def test_provider_catalog_exposes_switchable_chat_models() -> None:
    catalog = PROVIDER_CATALOGS["zhipu"]

    assert catalog.chat_models == ["glm-5.1", "glm-5.2", "glm-5-turbo"]
    assert catalog.embedding_dimensions["embedding-3"] == 2048


def test_saved_configuration_response_contract_does_not_include_plain_api_key() -> None:
    from app.api.schemas_v2 import ModelConfigurationResponse

    fields = ModelConfigurationResponse.model_fields

    assert "api_key" not in fields
    assert "api_key_masked" in fields
