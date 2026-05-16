import discord
import httpx
from discord import app_commands
from loguru import logger

from bot.api_client import ModerationAPIClient, ModerationAPIError
from bot.commands import register_commands
from bot.config import settings
from bot.guild_cache import GuildCache, GuildCacheEntry
from bot.image_sources import (
    IMAGE_CONTENT_TYPES,
    content_is_only_image_urls,
    iter_message_images,
)
from bot.review_view import ReviewView, handle_review_interaction


def build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    return intents


MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024  # совпадает с лимитом таски

# Action → допустим ли он в лог-канале при текущем log_level.
# Используется для фильтрации отправок в log channel.
_LOG_LEVEL_ACTIONS: dict[str, frozenset[str]] = {
    "off": frozenset(),
    "block": frozenset({"block"}),
    "block_flag": frozenset({"block", "flag"}),
    "all": frozenset({"allow", "flag", "block"}),
}


class ModeratorBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=build_intents())
        self.api = ModerationAPIClient(base_url=settings.moderation_api_url)
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=True,
        )
        self.tree = app_commands.CommandTree(self)
        self.guild_cache = GuildCache(self.api)
        register_commands(self)

    async def setup_hook(self) -> None:
        # Кнопки разметки переживают рестарт благодаря обработчику в
        # on_interaction (он матчит custom_id по префиксу review:).
        # PersistentView регистрировать не нужно — все компонент-интеракции
        # всё равно проходят через on_interaction.
        try:
            await self.tree.sync()
            logger.info("slash commands synced globally")
        except discord.HTTPException as e:
            logger.warning("slash commands sync failed: {err}", err=str(e))

    async def close(self) -> None:
        await self.api.close()
        await self._http.aclose()
        await super().close()

    async def on_ready(self) -> None:
        logger.info("Bot connected as {user}", user=self.user)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        # Делегируем клики по review-кнопкам единому обработчику.
        # Slash-команды обрабатывает CommandTree автоматически — сюда не приходят.
        if interaction.type is discord.InteractionType.component:
            handled = await handle_review_interaction(interaction, self.api)
            if handled:
                return

    async def on_message(self, message: discord.Message) -> None:
        skip_reason = await self._should_skip_moderation(message)
        if skip_reason is not None:
            logger.debug(
                "skip moderation | reason={r} | author={a} | channel={c}",
                r=skip_reason,
                a=getattr(message.author, "id", "?"),
                c=getattr(message.channel, "id", "?"),
            )
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

    # ------------------------- Skip rules -------------------------

    async def _should_skip_moderation(
        self, message: discord.Message,
    ) -> str | None:
        """Решает, нужно ли модерировать сообщение.

        Возвращает текстовую причину skip'а или None, если сообщение
        должно пройти модерацию. Порядок: от дешёвых хардкод-инвариантов
        к настраиваемым per-guild спискам.

        ВАЖНО: skip означает полное отсутствие модерации — нет ни вызова API,
        ни записи в Verdict, ни отправки в любые Discord-каналы. Содержимое
        таких сообщений не попадает в обучающую выборку.
        """
        # 1. Хардкод-инварианты
        if message.author.bot:
            return "author_is_bot"
        if not message.guild:
            return "direct_message"

        # 2. Администраторы (включая владельца сервера) — всегда доверенные.
        #    Проверяем permissions_for, чтобы учесть и роль-овны, и явный
        #    administrator-флаг, и канал-специфичные overrides.
        author_perms = message.channel.permissions_for(message.author)
        if author_perms.administrator:
            return "author_has_admin_permission"

        # 3. Per-guild настраиваемые игнор-списки
        try:
            cfg: GuildCacheEntry = await self.guild_cache.get(message.guild.id)
        except Exception:
            # Кеш на случай ошибок сам возвращает пустой entry, но на
            # всякий случай — не блокируем модерацию из-за сбоя кеша.
            logger.exception("guild cache unexpected error")
            return None

        if str(message.channel.id) in cfg.ignored_channel_ids:
            return "channel_in_ignore_list"
        author_role_ids = {str(r.id) for r in getattr(message.author, "roles", [])}
        if author_role_ids & cfg.ignored_role_ids:
            return "author_has_ignored_role"

        return None

    # ------------------------- Verdict handling -------------------------

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
            await self._delete_message(message, source)
        elif action == "flag":
            logger.warning(
                "FLAGGED | source={src} | message_id={mid} | score={s:.3f}",
                src=source, mid=message.id, s=score,
            )

        # Отправка в review/log-каналы. Делается ПОСЛЕ применения действия
        # (delete), чтобы embed в review-канале не содержал ссылку на
        # уже удалённое сообщение для action=block... хотя ссылка всё
        # равно может работать (Discord сохраняет history). Главное —
        # не блокируем основное действие.
        try:
            cfg = await self.guild_cache.get(message.guild.id)
        except Exception:
            logger.exception("guild cache failure during apply_verdicts")
            return

        await self._post_to_review_channel(
            message, source, worst_verdict, cfg,
        )
        await self._post_to_log_channel(
            message, source, worst_verdict, cfg,
        )

    async def _delete_message(
        self, message: discord.Message, source: str,
    ) -> None:
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

    async def _post_to_review_channel(
        self,
        original: discord.Message,
        source: str,
        verdict: dict,
        cfg: GuildCacheEntry,
    ) -> None:
        if verdict["action"] != "flag":
            return
        if not cfg.review_channel_id:
            return
        verdict_id = verdict.get("verdict_id")
        if verdict_id is None:
            logger.debug(
                "skip review post: no verdict_id | message={mid}",
                mid=original.id,
            )
            return

        channel = self._resolve_text_channel(original.guild, cfg.review_channel_id)
        if channel is None:
            logger.warning(
                "review channel not found or wrong type | guild={g} | id={c}",
                g=original.guild.id, c=cfg.review_channel_id,
            )
            return

        embed = self._build_review_embed(original, source, verdict)
        view = ReviewView(verdict_id=int(verdict_id), api=self.api)
        try:
            sent = await channel.send(embed=embed, view=view)
        except (discord.Forbidden, discord.HTTPException) as e:
            logger.warning(
                "failed to post review message | channel={c} | err={err}",
                c=channel.id, err=str(e),
            )
            return

        try:
            await self.api.create_review_entry(
                verdict_id=int(verdict_id),
                review_message_id=str(sent.id),
            )
        except ModerationAPIError as e:
            logger.warning(
                "failed to create review entry | verdict={v} | err={err}",
                v=verdict_id, err=str(e),
            )

    async def _post_to_log_channel(
        self,
        original: discord.Message,
        source: str,
        verdict: dict,
        cfg: GuildCacheEntry,
    ) -> None:
        if not cfg.log_channel_id:
            return
        allowed = _LOG_LEVEL_ACTIONS.get(cfg.log_level, frozenset())
        if verdict["action"] not in allowed:
            return

        channel = self._resolve_text_channel(original.guild, cfg.log_channel_id)
        if channel is None:
            return

        embed = self._build_log_embed(original, source, verdict)
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as e:
            logger.debug(
                "failed to post log message | channel={c} | err={err}",
                c=channel.id, err=str(e),
            )

    @staticmethod
    def _resolve_text_channel(
        guild: discord.Guild | None, channel_id: str,
    ) -> discord.TextChannel | None:
        if guild is None:
            return None
        ch = guild.get_channel(int(channel_id))
        if isinstance(ch, discord.TextChannel):
            return ch
        return None

    @staticmethod
    def _build_review_embed(
        message: discord.Message, source: str, verdict: dict,
    ) -> discord.Embed:
        score = verdict.get("score", 0.0)
        action = verdict.get("action", "?")
        embed = discord.Embed(
            title="Спорное сообщение",
            description=(
                f"**Автор:** {message.author.mention} (`{message.author.id}`)\n"
                f"**Канал:** {message.channel.mention}\n"
                f"**Источник:** {source}\n"
                f"**Action:** `{action}` · **Score:** `{score:.3f}`\n"
                f"[Перейти к оригиналу]({message.jump_url})"
            ),
            color=discord.Color.orange(),
        )
        content = message.content
        if source == "image":
            extracted = verdict.get("extracted_text") or "(пусто)"
            embed.add_field(
                name="Распознанный текст",
                value=extracted[:1000] or "(пусто)",
                inline=False,
            )
        if content:
            embed.add_field(
                name="Контент",
                value=content[:1000],
                inline=False,
            )
        categories = verdict.get("categories") or {}
        if categories:
            cat_str = ", ".join(
                f"{k}={v:.2f}" if isinstance(v, (int, float)) else f"{k}={v}"
                for k, v in list(categories.items())[:6]
            )
            embed.add_field(name="Категории", value=cat_str, inline=False)
        embed.set_footer(text="Нажмите кнопку, чтобы разметить решение")
        return embed

    @staticmethod
    def _build_log_embed(
        message: discord.Message, source: str, verdict: dict,
    ) -> discord.Embed:
        score = verdict.get("score", 0.0)
        action = verdict.get("action", "?")
        color = {
            "block": discord.Color.red(),
            "flag": discord.Color.orange(),
            "allow": discord.Color.green(),
        }.get(action, discord.Color.greyple())
        embed = discord.Embed(
            description=(
                f"`{action}` · score `{score:.3f}` · {source}\n"
                f"{message.author.mention} в {message.channel.mention} "
                f"· [перейти]({message.jump_url})"
            ),
            color=color,
        )
        extracted = verdict.get("extracted_text")
        if source == "image" and extracted:
            embed.add_field(
                name="OCR",
                value=extracted[:200],
                inline=False,
            )
        return embed
