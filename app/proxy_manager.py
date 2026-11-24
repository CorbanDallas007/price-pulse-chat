import asyncio
import time
from typing import List, Optional


class ProxyManager:
    """Ротатор прокси с карантином (Circuit Breaker).

    Поддерживает HTTP и SOCKS5 прокси. Забаненные прокси отправляются
    в карантин на настроенное время и автоматически возвращаются в ротацию.
    """

    def __init__(self, proxy_list: List[str], ban_minutes: int = 10):
        self.all_proxies: List[str] = [p.strip() for p in proxy_list if p.strip()]
        self._index = 0
        self._banned_until: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.ban_seconds = ban_minutes * 60

    async def get_proxy(self) -> Optional[str]:
        """Возвращает следующий доступный прокси или None, если список пуст."""
        if not self.all_proxies:
            return None
        async with self._lock:
            now = time.monotonic()
            # Освобождаем прокси, чей карантин истёк
            self._banned_until = {
                p: t for p, t in self._banned_until.items() if t > now
            }
            available = [p for p in self.all_proxies if p not in self._banned_until]
            if not available:
                return None
            proxy = available[self._index % len(available)]
            self._index += 1
            return proxy

    async def ban_proxy(self, proxy: str) -> None:
        """Поставить прокси на карантин на ban_seconds."""
        async with self._lock:
            self._banned_until[proxy] = time.monotonic() + self.ban_seconds

    def is_banned(self, proxy: str) -> bool:
        """Проверяет, находится ли прокси в карантине прямо сейчас."""
        return proxy in self._banned_until and self._banned_until[proxy] > time.monotonic()