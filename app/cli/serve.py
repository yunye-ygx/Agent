"""Project launcher.

psycopg's async driver cannot run on Windows' default Proactor loop, and Uvicorn
creates its loop before importing ``app.main``. This launcher selects the
Selector policy first, so the LangGraph PostgreSQL saver works in the local web
process too.
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def selector_loop_factory(use_subprocess: bool = False):
    """Uvicorn 0.49+ asks for a factory; return the psycopg-compatible loop."""

    return asyncio.SelectorEventLoop()


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    import uvicorn

    parser = argparse.ArgumentParser(description="启动聊天/RAG API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        loop="app.cli.serve:selector_loop_factory",
    )


if __name__ == "__main__":
    main()