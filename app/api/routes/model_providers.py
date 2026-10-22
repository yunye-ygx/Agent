from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas_v2 import (
    ModelConfigurationCreate,
    ModelConfigurationResponse,
    ModelConfigurationTestResponse,
    ModelConfigurationUpdate,
    ModelProviderCatalogResponse,
    ModelProviderResponse,
    ModelProviderTestRequest,
    ModelProviderTestResponse,
)
from app.core.config import settings
from app.db import mysql_repository as repository
from app.db.mysql_session import get_mysql_session
from app.rag.embedding import EmbeddingError, OpenAICompatibleEmbeddingClient

router = APIRouter(prefix="/api/v1/model-providers", tags=["model-providers"])


PROVIDER_CATALOGS: dict[str, ModelProviderCatalogResponse] = {
    "qwen": ModelProviderCatalogResponse(
        provider_key="qwen",
        display_name="通义千问（百炼）",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        chat_models=["qwen-plus"],
        embedding_models=["text-embedding-v4"],
        embedding_dimensions={"text-embedding-v4": 2048},
        capabilities=["Chat", "Embedding"],
    ),
    "custom": ModelProviderCatalogResponse(
        provider_key="custom",
        display_name="自定义 OpenAI Compatible",
        base_url="",
        chat_models=[],
        embedding_models=[],
        capabilities=["Chat", "Embedding"],
    ),
}


