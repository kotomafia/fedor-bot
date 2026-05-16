"""Persistent Discord UI view с 3 кнопками для разметки спорных вердиктов.

Кнопки:
  ✅ Верно         → status=approved, corrected_label сохраняется текущим
  ❌ Не токсично   → status=rejected, corrected_label=neutral, score=0.0
  ⬆️ Занижено      → status=rejected, corrected_label=toxic,   score=1.0

Числовые скоры не вводятся вручную модератором — выводятся системно
на стороне API по corrected_label. Это намеренный дизайн: никто не знает
"правильного" числа, важна только семантика метки.

ReviewView спроектирован как persistent view (timeout=None, кнопки с
постоянными custom_id), чтобы кнопки продолжали работать после рестарта
бота. Регистрируется один раз в ModeratorBot.setup_hook через bot.add_view.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from loguru import logger

from bot.api_client import ModerationAPIError

if TYPE_CHECKING:
    from bot.api_client import ModerationAPIClient


# Префикс custom_id, чтобы persistent_view мог обработать клик
# без хранения per-view state на стороне бота.
_BUTTON_PREFIX = "review:"

_DECISION_APPROVE = "approve"
_DECISION_NOT_TOXIC = "not_toxic"
_DECISION_UNDERRATED = "underrated"

# Маппинг кнопки → (status, corrected_label).
# corrected_score проставляется API на основе corrected_label.
_DECISION_MAP: dict[str, tuple[str, str | None]] = {
    _DECISION_APPROVE: ("approved", None),
    _DECISION_NOT_TOXIC: ("rejected", "neutral"),
    _DECISION_UNDERRATED: ("rejected", "toxic"),
}

_DECISION_LABELS: dict[str, str] = {
    _DECISION_APPROVE: "Верно",
    _DECISION_NOT_TOXIC: "Не токсично",
    _DECISION_UNDERRATED: "Занижено",
}


def make_custom_id(verdict_id: int, decision: str) -> str:
    return f"{_BUTTON_PREFIX}{verdict_id}:{decision}"


def parse_custom_id(custom_id: str) -> tuple[int, str] | None:
    if not custom_id.startswith(_BUTTON_PREFIX):
        return None
    parts = custom_id[len(_BUTTON_PREFIX):].split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), parts[1]
    except ValueError:
        return None


class ReviewView(discord.ui.View):
    """View для конкретного verdict_id — используется при ПЕРВИЧНОЙ отправке.

    После рестарта бота клики обрабатывает PersistentReviewView
    (он матчит по префиксу custom_id, без знания verdict_id заранее).
    """

    def __init__(self, verdict_id: int, api: "ModerationAPIClient") -> None:
        super().__init__(timeout=None)
        self.verdict_id = verdict_id
        self._api = api

        self.add_item(_build_button(verdict_id, _DECISION_APPROVE,
                                    discord.ButtonStyle.success, "Верно"))
        self.add_item(_build_button(verdict_id, _DECISION_NOT_TOXIC,
                                    discord.ButtonStyle.secondary, "Не токсично"))
        self.add_item(_build_button(verdict_id, _DECISION_UNDERRATED,
                                    discord.ButtonStyle.danger, "Занижено"))


def _build_button(verdict_id: int, decision: str,
                  style: discord.ButtonStyle, label: str) -> discord.ui.Button:
    btn = discord.ui.Button(
        style=style, label=label, custom_id=make_custom_id(verdict_id, decision),
    )
    return btn


async def handle_review_interaction(
    interaction: discord.Interaction,
    api: "ModerationAPIClient",
) -> bool:
    """Обработчик клика по кнопке разметки.

    Вызывается из ModeratorBot.on_interaction для всех component-кликов
    с custom_id, начинающимся на review:. Возвращает True если интеракция
    была обработана (в т.ч. с ошибкой), False если custom_id не наш.
    """
    if interaction.type is not discord.InteractionType.component:
        return False
    custom_id = (interaction.data or {}).get("custom_id", "")
    if not isinstance(custom_id, str):
        return False
    parsed = parse_custom_id(custom_id)
    if parsed is None:
        return False
    verdict_id, decision = parsed

    if decision not in _DECISION_MAP:
        await interaction.response.send_message(
            "Неизвестное решение.", ephemeral=True,
        )
        return True

    # Право Manage Messages защищает от случайных кликов рядовых участников.
    perms = interaction.user.guild_permissions if interaction.guild else None
    if perms is None or not perms.manage_messages:
        await interaction.response.send_message(
            "Нет прав для разметки (требуется Manage Messages).",
            ephemeral=True,
        )
        return True

    status_value, corrected_label = _DECISION_MAP[decision]
    reviewer_id = str(interaction.user.id)

    try:
        await interaction.response.defer(ephemeral=True, thinking=False)
        await api.update_review_entry(
            verdict_id=verdict_id,
            status=status_value,
            corrected_label=corrected_label,
            reviewer_id=reviewer_id,
        )
    except ModerationAPIError as e:
        logger.warning(
            "review decision failed | verdict={v} | err={err}",
            v=verdict_id, err=str(e),
        )
        await interaction.followup.send(
            "Не удалось записать решение, попробуйте позже.", ephemeral=True,
        )
        return True

    decision_human = _DECISION_LABELS[decision]
    await _finalize_review_message(
        interaction.message, interaction.user, decision_human,
    )
    await interaction.followup.send(
        f"Записано: {decision_human}.", ephemeral=True,
    )
    return True


async def _finalize_review_message(
    message: discord.Message | None,
    reviewer: discord.User | discord.Member,
    decision_human: str,
) -> None:
    """Отключает кнопки и помечает embed решением модератора."""
    if message is None:
        return

    new_view = discord.ui.View(timeout=None)
    for original_btn in _iter_message_buttons(message):
        btn = discord.ui.Button(
            style=original_btn.style,
            label=original_btn.label,
            custom_id=original_btn.custom_id,
            disabled=True,
        )
        new_view.add_item(btn)

    embeds = list(message.embeds)
    if embeds:
        embeds[0].add_field(
            name="Решение модератора",
            value=f"{decision_human} — {reviewer.mention}",
            inline=False,
        )

    try:
        await message.edit(embeds=embeds, view=new_view)
    except (discord.HTTPException, discord.Forbidden) as e:
        logger.warning("failed to finalize review message | err={err}", err=str(e))


def _iter_message_buttons(message: discord.Message):
    """Извлекает кнопки из всех компонентов сообщения."""
    for row in message.components:
        for child in getattr(row, "children", []):
            if isinstance(child, discord.Button):
                yield child
