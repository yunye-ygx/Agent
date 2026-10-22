"""Read the live Model Studio catalog for the selected Qwen endpoint."""

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx


@dataclass(frozen=True)
class ModelChoice:
    id: str
    name: str
    description: str | None = None


@dataclass(frozen=True)
class DiscoveredModels:
    chat_models: list[ModelChoice]
    embedding_models: list[ModelChoice]


class ModelDiscoveryError(Exception):
    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def qwen_models_url(base_url: str) -> str:
    """Map an official OpenAI-compatible URL to its native catalog endpoint."""
    parsed = urlsplit(base_url.strip().rstrip("/"))
    hostname = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not hostname.endswith(".aliyuncs.com")
        or parsed.username
        or parsed.password
        or parsed.port
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"/compatible-mode/v1", "/api/v1"}
    ):
        raise ValueError("千问 Base URL 必须是阿里云官方的 /compatible-mode/v1 地址")
    return urlunsplit((parsed.scheme, parsed.netloc, "/api/v1/models", "", ""))


def _choices(rows: list[dict], *, kind: str) -> list[ModelChoice]:
    found: dict[str, ModelChoice] = {}
    for row in rows:
        model_id = row.get("model")
        capabilities = row.get("capabilities") or []
        if not isinstance(model_id, str) or not model_id:
            continue
        if kind == "chat" and (
            "TG" not in capabilities or not model_id.lower().startswith("qwen")
        ):
            continue
        if kind == "embedding" and (
            "TR" not in capabilities
            or "embedding" not in model_id.lower()
            or "async" in model_id.lower()
        ):
            continue
        found.setdefault(
            model_id,
            ModelChoice(
                id=model_id,
                name=row.get("name") or model_id,
                description=row.get("description") or None,
            ),
        )
    return list(found.values())


async def discover_qwen_models(
    base_url: str, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> DiscoveredModels:
    endpoint = qwen_models_url(base_url)
    headers = {"Authorization": f"Bearer {api_key}"}

    async with httpx.AsyncClient(timeout=20, transport=transport) as client:
        async def fetch(capability: str, *, qwen_only: bool = False) -> list[dict]:
            collected: list[dict] = []
            for page in range(1, 21):
                params = {"capabilities": capability, "page_no": page, "page_size": 100}
                if qwen_only:
                    params["providers"] = "qwen"
                try:
                    response = await client.get(endpoint, headers=headers, params=params)
                except httpx.TimeoutException as exc:
                    raise ModelDiscoveryError("千问模型目录请求超时", status_code=504) from exc
                except httpx.HTTPError as exc:
                    raise ModelDiscoveryError("无法连接千问模型目录") from exc
                if response.status_code in {401, 403}:
                    raise ModelDiscoveryError("千问 API Key 无效或无模型目录权限", status_code=401)
                if response.status_code == 429:
                    raise ModelDiscoveryError("千问模型目录请求过于频繁，请稍后重试", status_code=503)
                if not response.is_success:
                    raise ModelDiscoveryError("千问模型目录暂时不可用")
                try:
                    body = response.json()
                    output = body["output"]
                    rows = output["models"]
                    total = output["total"]
                    if body.get("success") is False or not isinstance(rows, list) or not isinstance(total, int):
                        raise ValueError("invalid model catalog")
                except (ValueError, KeyError, TypeError) as exc:
                    raise ModelDiscoveryError("千问模型目录返回格式不正确") from exc
                collected.extend(row for row in rows if isinstance(row, dict))
                if not rows or len(collected) >= total:
                    return collected
            raise ModelDiscoveryError("千问模型目录分页超过上限，请稍后再试")

        chat_rows = await fetch("TG", qwen_only=True)
        embedding_rows = await fetch("TR")
    return DiscoveredModels(
        chat_models=_choices(chat_rows, kind="chat"),
        embedding_models=_choices(embedding_rows, kind="embedding"),
    )
