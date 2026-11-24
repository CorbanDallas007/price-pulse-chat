import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import List, Literal, Optional

from curl_cffi.requests import AsyncSession

from app.models import ProductOffer, ScraperStatus
from app.proxy_manager import ProxyManager

logger = logging.getLogger("stealth-price-chat")


class BlockedException(Exception):
    """Выбрасывается при обнаружении блокировки (403/429/капча/antibot)."""


class CircuitBreaker:
    """Circuit Breaker по доменам: при repeated blocked/status домен отключается на N минут."""

    def __init__(self, ban_minutes: int = 10, max_failures: int = 3):
        self.ban_seconds = ban_minutes * 60
        self.max_failures = max_failures
        self._failures: dict[str, int] = {}
        self._banned_until: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def is_blocked(self, domain: str) -> bool:
        async with self._lock:
            until = self._banned_until.get(domain, 0.0)
            if until > time.monotonic():
                return True
            return False

    async def record_failure(self, domain: str) -> None:
        async with self._lock:
            self._failures[domain] = self._failures.get(domain, 0) + 1
            if self._failures[domain] >= self.max_failures:
                self._banned_until[domain] = time.monotonic() + self.ban_seconds
                logger.warning("CircuitBreaker: домен %s заблокирован на %d мин", domain, self.ban_seconds // 60)

    async def record_success(self, domain: str) -> None:
        async with self._lock:
            self._failures.pop(domain, None)
            self._banned_until.pop(domain, None)


class BaseScraper(ABC):
    """Абстрактный базовый класс для всех адаптеров источников."""

    def __init__(self, name: str, domain: str, proxy_manager: Optional[ProxyManager] = None):
        self.name = name
        self.domain = domain
        self.proxy_manager = proxy_manager
        self.session: Optional[AsyncSession] = None
        self._circuit = CircuitBreaker()

    async def _ensure_session(self) -> AsyncSession:
        if self.session is None or self.session.is_closed():
            proxy = await self.proxy_manager.get_proxy() if self.proxy_manager else None
            self.session = AsyncSession(impersonate="chrome124", proxy=proxy)
        return self.session

    async def close(self) -> None:
        if self.session is not None and not self.session.is_closed():
            await self.session.close()

    async def safe_search(self, query: str) -> List[ProductOffer]:
        """Безопасный поиск с перехватом ошибок и CircuitBreaker."""
        if await self._circuit.is_blocked(self.domain):
            logger.warning("[%s] домен в карантине, пропуск", self.name)
            return []
        start = time.monotonic()
        try:
            offers = await self.search(query)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            await self._circuit.record_success(self.domain)
            logger.info("[%s] найдено %d предложений за %d мс", self.name, len(offers), elapsed_ms)
            return offers
        except BlockedException:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            await self._circuit.record_failure(self.domain)
            logger.warning("[%s] заблокирован за %d мс", self.name, elapsed_ms)
            return []
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.error("[%s] неожиданная ошибка за %d мс: %s", self.name, elapsed_ms, exc)
            return []

    @abstractmethod
    async def search(self, query: str) -> List[ProductOffer]:
        """Реализует конкретный поиск у источника."""
        ...

    def status(self, items_found: int, status: Literal["ok", "timeout", "blocked", "error"], elapsed_ms: int) -> ScraperStatus:
        return ScraperStatus(
            adapter=self.name,
            status=status,
            items_found=items_found,
            elapsed_ms=elapsed_ms,
        )