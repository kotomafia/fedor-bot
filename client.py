import discord
import httpx
from loguru import logger

from bot.api_client import ModerationAPIClient, ModerationAPIError
from bot.config import settings
from bot.image_sources import (
    IMAGE_CONTENT_TYPES,
    content_is_only_image_urls,
    iter_message_images,
)


def build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    return intents


MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024  # совпадает с лимитом таски


class ModeratorBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=build_intents())
        self.api = ModerationAPIClient(base_url=settings.moderation_api_url)
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self.api.close()
        await self._http.aclose()
        await super().close()

    async def on_ready(self) -> None:
        logger.info("Bot connected as {user}", user=self.user)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not message.guild:
            return

        actions_to_take: list[tuple[str, dict]] = []

        # 1. Текст (не голая ссылка на картинку — её обработаем ниже)
        if message.content and not content_is_only_image_urls(message.content):
            try:
                verdict = await self.api.moderate_text(
                    message_id=str(message.id),
                    guild_id=str(message.guild.id),
                    channel_id=str(message.channel.id),
                    author_id=str(message.author.id),
                    content=message.content,
                )
                actions_to_take.append(("text", verdict))
            except ModerationAPIError:
                pass  # fail-open

        # 2. Картинки: вложения, embed-превью, URL в тексте
        async for image_bytes, source_hint in iter_message_images(
            message,
            self._http,
            max_bytes=MAX_DOWNLOAD_BYTES,
            image_content_types=IMAGE_CONTENT_TYPES,
        ):
            try:
                verdict = await self.api.moderate_image(
                    message_id=str(message.id),
                    guild_id=str(message.guild.id),
                    channel_id=str(message.channel.id),
                    author_id=str(message.author.id),
                    image_bytes=image_bytes,
                    source_hint=source_hint,
                )
                actions_to_take.append(("image", verdict))
            except ModerationAPIError as e:
                logger.warning(
                    "image moderation failed | hint={h} | err={err}",
                    h=source_hint,
                    err=str(e),
                )

        await self._apply_verdicts(message, actions_to_take)

    async def _apply_verdicts(
        self, message: discord.Message, verdicts: list[tuple[str, dict]],
    ) -> None:
        if not verdicts:
            return

        priority = {"allow": 0, "flag": 1, "block": 2}
        worst = max(verdicts, key=lambda v: priority[v[1]["action"]])
        source, worst_verdict = worst

        action = worst_verdict["action"]
        score = worst_verdict["score"]

        logger.info(
            "final verdict | source={src} | author={a} | score={s:.3f} | action={act} | extracted={ext!r}",
            src=source,
            a=str(message.author),
            s=score,
            act=action,
            ext=worst_verdict.get("extracted_text"),
        )

        if action == "block":
            try:
                await message.delete()
                reason = "сообщения" if source == "text" else "содержимого изображения"
                await message.channel.send(
                    f"{message.author.mention}, ваше сообщение удалено по правилам сервера ({reason}).",
                    delete_after=10,
                )
            except discord.Forbidden:
                logger.warning("no permission to delete | channel={ch}", ch=message.channel.id)
            except discord.NotFound:
                pass
        elif action == "flag":
            logger.warning(
                "FLAGGED | source={src} | message_id={mid} | score={s:.3f} | content_len={clen}",
                src=source,
                mid=message.id,
                s=score,
                clen=len(message.content or ""),
            )
