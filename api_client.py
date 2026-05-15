import asyncio
import base64

import httpx
from loguru import logger

from bot.config import settings


class ModerationAPIError(Exception):
    pass


class ModerationTimeoutError(ModerationAPIError):
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