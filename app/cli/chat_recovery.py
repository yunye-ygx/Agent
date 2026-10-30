import asyncio

from app.chat.recovery import run_chat_recovery


def main() -> None:
    asyncio.run(run_chat_recovery())


if __name__ == "__main__":
    main()