def _mask_key(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "••••••••"
    return f"{value[:4]}••••••••{value[-4:]}"


def _catalog(provider_key: str) -> ModelProviderCatalogResponse:
    try:
        return PROVIDER_CATALOGS[provider_key]
    except KeyError as exc:
        raise HTTPException(status_code=422, detail="不支持的模型提供商") from exc


def _configuration_capabilities(
    provider_key: str, embedding_model: str | None
) -> list[str]:
    catalog = PROVIDER_CATALOGS.get(provider_key)
    capabilities = list(catalog.capabilities if catalog else ["Chat"])
    if embedding_model and "Embedding" not in capabilities:
        capabilities.append("Embedding")
    return capabilities


def _serialize_configuration(item) -> ModelConfigurationResponse:
    return ModelConfigurationResponse(
        id=item.id,
        provider_key=item.provider_key,
        provider_name=item.provider_name,
        display_name=item.display_name,
        base_url=item.base_url,
        api_key_masked=_mask_key(item.api_key),
        chat_model=item.chat_model,
        embedding_model=item.embedding_model,
        embedding_dimension=item.embedding_dimension,
        capabilities=item.capabilities or [],
        is_default=item.is_default,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.get("/catalog", response_model=list[ModelProviderCatalogResponse])
async def list_provider_catalog() -> list[ModelProviderCatalogResponse]:
    """Return provider/model choices for the configuration UI."""

    return list(PROVIDER_CATALOGS.values())


@router.get("/configurations", response_model=list[ModelConfigurationResponse])
async def list_model_configurations(
    session: AsyncSession = Depends(get_mysql_session),
) -> list[ModelConfigurationResponse]:
    return [
        _serialize_configuration(item)
        for item in await repository.list_model_configurations(session)
    ]


@router.get("/configurations/default", response_model=ModelConfigurationResponse)
async def get_default_model_configuration(
    session: AsyncSession = Depends(get_mysql_session),
) -> ModelConfigurationResponse:
    items = await repository.list_model_configurations(session)
    item = next((configuration for configuration in items if configuration.is_default), None)
    if item is None:
        raise HTTPException(status_code=404, detail="尚未设置默认模型配置")
    return _serialize_configuration(item)


@router.get("/configurations/{configuration_id}", response_model=ModelConfigurationResponse)
async def get_model_configuration(
    configuration_id: str,
    session: AsyncSession = Depends(get_mysql_session),
) -> ModelConfigurationResponse:
    item = await repository.get_model_configuration(session, configuration_id)
    if item is None:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    return _serialize_configuration(item)


@router.post(
    "/configurations",
    response_model=ModelConfigurationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_model_configuration(
    payload: ModelConfigurationCreate,
    session: AsyncSession = Depends(get_mysql_session),
) -> ModelConfigurationResponse:
    catalog = _catalog(payload.provider_key)
    item = await repository.create_model_configuration(
        session,
        provider_key=payload.provider_key,
        provider_name=catalog.display_name,
        display_name=payload.display_name.strip(),
        base_url=payload.base_url.strip().rstrip("/"),
        api_key=payload.api_key,
        chat_model=payload.chat_model.strip(),
        embedding_model=payload.embedding_model.strip() if payload.embedding_model else None,
        embedding_dimension=payload.embedding_dimension,
        capabilities=_configuration_capabilities(payload.provider_key, payload.embedding_model),
        is_default=payload.set_default,
    )
    await session.commit()
    await session.refresh(item)
    return _serialize_configuration(item)


@router.put("/configurations/{configuration_id}", response_model=ModelConfigurationResponse)
async def update_model_configuration(
    configuration_id: str,
    payload: ModelConfigurationUpdate,
    session: AsyncSession = Depends(get_mysql_session),
) -> ModelConfigurationResponse:
    item = await repository.get_model_configuration(session, configuration_id)
    if item is None:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    catalog = _catalog(payload.provider_key)
    await repository.update_model_configuration(
        session,
        item,
        provider_key=payload.provider_key,
        provider_name=catalog.display_name,
        display_name=payload.display_name.strip(),
        base_url=payload.base_url.strip().rstrip("/"),
        api_key=payload.api_key,
        chat_model=payload.chat_model.strip(),
        embedding_model=payload.embedding_model.strip() if payload.embedding_model else None,
        embedding_dimension=payload.embedding_dimension,
        capabilities=_configuration_capabilities(payload.provider_key, payload.embedding_model),
        is_default=payload.set_default,
    )
    await session.commit()
    await session.refresh(item)
    return _serialize_configuration(item)


@router.delete("/configurations/{configuration_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_configuration(
    configuration_id: str,
    session: AsyncSession = Depends(get_mysql_session),
) -> None:
    item = await repository.get_model_configuration(session, configuration_id)
    if item is None:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    await repository.delete_model_configuration(session, item)
    await session.commit()


@router.post(
    "/configurations/{configuration_id}/test",
    response_model=ModelConfigurationTestResponse,
)
async def test_saved_model_configuration(
    configuration_id: str,
    request: ModelProviderTestRequest,
    session: AsyncSession = Depends(get_mysql_session),
) -> ModelConfigurationTestResponse:
    item = await repository.get_model_configuration(session, configuration_id)
    if item is None:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(base_url=item.base_url.rstrip("/"), timeout=settings.model_timeout_seconds) as client:
            vector_dimension = None
            if item.embedding_model:
                embedding_payload = {"model": item.embedding_model, "input": [request.text]}
                if item.embedding_model == "text-embedding-v4" and item.embedding_dimension:
                    embedding_payload["dimensions"] = item.embedding_dimension
                response = await client.post(
                    "/embeddings",
                    headers={"Authorization": f"Bearer {item.api_key}"},
                    json=embedding_payload,
                )
                response.raise_for_status()
                data = response.json().get("data")
                vector = data[0].get("embedding") if isinstance(data, list) and data else None
                if not isinstance(vector, list):
                    raise TypeError("Embedding 返回格式不正确")
                vector_dimension = len(vector)
            chat_response = await client.post(
                "/chat/completions",
                headers={"Authorization": f"Bearer {item.api_key}"},
                json={
                    "model": item.chat_model,
                    "messages": [{"role": "user", "content": request.text}],
                    "temperature": 0,
                    "max_tokens": 16,
                },
            )
            chat_response.raise_for_status()
            choices = chat_response.json().get("choices")
            chat_message = choices[0].get("message", {}) if choices else {}
            chat_success = bool(chat_message.get("content") or chat_message.get("reasoning_content"))
        return ModelConfigurationTestResponse(
            configuration_id=item.id,
            provider=item.provider_key,
            success=True,
            message="Chat + Embedding 接口连接成功。" if vector_dimension else "Chat 接口连接成功。",
            model=item.chat_model,
            vector_dimension=vector_dimension,
            chat_success=chat_success,
            latency_ms=round((time.perf_counter() - started) * 1000),
        )
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        return ModelConfigurationTestResponse(
            configuration_id=item.id,
            provider=item.provider_key,
            success=False,
            message=f"连接测试失败：{str(exc)[:300]}",
            model=item.chat_model,
            latency_ms=round((time.perf_counter() - started) * 1000),
        )


def _provider(provider: str) -> ModelProviderResponse:
    if provider == "qwen":
        return ModelProviderResponse(
            provider="qwen",
            display_name="通义千问（百炼）",
            configured=bool(settings.llm_api_key and settings.llm_base_url),
            base_url=settings.llm_base_url or "",
            api_key_masked=_mask_key(settings.llm_api_key),
            chat_model=settings.llm_model,
            embedding_model=settings.embedding_model,
            embedding_dimension=settings.embedding_dimension,
            capabilities=["Chat", "Embedding", "当前解析链路"],
            current_pipeline_compatible=bool(
                settings.llm_api_key and settings.embedding_model and settings.embedding_dimension == 2048
            ),
        )
    raise HTTPException(status_code=404, detail="不支持的模型提供商")


@router.get("", response_model=list[ModelProviderResponse])
async def list_model_providers() -> list[ModelProviderResponse]:
    return [_provider("qwen")]


@router.get("/{provider}", response_model=ModelProviderResponse)
async def get_model_provider(provider: str) -> ModelProviderResponse:
    return _provider(provider)


@router.post("/{provider}/test", response_model=ModelProviderTestResponse)
async def test_model_provider(
    provider: str, request: ModelProviderTestRequest
) -> ModelProviderTestResponse:
    config = _provider(provider)
    if not config.configured:
        return ModelProviderTestResponse(
            provider=provider,
            success=False,
            message="尚未配置 API Key，当前只展示配置状态。",
            model=config.embedding_model,
        )

    started = time.perf_counter()
    try:
        if provider == "qwen":
            vectors = await OpenAICompatibleEmbeddingClient().embed_texts([request.text])
            dimension = len(vectors[0]) if vectors else None
            async with httpx.AsyncClient(
                base_url=settings.llm_base_url.rstrip("/"), timeout=settings.model_timeout_seconds
            ) as client:
                chat_response = await client.post(
                    "/chat/completions",
                    headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                    json={
                        "model": settings.llm_model,
                        "messages": [{"role": "user", "content": request.text}],
                        "temperature": 0,
                        "max_tokens": max(256, settings.llm_max_tokens),
                    },
                )
                chat_response.raise_for_status()
                chat_payload = chat_response.json()
                choices = chat_payload.get("choices")
                chat_message = choices[0].get("message", {}) if choices else {}
                chat_success = bool(chat_message.get("content") or chat_message.get("reasoning_content"))
            return ModelProviderTestResponse(
                provider=provider,
                success=True,
                message="Chat + Embedding 接口连接成功。",
                model=settings.embedding_model,
                vector_dimension=dimension,
                chat_success=chat_success,
                latency_ms=round((time.perf_counter() - started) * 1000),
            )

        raise HTTPException(status_code=404, detail="不支持的模型提供商")
    except (EmbeddingError, httpx.HTTPError, TypeError, ValueError) as exc:
        return ModelProviderTestResponse(
            provider=provider,
            success=False,
            message=f"连接测试失败：{str(exc)[:300]}",
            model=config.embedding_model,
            latency_ms=round((time.perf_counter() - started) * 1000),
        )
