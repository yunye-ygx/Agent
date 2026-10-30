"""RAG learning application package."""

import asyncio
import sys

# Psycopg's async driver cannot run on Windows' default Proactor loop. This
# package is imported before pytest, CLI and Uvicorn create their event loops.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
