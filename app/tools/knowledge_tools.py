"""Real support-knowledge search tool.

Reuses the customer-facing support service. 型号维度不入库：调用方（Agent）负责先用
账号下的设备型号限定问题，或在信息不足时向用户澄清。
"""

from __future__ import annotations

from pydantic import Field

from app.business import knowledge as service
from app.business.errors import BusinessError, Unavailable
from app.tool_runtime import ExecutionContext, ToolArguments, ToolRejected
from app.tools.base import load_principal, read_session


class SearchKnowledgeArguments(ToolArguments):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=10)


async def search_knowledge(
    arguments: SearchKnowledgeArguments, context: ExecutionContext
) -> dict:
    async with read_session() as session:
        principal = await load_principal(session, context)
        try:
            return await service.search_support_knowledge(
                session,
                principal,
                query=arguments.query,
                top_k=arguments.top_k,
            )
        except Unavailable as exc:
            raise ToolRejected(exc.code, exc.message) from exc
        except BusinessError as exc:
            raise ToolRejected(exc.code, exc.message) from exc
