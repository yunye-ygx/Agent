import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from test_a1_lifecycle import db  # noqa: F401

from app.business.models import BusinessBase
from app.db.mysql_models import Base


class _Nested:
    def __init__(self, session: Session):
        self.session = session
        self.transaction = None

    async def __aenter__(self):
        self.transaction = self.session.begin_nested()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.transaction.commit()
        else:
            self.transaction.rollback()
        return False


class AsyncSessionAdapter:
    """Minimal async facade over a sync SQLite Session for service tests.

    SQLite proves transaction outcomes and constraints here; InnoDB row-lock
    behaviour is covered separately by the MySQL integration test.
    """

    def __init__(self, session: Session):
        self.session = session

    async def execute(self, query, *args, **kwargs):
        return self.session.execute(query, *args, **kwargs)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    def add(self, item):
        self.session.add(item)

    async def delete(self, item):
        self.session.delete(item)

    async def get(self, model, key):
        return self.session.get(model, key)

    async def refresh(self, item):
        self.session.refresh(item)

    def begin_nested(self):
        return _Nested(self.session)


@pytest.fixture
def business_db():
    engine = create_engine("sqlite://")
    BusinessBase.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield AsyncSessionAdapter(session)
    engine.dispose()


@pytest.fixture
def knowledge_db():
    """Business tables plus the legacy document/generation tables on one SQLite DB."""

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    BusinessBase.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield AsyncSessionAdapter(session)
    engine.dispose()
