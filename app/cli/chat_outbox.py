import asyncio

from app.chat.outbox_publisher import run_chat_outbox


def main() -> None:
    asyncio.run(run_chat_outbox())


if __name__ == "__main__":
    main()

