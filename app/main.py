import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.errors import install_business_error_handler
from app.api.routes.auth import router as auth_router
from app.api.routes.chat import router as chat_router
from app.api.routes.customers import router as customers_router
from app.api.routes.devices import router as devices_router
from app.api.routes.documents_v2 import router as documents_router
from app.api.routes.knowledge_bases import router as knowledge_bases_router
from app.api.routes.model_providers import router as model_providers_router
from app.api.routes.operations import router as operations_router
from app.api.routes.orders import router as orders_router
from app.api.routes.retrieval import router as retrieval_router
from app.api.routes.retrieval_experiments import router as retrieval_experiments_router
from app.api.routes.system import router as system_router
from app.api.routes.tickets import router as tickets_router
from app.api.routes.tool_lab import router as tool_lab_router
from app.bootstrap import initialize_infrastructure
from app.core.config import settings
from app.db.mysql_session import AsyncSessionFactory
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
        # MySQL/business schema is required. RAG dependencies are reported but do
        # not prevent business login/orders/devices/tickets from serving.
        dependency_status = await initialize_infrastructure()
        application.state.dependency_status = dependency_status or {}
        rag_ready = bool((dependency_status or {}).get("rag", {}).get("ready"))
        if settings.run_background_services and rag_ready:
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

app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(customers_router)
app.include_router(orders_router)
app.include_router(devices_router)
app.include_router(knowledge_bases_router)
app.include_router(model_providers_router)
app.include_router(documents_router)
app.include_router(retrieval_router)
app.include_router(retrieval_experiments_router)
app.include_router(tickets_router)
app.include_router(operations_router)
app.include_router(system_router)
app.include_router(tool_lab_router)
install_business_error_handler(app)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web"), name="static")


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "web" / "index.html")


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    """Liveness only; this intentionally does not claim dependency readiness."""

    return {"status": "ok", "environment": settings.app_env}


async def check_database() -> bool:
    try:
        async with AsyncSessionFactory() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@app.get("/health/ready", tags=["system"])
async def readiness(request: Request):
    database_ready = await check_database()
    dependency_status = getattr(request.app.state, "dependency_status", {}) or {}
    rag = dependency_status.get("rag") or {"ready": False, "message": "知识检索尚未初始化"}
    ready = bool(database_ready)
    payload = {
        "status": "ready" if ready else "not_ready",
        "business": {"ready": database_ready},
        "rag": rag,
    }
    return JSONResponse(status_code=200 if ready else 503, content=payload)
