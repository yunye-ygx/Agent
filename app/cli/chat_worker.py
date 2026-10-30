import asyncio

from app.chat.worker import run_chat_worker


def main() -> None:
    asyncio.run(run_chat_worker())


if __name__ == "__main__":
    main()

