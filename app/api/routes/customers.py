from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.auth.schemas import CustomerPage, CustomerSummary
from app.auth.users import list_customers
from app.db.mysql_session import get_mysql_session

router = APIRouter(
    prefix="/api/v1/admin/customers",
    tags=["admin-customers"],
    dependencies=[Depends(require_admin)],
)


@router.get("", response_model=CustomerPage)
async def search_customers(
    q: str = Query(default="", max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_mysql_session),
) -> CustomerPage:
    rows, total = await list_customers(session, query=q, page=page, page_size=page_size)
    return CustomerPage(
        items=[
            CustomerSummary(
                id=row.id,
                username=row.username,
                display_name=row.display_name,
                status=row.status,
                created_at=row.created_at.isoformat() if row.created_at else "",
            )
            for row in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )
