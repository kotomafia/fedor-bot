"""Локальный TTL-кеш per-guild настроек на стороне бота.

Один HTTP-вызов к API за каждый guild раз в TTL_SECONDS даёт боту:
- review_channel_id / log_channel_id / log_level
- множества ignored_channel_ids / ignored_role_ids для быстрых проверок в on_message

Кеш инвалидируется явно из slash-команд после мутаций. Если API ушло из строя,
закешированное значение продолжает работать до истечения TTL, после чего
получаем default (skip-чек просто пропустит все правила).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from loguru import logger

from bot.api_client import ModerationAPIClient, ModerationAPIError

TTL_SECONDS = 60.0


@dataclass(frozen=True)
class GuildCacheEntry:
    guild_id: str
    review_channel_id: str | None = None
    log_channel_id: str | None = None
    log_level: str = "block_flag"
    ignored_channel_ids: frozenset[str] = field(default_factory=frozenset)
    ignored_role_ids: frozenset[str] = field(default_factory=frozenset)
    whitelist_count: int = 0


_EMPTY_ENTRY_BY_GUILD: dict[str, GuildCacheEntry] = {}


def _empty(guild_id: str) -> GuildCacheEntry:
    """Дефолт, когда API недоступен и ничего не закешировано."""
    cached = _EMPTY_ENTRY_BY_GUILD.get(guild_id)
    if cached is None:
        cached = GuildCacheEntry(guild_id=guild_id)
        _EMPTY_ENTRY_BY_GUILD[guild_id] = cached
    return cached


class GuildCache:
    def __init__(self, api: ModerationAPIClient, ttl_seconds: float = TTL_SECONDS) -> None:
        self._api = api
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[float, GuildCacheEntry]] = {}
        self._fetch_locks: dict[str, asyncio.Lock] = {}

    def invalidate(self, guild_id: str) -> None:
        self._entries.pop(str(guild_id), None)

    def _fresh(self, guild_id: str) -> GuildCacheEntry | None:
        item = self._entries.get(guild_id)
        if item is None:
            return None
        cached_at, entry = item
        if time.monotonic() - cached_at > self._ttl:
            return None
        return entry

    async def get(self, guild_id: int | str) -> GuildCacheEntry:
        key = str(guild_id)
        cached = self._fresh(key)
        if cached is not None:
            return cached

        lock = self._fetch_locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Повторная проверка после ожидания блокировки — другой воркер
            # мог уже обновить кеш, пока мы стояли в очереди.
            cached = self._fresh(key)
            if cached is not None:
                return cached
            try:
                data = await self._api.get_guild_settings(key)
            except ModerationAPIError as e:
                logger.warning(
                    "guild cache fetch failed | guild={g} | err={err}",
                    g=key, err=str(e),
                )
                return _empty(key)

            entry = GuildCacheEntry(
                guild_id=key,
                review_channel_id=data.get("review_channel_id"),
                log_channel_id=data.get("log_channel_id"),
                log_level=data.get("log_level") or "block_flag",
                ignored_channel_ids=frozenset(data.get("ignored_channel_ids") or []),
                ignored_role_ids=frozenset(data.get("ignored_role_ids") or []),
                whitelist_count=int(data.get("whitelist_count") or 0),
            )
            self._entries[key] = (time.monotonic(), entry)
            return entry
