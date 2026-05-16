"""Регистрация всех slash-команд бота.

Команды разделены по группам:
- /set-review-channel, /set-log-channel, /set-log-level — конфигурация каналов
  (требуют administrator)
- /whitelist add|remove|list — управление белым списком (manage_messages)
- /ignore channel|role add|remove|list — игнор-листы (administrator)
- /override-verdict — ретроактивный пересмотр решения (manage_messages)
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from loguru import logger

from bot.api_client import (
    ModerationAPIConflictError,
    ModerationAPIError,
    ModerationAPINotFoundError,
)

if TYPE_CHECKING:
    from bot.client import ModeratorBot


# Discord message ID (snowflake) — 17–20 цифр.
# Ссылка имеет вид https://discord.com/channels/{guild_id}/{channel_id}/{message_id}
_MESSAGE_ID_RE = re.compile(r"(\d{17,20})")
_MESSAGE_LINK_RE = re.compile(
    r"discord\.com/channels/(\d{17,20})/(\d{17,20})/(\d{17,20})",
)


def _parse_message_reference(value: str, expected_guild_id: int) -> int | None:
    """Извлекает message_id из голого ID или discord-ссылки.

    Возвращает None если ничего не подходит, и -1 если это ссылка
    на сообщение другого сервера (нельзя править чужие вердикты).
    """
    value = value.strip()
    if link := _MESSAGE_LINK_RE.search(value):
        guild_id_in_link = int(link.group(1))
        if guild_id_in_link != expected_guild_id:
            return -1
        return int(link.group(3))
    if match := _MESSAGE_ID_RE.search(value):
        return int(match.group(1))
    return None


def register_commands(bot: "ModeratorBot") -> None:
    """Подключает все команды к дереву бота."""
    tree = bot.tree

    # -------------------- Каналы и логирование --------------------

    @tree.command(
        name="set-review-channel",
        description="Канал, куда бот будет слать спорные сообщения для разметки",
    )
    @app_commands.describe(channel="Текстовый канал для модерации спорных решений")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def set_review_channel(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        await _patch_settings_and_reply(
            interaction, review_channel_id=str(channel.id),
            success_msg=f"Канал спорных решений установлен: {channel.mention}",
        )

    @tree.command(
        name="set-log-channel",
        description="Канал для аудит-логов вердиктов (block/flag/all по выбору)",
    )
    @app_commands.describe(channel="Текстовый канал для логов модерации")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def set_log_channel(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        await _patch_settings_and_reply(
            interaction, log_channel_id=str(channel.id),
            success_msg=f"Канал логов установлен: {channel.mention}",
        )

    @tree.command(
        name="set-log-level",
        description="Какие вердикты слать в лог-канал",
    )
    @app_commands.describe(level="Уровень логирования")
    @app_commands.choices(level=[
        app_commands.Choice(name="off — ничего", value="off"),
        app_commands.Choice(name="block — только блокировки", value="block"),
        app_commands.Choice(name="block_flag — блок + спорные (рекомендуется)",
                            value="block_flag"),
        app_commands.Choice(name="all — все включая allow",
                            value="all"),
    ])
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def set_log_level(
        interaction: discord.Interaction,
        level: app_commands.Choice[str],
    ) -> None:
        await _patch_settings_and_reply(
            interaction, log_level=level.value,
            success_msg=f"Уровень логирования установлен: `{level.value}`",
        )

    # -------------------- Whitelist --------------------

    whitelist_group = app_commands.Group(
        name="whitelist",
        description="Управление белым списком фраз (обходят модерацию)",
        default_permissions=discord.Permissions(manage_messages=True),
        guild_only=True,
    )

    @whitelist_group.command(
        name="add", description="Добавить фразу в белый список",
    )
    @app_commands.describe(phrase="Фраза или слово, которое всегда разрешено")
    async def whitelist_add(
        interaction: discord.Interaction, phrase: str,
    ) -> None:
        if not _ensure_guild(interaction):
            return
        try:
            entry = await bot.api.add_whitelist_phrase(
                guild_id=str(interaction.guild_id),
                phrase=phrase,
                added_by=str(interaction.user.id),
            )
        except ModerationAPIConflictError:
            await interaction.response.send_message(
                "Эта фраза уже есть в белом списке.", ephemeral=True,
            )
            return
        except ModerationAPIError as e:
            await _api_error_reply(interaction, e)
            return
        bot.guild_cache.invalidate(str(interaction.guild_id))
        await interaction.response.send_message(
            f"Добавлено в белый список (id={entry['id']}): `{phrase}`",
            ephemeral=True,
        )

    @whitelist_group.command(
        name="remove", description="Удалить фразу из белого списка по ID",
    )
    @app_commands.describe(phrase_id="ID записи из /whitelist list")
    async def whitelist_remove(
        interaction: discord.Interaction, phrase_id: int,
    ) -> None:
        if not _ensure_guild(interaction):
            return
        try:
            await bot.api.delete_whitelist_phrase(
                guild_id=str(interaction.guild_id), phrase_id=phrase_id,
            )
        except ModerationAPINotFoundError:
            await interaction.response.send_message(
                "Запись с таким ID не найдена.", ephemeral=True,
            )
            return
        except ModerationAPIError as e:
            await _api_error_reply(interaction, e)
            return
        bot.guild_cache.invalidate(str(interaction.guild_id))
        await interaction.response.send_message(
            f"Удалено: id={phrase_id}", ephemeral=True,
        )

    @whitelist_group.command(
        name="list", description="Показать все фразы белого списка",
    )
    async def whitelist_list(interaction: discord.Interaction) -> None:
        if not _ensure_guild(interaction):
            return
        try:
            items = await bot.api.list_whitelist(str(interaction.guild_id))
        except ModerationAPIError as e:
            await _api_error_reply(interaction, e)
            return
        if not items:
            await interaction.response.send_message(
                "Белый список пуст.", ephemeral=True,
            )
            return
        embed = discord.Embed(
            title="Белый список",
            description="\n".join(
                f"`{item['id']}` — {item['phrase']}" for item in items
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    tree.add_command(whitelist_group)

    # -------------------- Ignore (каналы и роли) --------------------

    ignore_group = app_commands.Group(
        name="ignore",
        description="Игнор-списки каналов и ролей (не модерируются)",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    ignore_channel = app_commands.Group(
        name="channel",
        description="Каналы, которые бот не модерирует",
        parent=ignore_group,
    )

    @ignore_channel.command(name="add", description="Добавить канал в игнор")
    @app_commands.describe(channel="Канал, который бот должен игнорировать")
    async def ignore_channel_add(
        interaction: discord.Interaction, channel: discord.TextChannel,
    ) -> None:
        if not _ensure_guild(interaction):
            return
        try:
            await bot.api.add_ignored_channel(
                guild_id=str(interaction.guild_id),
                channel_id=str(channel.id),
                added_by=str(interaction.user.id),
            )
        except ModerationAPIConflictError:
            await interaction.response.send_message(
                f"{channel.mention} уже в игнор-списке.", ephemeral=True,
            )
            return
        except ModerationAPIError as e:
            await _api_error_reply(interaction, e)
            return
        bot.guild_cache.invalidate(str(interaction.guild_id))
        await interaction.response.send_message(
            f"{channel.mention} добавлен в игнор. Сообщения отсюда не "
            "модерируются и не сохраняются для обучения.",
            ephemeral=True,
        )

    @ignore_channel.command(name="remove",
                            description="Убрать канал из игнор-списка")
    @app_commands.describe(channel="Канал, который снова будет модерироваться")
    async def ignore_channel_remove(
        interaction: discord.Interaction, channel: discord.TextChannel,
    ) -> None:
        if not _ensure_guild(interaction):
            return
        try:
            await bot.api.remove_ignored_channel(
                guild_id=str(interaction.guild_id),
                channel_id=str(channel.id),
            )
        except ModerationAPINotFoundError:
            await interaction.response.send_message(
                f"{channel.mention} не был в игнор-списке.", ephemeral=True,
            )
            return
        except ModerationAPIError as e:
            await _api_error_reply(interaction, e)
            return
        bot.guild_cache.invalidate(str(interaction.guild_id))
        await interaction.response.send_message(
            f"{channel.mention} убран из игнора.", ephemeral=True,
        )

    @ignore_channel.command(name="list", description="Список игнор-каналов")
    async def ignore_channel_list(interaction: discord.Interaction) -> None:
        if not _ensure_guild(interaction):
            return
        try:
            cfg = await bot.api.get_guild_settings(str(interaction.guild_id))
        except ModerationAPIError as e:
            await _api_error_reply(interaction, e)
            return
        ids = cfg.get("ignored_channel_ids") or []
        if not ids:
            await interaction.response.send_message(
                "Список пуст.", ephemeral=True,
            )
            return
        lines: list[str] = []
        for cid in ids:
            ch = interaction.guild.get_channel(int(cid)) if interaction.guild else None
            lines.append(f"- {ch.mention if ch else f'`{cid}` (удалён)'}")
        embed = discord.Embed(
            title="Игнор-каналы", description="\n".join(lines),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    ignore_role = app_commands.Group(
        name="role",
        description="Роли, чьих участников бот не модерирует",
        parent=ignore_group,
    )

    @ignore_role.command(name="add", description="Добавить роль в игнор")
    @app_commands.describe(role="Роль, чьих обладателей не модерировать")
    async def ignore_role_add(
        interaction: discord.Interaction, role: discord.Role,
    ) -> None:
        if not _ensure_guild(interaction):
            return
        try:
            await bot.api.add_ignored_role(
                guild_id=str(interaction.guild_id),
                role_id=str(role.id),
                added_by=str(interaction.user.id),
            )
        except ModerationAPIConflictError:
            await interaction.response.send_message(
                f"{role.mention} уже в игнор-списке.", ephemeral=True,
            )
            return
        except ModerationAPIError as e:
            await _api_error_reply(interaction, e)
            return
        bot.guild_cache.invalidate(str(interaction.guild_id))
        await interaction.response.send_message(
            f"Носители роли {role.mention} больше не модерируются.",
            ephemeral=True,
        )

    @ignore_role.command(name="remove",
                         description="Убрать роль из игнор-списка")
    @app_commands.describe(role="Роль, чьи обладатели снова будут модерироваться")
    async def ignore_role_remove(
        interaction: discord.Interaction, role: discord.Role,
    ) -> None:
        if not _ensure_guild(interaction):
            return
        try:
            await bot.api.remove_ignored_role(
                guild_id=str(interaction.guild_id),
                role_id=str(role.id),
            )
        except ModerationAPINotFoundError:
            await interaction.response.send_message(
                f"{role.mention} не была в игнор-списке.", ephemeral=True,
            )
            return
        except ModerationAPIError as e:
            await _api_error_reply(interaction, e)
            return
        bot.guild_cache.invalidate(str(interaction.guild_id))
        await interaction.response.send_message(
            f"{role.mention} убрана из игнора.", ephemeral=True,
        )

    @ignore_role.command(name="list", description="Список игнор-ролей")
    async def ignore_role_list(interaction: discord.Interaction) -> None:
        if not _ensure_guild(interaction):
            return
        try:
            cfg = await bot.api.get_guild_settings(str(interaction.guild_id))
        except ModerationAPIError as e:
            await _api_error_reply(interaction, e)
            return
        ids = cfg.get("ignored_role_ids") or []
        if not ids:
            await interaction.response.send_message(
                "Список пуст.", ephemeral=True,
            )
            return
        lines: list[str] = []
        for rid in ids:
            role = interaction.guild.get_role(int(rid)) if interaction.guild else None
            lines.append(f"- {role.mention if role else f'`{rid}` (удалена)'}")
        embed = discord.Embed(
            title="Игнор-роли", description="\n".join(lines),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    tree.add_command(ignore_group)

    # -------------------- Override verdict --------------------

    @tree.command(
        name="override-verdict",
        description="Пересмотреть решение бота по message ID или ссылке",
    )
    @app_commands.describe(
        message="Message ID или ссылка на сообщение",
        label="Какая метка должна быть на самом деле",
        reason="Опционально: почему меняем (для аудита)",
    )
    @app_commands.choices(label=[
        app_commands.Choice(name="Не токсично", value="neutral"),
        app_commands.Choice(name="Спорно", value="uncertain"),
        app_commands.Choice(name="Токсично", value="toxic"),
    ])
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def override_verdict(
        interaction: discord.Interaction,
        message: str,
        label: app_commands.Choice[str],
        reason: str | None = None,
    ) -> None:
        if not _ensure_guild(interaction):
            return

        parsed = _parse_message_reference(message, int(interaction.guild_id))
        if parsed is None:
            await interaction.response.send_message(
                "Не удалось извлечь message ID. Используйте либо чистый ID, "
                "либо ссылку на сообщение (правый клик → Copy Message Link).",
                ephemeral=True,
            )
            return
        if parsed == -1:
            await interaction.response.send_message(
                "Эта ссылка ведёт на сообщение другого сервера — пересмотреть "
                "вердикт можно только в рамках своего сервера.",
                ephemeral=True,
            )
            return
        message_id = parsed

        try:
            verdict = await bot.api.get_verdict_by_message(
                guild_id=str(interaction.guild_id),
                message_id=str(message_id),
            )
        except ModerationAPIError as e:
            await _api_error_reply(interaction, e)
            return
        if verdict is None:
            await interaction.response.send_message(
                "Это сообщение не было модерировано (возможно, оно из игнор-канала "
                "или от участника с игнор-ролью/правом администратора).",
                ephemeral=True,
            )
            return

        try:
            await bot.api.update_review_entry(
                verdict_id=int(verdict["id"]),
                status="rejected",
                corrected_label=label.value,
                reviewer_id=str(interaction.user.id),
                notes=reason,
            )
        except ModerationAPIError as e:
            await _api_error_reply(interaction, e)
            return

        await interaction.response.send_message(
            f"Вердикт по сообщению `{message_id}` помечен как `{label.value}`. "
            "Решение сохранено для дообучения модели.",
            ephemeral=True,
        )


# -------------------- Вспомогательные --------------------


def _ensure_guild(interaction: discord.Interaction) -> bool:
    if interaction.guild_id is None or interaction.guild is None:
        # @guild_only декоратор обычно отсеивает DM, но на всякий случай.
        return False
    return True


async def _api_error_reply(
    interaction: discord.Interaction, error: ModerationAPIError,
) -> None:
    logger.warning("slash command API error | err={err}", err=str(error))
    text = "Сервис модерации сейчас недоступен, попробуйте позже."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def _patch_settings_and_reply(
    interaction: discord.Interaction,
    *,
    success_msg: str,
    **fields,
) -> None:
    """Универсальный апдейт настроек гильдии через PATCH + инвалидация кеша."""
    if not _ensure_guild(interaction):
        return

    # Доступ к боту через client (он же ModeratorBot).
    from bot.client import ModeratorBot
    bot: ModeratorBot = interaction.client  # type: ignore[assignment]

    try:
        await bot.api.patch_guild_settings(
            guild_id=str(interaction.guild_id), **fields,
        )
    except ModerationAPIError as e:
        await _api_error_reply(interaction, e)
        return
    bot.guild_cache.invalidate(str(interaction.guild_id))
    await interaction.response.send_message(success_msg, ephemeral=True)
