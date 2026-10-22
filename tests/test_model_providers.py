from app.api.routes.model_providers import PROVIDER_CATALOGS, _mask_key, _provider
from app.core.config import settings


def test_provider_key_is_masked() -> None:
    masked = _mask_key("abcd-secret-value-wxyz")

    assert masked == "abcd••••••••wxyz"
    assert "secret" not in masked


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
