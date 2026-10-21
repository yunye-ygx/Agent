import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes.documents_v2 import router as documents_router
from app.api.routes.knowledge_bases import router as knowledge_bases_router
from app.api.routes.model_providers import router as model_providers_router
from app.api.routes.retrieval import router as retrieval_router
from app.bootstrap import initialize_infrastructure
from app.core.config import settings
from app.messaging.outbox_publisher import run_publisher
from app.rag.maintenance import run_maintenance
from app.worker import run_worker

logger = logging.getLogger(__name__)


def _report_background_task(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("FastAPI background service stopped unexpectedly: %s", error)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Initialize external indexes/buckets when the service starts."""

    background_tasks: set[asyncio.Task[None]] = set()
    if settings.app_env != "test":
        # These calls are intentionally opt-in for tests and fail loudly in local
        # development when the required infrastructure is not running.
        await initialize_infrastructure()
        if settings.run_background_services:
            services = (
                ("outbox-publisher", run_publisher),
                ("rabbitmq-worker", run_worker),
                ("a1-maintenance", run_maintenance),
            )
            for name, service in services:
                task = asyncio.create_task(service(), name=name)
                task.add_done_callback(_report_background_task)
                background_tasks.add(task)
            application.state.background_services = tuple(background_tasks)
            logger.info(
                "Local background services started: %s", ", ".join(name for name, _ in services)
            )
    try:
        yield
    finally:
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
            application.state.background_services = ()
            logger.info("Local background services stopped")


app = FastAPI(
    title=settings.app_name,
    version="0.2.0",
    description="An observable RAG knowledge-base demo using MySQL, MinIO, RabbitMQ and Elasticsearch.",
    lifespan=lifespan,
)

app.include_router(knowledge_bases_router)
app.include_router(model_providers_router)
app.include_router(documents_router)
app.include_router(retrieval_router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web"), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "web" / "index.html")


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.app_env}
