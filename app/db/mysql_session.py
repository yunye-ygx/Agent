from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(settings.mysql_url, pool_pre_ping=True, pool_recycle=1800)
AsyncSessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def get_mysql_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionFactory() as session:
        yield session
