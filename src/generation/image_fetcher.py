"""
Image fetcher: Unsplash API → URL изображения.

Архитектура:
- Unsplash первый приоритет (лучшее качество)
- Pexels fallback если Unsplash не нашёл или лимит исчерпан
- Redis кеш: один и тот же запрос не идёт в API повторно
- Кеш TTL: 24 часа — фото не меняются часто
- При отсутствии Redis — работает без кеша (для MVP без Redis)
"""

import hashlib
import json
import logging
import os
import asyncio
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
REDIS_URL = os.getenv("REDIS_URL", "")

CACHE_TTL = 60 * 60 * 24  # 24 часа


# ── Redis клиент (опциональный) ────────────────────────────────────────────────

_redis = None

async def _get_redis():
    """Возвращает Redis клиент или None если Redis не настроен."""
    global _redis
    if _redis is not None:
        return _redis
    if not REDIS_URL:
        return None
    try:
        import redis.asyncio as aioredis
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        await _redis.ping()
        return _redis
    except Exception as e:
        logger.warning(f"Redis недоступен, работаем без кеша: {e}")
        return None


def _cache_key(query: str) -> str:
    h = hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
    return f"img:{h}"


async def _cache_get(query: str) -> Optional[str]:
    r = await _get_redis()
    if not r:
        return None
    try:
        return await r.get(_cache_key(query))
    except Exception:
        return None


async def _cache_set(query: str, url: str) -> None:
    r = await _get_redis()
    if not r:
        return
    try:
        await r.setex(_cache_key(query), CACHE_TTL, url)
    except Exception:
        pass


# ── Unsplash ───────────────────────────────────────────────────────────────────

async def _fetch_unsplash(session: aiohttp.ClientSession, query: str) -> Optional[str]:
    if not UNSPLASH_ACCESS_KEY:
        return None
    try:
        async with session.get(
            "https://api.unsplash.com/search/photos",
            params={
                "query": query,
                "per_page": 5,
                "orientation": "landscape",
                "content_filter": "high",
            },
            headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                logger.warning(f"Unsplash {resp.status} для '{query}'")
                return None
            data = await resp.json()
            results = data.get("results", [])
            if not results:
                return None
            # Берём второе фото если есть — первое часто слишком очевидное
            photo = results[1] if len(results) > 1 else results[0]
            # regular: ~1080px — оптимально для PDF
            return photo["urls"]["regular"]
    except Exception as e:
        logger.warning(f"Unsplash ошибка для '{query}': {e}")
        return None


# ── Pexels (fallback) ──────────────────────────────────────────────────────────

async def _fetch_pexels(session: aiohttp.ClientSession, query: str) -> Optional[str]:
    if not PEXELS_API_KEY:
        return None
    try:
        async with session.get(
            "https://api.pexels.com/v1/search",
            params={"query": query, "per_page": 5, "orientation": "landscape"},
            headers={"Authorization": PEXELS_API_KEY},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            photos = data.get("photos", [])
            if not photos:
                return None
            photo = photos[1] if len(photos) > 1 else photos[0]
            return photo["src"]["large"]
    except Exception as e:
        logger.warning(f"Pexels ошибка для '{query}': {e}")
        return None


# ── Основная функция ───────────────────────────────────────────────────────────

async def fetch_image_url(query: str) -> Optional[str]:
    """
    Получает URL изображения по поисковому запросу.
    Unsplash → Pexels → None.
    Результат кешируется на 24 часа.
    """
    if not query or not query.strip():
        return None

    # Проверяем кеш
    cached = await _cache_get(query)
    if cached:
        logger.debug(f"Image cache hit: '{query}'")
        return cached

    async with aiohttp.ClientSession() as session:
        url = await _fetch_unsplash(session, query)
        if not url:
            url = await _fetch_pexels(session, query)

    if url:
        await _cache_set(query, url)
        logger.info(f"Image fetched: '{query}' → {url[:60]}...")

    return url


async def fetch_images_for_slides(
    slides: list,
    max_concurrent: int = 3,
) -> dict[int, str]:
    """
    Параллельно получает изображения для всех слайдов у которых есть image_query.

    Args:
        slides: список Slide объектов
        max_concurrent: максимум параллельных запросов (Unsplash rate limit: 50/час)

    Returns:
        {slide.index: image_url}
    """
    # Собираем слайды которым нужны картинки
    slides_with_queries = [
        s for s in slides
        if s.image_query and s.image_query.strip()
    ]

    if not slides_with_queries:
        return {}

    # Семафор чтобы не превысить rate limit
    semaphore = asyncio.Semaphore(max_concurrent)

    async def fetch_one(slide) -> tuple[int, Optional[str]]:
        async with semaphore:
            url = await fetch_image_url(slide.image_query)
            return slide.index, url

    tasks = [fetch_one(s) for s in slides_with_queries]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    image_urls = {}
    for result in results:
        if isinstance(result, Exception):
            logger.warning(f"Image fetch failed: {result}")
            continue
        idx, url = result
        if url:
            image_urls[idx] = url

    logger.info(f"Images fetched: {len(image_urls)}/{len(slides_with_queries)}")
    return image_urls
