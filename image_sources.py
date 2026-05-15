import re
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import discord
import httpx
from loguru import logger

IMAGE_CONTENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/bmp",
}

IMAGE_URL_SUFFIX_RE = re.compile(
    r"https?://\S+?\.(?:png|jpe?g|webp|gif|bmp)(?:\?\S*)?",
    re.IGNORECASE,
)
DISCORD_CDN_RE = re.compile(
    r"https?://(?:cdn|media)\.discordapp\.(?:com|net)/\S+",
    re.IGNORECASE,
)


_IMAGE_HOSTS = frozenset({
    "cdn.discordapp.com",
    "media.discordapp.net",
    "i.imgur.com",
    "imgur.com",
})


def _looks_like_image_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    if any(path.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")):
        return True
    host = parsed.netloc.lower()
    if host in _IMAGE_HOSTS:
        return True
    # Discord attachment URLs без расширения в path
    return "/attachments/" in path


def content_is_only_image_urls(content: str) -> bool:
    """Сообщение из одной или нескольких ссылок на картинку — без отдельного текста."""
    stripped = content.strip()
    urls = urls_from_content(stripped)
    if not urls:
        return False
    remainder = stripped
    for url in urls:
        remainder = remainder.replace(url, "").strip()
    return not remainder


def urls_from_content(content: str | None) -> list[str]:
    if not content:
        return []
    seen: set[str] = set()
    urls: list[str] = []
    for pattern in (IMAGE_URL_SUFFIX_RE, DISCORD_CDN_RE):
        for match in pattern.finditer(content):
            url = match.group(0).rstrip(">).]}\"'")
            if url not in seen and _looks_like_image_url(url):
                seen.add(url)
                urls.append(url)
    return urls


def _urls_from_embed(embed: discord.Embed) -> list[str]:
    urls: list[str] = []
    if embed.image and embed.image.url:
        urls.append(embed.image.url)
    if embed.thumbnail and embed.thumbnail.url:
        url = embed.thumbnail.url
        if url not in urls:
            urls.append(url)
    # GIFV: превью часто в thumbnail, сам gif — в video
    if embed.video and embed.video.url and embed.video.url not in urls:
        if _looks_like_image_url(embed.video.url) or embed.type in ("gifv", "image"):
            urls.append(embed.video.url)
    return urls


async def _download_image(
    http: httpx.AsyncClient, url: str, max_bytes: int,
) -> bytes | None:
    try:
        async with http.stream("GET", url, follow_redirects=True) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type and content_type not in IMAGE_CONTENT_TYPES:
                if not content_type.startswith("image/"):
                    logger.debug("skip url wrong content-type | url={u} | ct={ct}", u=url, ct=content_type)
                    return None
            content_length = resp.headers.get("content-length")
            if content_length and int(content_length) > max_bytes:
                logger.info("skip url too large (header) | url={u}", u=url)
                return None
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    logger.info("skip url too large (body) | url={u}", u=url)
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPError as e:
        logger.warning("image download failed | url={u} | err={err}", u=url, err=str(e))
        return None


async def iter_message_images(
    message: discord.Message,
    http: httpx.AsyncClient,
    *,
    max_bytes: int,
    image_content_types: set[str],
) -> AsyncIterator[tuple[bytes, str]]:
    """Собирает байты картинок: вложения, embed-превью, ссылки в тексте."""
    seen_urls: set[str] = set()

    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type not in image_content_types:
            continue
        if attachment.content_type is None:
            name = (attachment.filename or "").lower()
            if not any(name.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")):
                continue
        if attachment.size > max_bytes:
            logger.info(
                "skip attachment too large | size={sz} | name={n}",
                sz=attachment.size,
                n=attachment.filename,
            )
            continue
        try:
            data = await attachment.read()
        except (discord.HTTPException, discord.NotFound) as e:
            logger.warning("attachment read failed: {err}", err=str(e))
            continue
        if len(data) > max_bytes:
            continue
        hint = attachment.filename or attachment.url
        yield data, hint

    url_jobs: list[tuple[str, str]] = []
    for embed in message.embeds:
        for url in _urls_from_embed(embed):
            if url not in seen_urls:
                seen_urls.add(url)
                url_jobs.append((url, f"embed:{embed.type or 'rich'}"))

    for url in urls_from_content(message.content):
        if url not in seen_urls:
            seen_urls.add(url)
            url_jobs.append((url, "content_url"))

    for url, hint in url_jobs:
        logger.info("image source from link/embed | hint={h} | url={u}", h=hint, u=url)
        data = await _download_image(http, url, max_bytes)
        if data:
            yield data, hint
