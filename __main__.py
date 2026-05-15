import sys

from loguru import logger

from bot.client import ModeratorBot
from bot.config import settings


def configure_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - <level>{message}</level>",
    )


def main() -> None:
    configure_logging()
    bot = ModeratorBot()
    bot.run(settings.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()