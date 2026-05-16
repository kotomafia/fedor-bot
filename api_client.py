import asyncio
import base64
from typing import Any

import httpx
from loguru import logger

from bot.config import settings


class ModerationAPIError(Exception):
    pass


class ModerationTimeoutError(ModerationAPIError):
    pass


class ModerationAPINotFoundError(ModerationAPIError):
    """Сущность не найдена (404), не считается ошибкой сети."""
    pass


class ModerationAPIConflictError(ModerationAPIError):
    """Сущность уже существует (409), например, фраза уже в белом списке."""
    pass


class ModerationAPIClient:
    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=2.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _submit(
        self, *, message_id: str, guild_id: str, channel_id: str,
        author_id: str, content: str,
    ) -> str:
        payload = {
            "message_id": message_id,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "author_id": author_id,
            "content": content,
        }
        try:
            r = await self._client.post("/api/v1/moderate/text", json=payload)
            r.raise_for_status()
            return r.json()["task_id"]
        except httpx.HTTPStatusError as e:
            raise ModerationAPIError(f"submit failed: {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise ModerationAPIError("API unreachable") from e

    async def _poll(self, task_id: str, max_wait: float, interval: float) -> dict:
        deadline = asyncio.get_event_loop().time() + max_wait
        while True:
            try:
                r = await self._client.get(f"/api/v1/moderate/result/{task_id}")
                r.raise_for_status()
                data = r.json()
            except httpx.RequestError as e:
                raise ModerationAPIError("API unreachable during poll") from e

            status = data["status"]
            if status == "success":
                return data["result"]
            if status == "failure":
                raise ModerationAPIError(f"task failed: {data.get('error')}")

            if asyncio.get_event_loop().time() >= deadline:
                raise ModerationTimeoutError(f"task {task_id} did not finish in {max_wait}s")

            await asyncio.sleep(interval)

    async def moderate_text(
        self, *, message_id: str, guild_id: str, channel_id: str,
        author_id: str, content: str,
        max_wait: float = 10.0, poll_interval: float = 0.2,
    ) -> dict:
        task_id = await self._submit(
            message_id=message_id, guild_id=guild_id, channel_id=channel_id,
            author_id=author_id, content=content,
        )
        return await self._poll(task_id, max_wait=max_wait, interval=poll_interval)

    async def moderate_image(
        self, *, message_id: str, guild_id: str, channel_id: str,
        author_id: str, image_bytes: bytes, source_hint: str | None = None,
        max_wait: float = 30.0, poll_interval: float = 0.5,
    ) -> dict:
        task_id = await self._submit_image(
            message_id=message_id, guild_id=guild_id, channel_id=channel_id,
            author_id=author_id, image_bytes=image_bytes, source_hint=source_hint,
        )
        return await self._poll(task_id, max_wait=max_wait, interval=poll_interval)

    async def _submit_image(
        self, *, message_id: str, guild_id: str, channel_id: str,
        author_id: str, image_bytes: bytes, source_hint: str | None,
    ) -> str:
        payload = {
            "message_id": message_id,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "author_id": author_id,
            "image_b64": base64.b64encode(image_bytes).decode("ascii"),
            "source_hint": source_hint,
        }
        try:
            r = await self._client.post("/api/v1/moderate/image", json=payload)
            r.raise_for_status()
            return r.json()["task_id"]
        except httpx.HTTPStatusError as e:
            raise ModerationAPIError(f"image submit failed: {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise ModerationAPIError("API unreachable") from e

    # ------------------------- Guild settings -------------------------

    async def get_guild_settings(self, guild_id: str) -> dict[str, Any]:
        """Один агрегированный запрос: настройки + игнор-листы."""
        try:
            r = await self._client.get(f"/api/v1/guilds/{guild_id}/settings")
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise ModerationAPIError(
                f"get guild settings failed: {e.response.status_code}",
            ) from e
        except httpx.RequestError as e:
            raise ModerationAPIError("API unreachable") from e

    async def patch_guild_settings(
        self, guild_id: str, **fields: Any,
    ) -> dict[str, Any]:
        try:
            r = await self._client.patch(
                f"/api/v1/guilds/{guild_id}/settings", json=fields,
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise ModerationAPIError(
                f"patch guild settings failed: {e.response.status_code}",
            ) from e
        except httpx.RequestError as e:
            raise ModerationAPIError("API unreachable") from e

    # ------------------------- Whitelist -------------------------

    async def list_whitelist(self, guild_id: str) -> list[dict[str, Any]]:
        try:
            r = await self._client.get(f"/api/v1/guilds/{guild_id}/whitelist")
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise ModerationAPIError(
                f"list whitelist failed: {e.response.status_code}",
            ) from e
        except httpx.RequestError as e:
            raise ModerationAPIError("API unreachable") from e

    async def add_whitelist_phrase(
        self, guild_id: str, phrase: str, added_by: str,
    ) -> dict[str, Any]:
        try:
            r = await self._client.post(
                f"/api/v1/guilds/{guild_id}/whitelist",
                json={"phrase": phrase, "added_by": added_by},
            )
            if r.status_code == 409:
                raise ModerationAPIConflictError("phrase already exists")
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise ModerationAPIError(
                f"add whitelist failed: {e.response.status_code}",
            ) from e
        except httpx.RequestError as e:
            raise ModerationAPIError("API unreachable") from e

    async def delete_whitelist_phrase(
        self, guild_id: str, phrase_id: int,
    ) -> None:
        try:
            r = await self._client.delete(
                f"/api/v1/guilds/{guild_id}/whitelist/{phrase_id}",
            )
            if r.status_code == 404:
                raise ModerationAPINotFoundError("phrase not found")
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise ModerationAPIError(
                f"delete whitelist failed: {e.response.status_code}",
            ) from e
        except httpx.RequestError as e:
            raise ModerationAPIError("API unreachable") from e

    # ------------------------- Ignored channels -------------------------

    async def add_ignored_channel(
        self, guild_id: str, channel_id: str, added_by: str,
    ) -> dict[str, Any]:
        try:
            r = await self._client.post(
                f"/api/v1/guilds/{guild_id}/ignored-channels",
                json={"channel_id": channel_id, "added_by": added_by},
            )
            if r.status_code == 409:
                raise ModerationAPIConflictError("channel already ignored")
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise ModerationAPIError(
                f"add ignored channel failed: {e.response.status_code}",
            ) from e
        except httpx.RequestError as e:
            raise ModerationAPIError("API unreachable") from e

    async def remove_ignored_channel(
        self, guild_id: str, channel_id: str,
    ) -> None:
        try:
            r = await self._client.delete(
                f"/api/v1/guilds/{guild_id}/ignored-channels/{channel_id}",
            )
            if r.status_code == 404:
                raise ModerationAPINotFoundError("channel not in ignore list")
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise ModerationAPIError(
                f"remove ignored channel failed: {e.response.status_code}",
            ) from e
        except httpx.RequestError as e:
            raise ModerationAPIError("API unreachable") from e

    # ------------------------- Ignored roles -------------------------

    async def add_ignored_role(
        self, guild_id: str, role_id: str, added_by: str,
    ) -> dict[str, Any]:
        try:
            r = await self._client.post(
                f"/api/v1/guilds/{guild_id}/ignored-roles",
                json={"role_id": role_id, "added_by": added_by},
            )
            if r.status_code == 409:
                raise ModerationAPIConflictError("role already ignored")
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise ModerationAPIError(
                f"add ignored role failed: {e.response.status_code}",
            ) from e
        except httpx.RequestError as e:
            raise ModerationAPIError("API unreachable") from e

    async def remove_ignored_role(
        self, guild_id: str, role_id: str,
    ) -> None:
        try:
            r = await self._client.delete(
                f"/api/v1/guilds/{guild_id}/ignored-roles/{role_id}",
            )
            if r.status_code == 404:
                raise ModerationAPINotFoundError("role not in ignore list")
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise ModerationAPIError(
                f"remove ignored role failed: {e.response.status_code}",
            ) from e
        except httpx.RequestError as e:
            raise ModerationAPIError("API unreachable") from e

    # ------------------------- Review queue -------------------------

    async def create_review_entry(
        self, verdict_id: int, review_message_id: str | None,
    ) -> dict[str, Any]:
        try:
            r = await self._client.post(
                "/api/v1/review",
                json={
                    "verdict_id": verdict_id,
                    "review_message_id": review_message_id,
                },
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise ModerationAPIError(
                f"create review entry failed: {e.response.status_code}",
            ) from e
        except httpx.RequestError as e:
            raise ModerationAPIError("API unreachable") from e

    async def update_review_entry(
        self,
        verdict_id: int,
        *,
        status: str,
        corrected_label: str | None,
        reviewer_id: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": status,
            "corrected_label": corrected_label,
            "reviewer_id": reviewer_id,
        }
        if notes is not None:
            payload["notes"] = notes
        try:
            r = await self._client.patch(
                f"/api/v1/review/{verdict_id}", json=payload,
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise ModerationAPIError(
                f"update review entry failed: {e.response.status_code}",
            ) from e
        except httpx.RequestError as e:
            raise ModerationAPIError("API unreachable") from e

    async def get_verdict_by_message(
        self, guild_id: str, message_id: str,
    ) -> dict[str, Any] | None:
        try:
            r = await self._client.get(
                f"/api/v1/verdicts/by-message/{guild_id}/{message_id}",
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise ModerationAPIError(
                f"get verdict by message failed: {e.response.status_code}",
            ) from e
        except httpx.RequestError as e:
            raise ModerationAPIError("API unreachable") from e