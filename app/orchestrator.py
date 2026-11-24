import asyncio
import time
from typing import List, Dict, Optional

from cachetools import TTLCache

from app.config import settings
from app.models import ProductOffer, ScraperStatus, SearchRequest, SearchResponse
from app.pipeline import run_pipeline
from app.scrapers.aggregator_scraper import AggregatorScraper
from app.scrapers.avito_scraper import AvitoScraper
from app.scrapers.base import BaseScraper
from app.scrapers.ozon_scraper import OzonScraper
from app.scrapers.wb_scraper import WBScraper
from app.browser_pool import browser_pool
from app.proxy_manager import ProxyManager

import logging
from curl_cffi.requests import AsyncSession

logger = logging.getLogger("stealth-price-chat")


class Orchestrator:
    """Scatter-Gather оркестратор с TTLCache и Singleflight."""

    def __init__(self):
        self._cache: TTLCache[str, tuple[float, SearchResponse]] = TTLCache(
            maxsize=1000, ttl=settings.CACHE_TTL_SECONDS
        )
        self._inflight: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._session: Optional[AsyncSession] = None
        self._proxy_manager: Optional[ProxyManager] = None

    async def _ensure_session(self) -> AsyncSession:
        if self._session is None or self._session.is_closed():
            proxy_list = [p.strip() for p in settings.PROXY_LIST.split(",") if p.strip()]
            self._proxy_manager = ProxyManager(
                proxy_list, ban_minutes=settings.PROXY_BAN_MINUTES
            )
            self._session = AsyncSession(impersonate="chrome124")
        return self._session

    async def _ensure_browser(self) -> None:
        await browser_pool.start()

    async def close(self) -> None:
        if self._session is not None and not self._session.is_closed():
            await self._session.close()
        await browser_pool.stop()

    async def search(self, request: SearchRequest) -> SearchResponse:
        start_time = time.monotonic()

        # Проверка кэша
        cache_key = f"{request.query.strip().lower()}:{request.condition}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            ts, response = cached
            if time.monotonic() - ts < settings.CACHE_TTL_SECONDS:
                response.cached = True
                return response

        # Singleflight: если тот же запрос уже в работе, ждём результат
        async with self._lock:
            if cache_key in self._inflight:
                return await self._inflight[cache_key]
            future = asyncio.get_event_loop().create_future()
            self._inflight[cache_key] = future

        try:
            response = await self._execute_search(request)
            future.set_result(response)
            return response
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            async with self._lock:
                self._inflight.pop(cache_key, None)

    async def _execute_search(self, request: SearchRequest) -> SearchResponse:
        session = await self._ensure_session()
        await self._ensure_browser()

        # Инициализация всех скраперов
        scrapers: List[BaseScraper] = [
            WBScraper(session),
            AggregatorScraper(session),
            AvitoScraper(browser_pool),
            OzonScraper(browser_pool),
        ]

        # Scatter-Gather: параллельный запуск всех скраперов
        async def _run(scraper: BaseScraper):
            offers = await scraper.safe_search(request.query)
            elapsed = 0
            status = "ok"
            return scraper, offers, status, elapsed

        results = await asyncio.gather(
            *(_run(s) for s in scrapers), return_exceptions=True
        )

        all_offers: List[ProductOffer] = []
        sources: List[ScraperStatus] = []

        for result in results:
            if isinstance(result, Exception):
                logger.error("Скрапер упал с исключением: %s", result)
                continue
            scraper, offers, status, elapsed = result
            sources.append(
                scraper.status(
                    items_found=len(offers),
                    status=status,
                    elapsed_ms=elapsed,
                )
            )
            all_offers.extend(offers)

        # Конвейер очистки
        cleaned = run_pipeline(all_offers, request)

        # Берём топ-5
        top5 = cleaned[:5]

        elapsed_ms = int((time.monotonic() - start_time) * 1000)

        response = SearchResponse(
            query=request.query,
            condition=request.condition,
            cached=False,
            elapsed_ms=elapsed_ms,
            sources=sources,
            items=top5,
        )

        # Сохранение в кэш
        self._cache[cache_key] = (time.monotonic(), response)
        return response