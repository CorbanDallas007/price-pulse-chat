import asyncio
import logging
from typing import Optional

from camoufox.async_api import AsyncCamoufox

from app.config import settings

logger = logging.getLogger("stealth-price-chat")


class BrowserPool:
    """Singleton-менеджер Camoufox с блокировкой вкладок.

    Один экземпляр браузера с persistent_context (сохранение cookies на диск).
    Semaphore ограничивает одновременное открытие вкладок (макс. 2).
    """

    _instance: Optional["BrowserPool"] = None
    _lock = asyncio.Lock()

    def __new__(cls) -> "BrowserPool":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._browser: Optional[AsyncCamoufox] = None
        self._tab_semaphore = asyncio.Semaphore(2)
        self._started = False

    async def start(self) -> None:
        """Запуск браузера при старте FastAPI (lifespan)."""
        if self._started:
            return
        logger.info("Запуск Camoufox браузера (headless=%s)...", settings.HEADLESS_MODE)
        self._browser = await AsyncCamoufox(
            headless=settings.HEADLESS_MODE,
            persistent_context=True,
            user_data_dir="./browser_profile",
            humanize=True,
            os="windows",
        ).start()
        self._started = True
        logger.info("Camoufox браузер запущен.")

    async def stop(self) -> None:
        """Остановка браузера при завершении работы приложения."""
        if self._browser is not None:
            await self._browser.stop()
            self._browser = None
            self._started = False
            logger.info("Camoufox браузер остановлен.")

    async def new_page(self):
        """Создание новой вкладки с ограничением семафором.

        Вызывающий код должен закрыть страницу в блоке finally.
        """
        await self._tab_semaphore.acquire()
        if self._browser is None:
            raise RuntimeError("BrowserPool не запущен. Вызовите start() primero.")
        context = await self._browser.new_context()
        page = await context.new_page()
        return page, context, self._tab_semaphore

    async def close_page(self, page, context, semaphore: asyncio.Semaphore) -> None:
        """Безопасное закрытие вкладки и контекста, освобождение семафора."""
        try:
            await page.close()
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("Ошибка закрытия страницы: %s", exc)
        try:
            await context.close()
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("Ошибка закрытия контекста: %s", exc)
        semaphore.release()


browser_pool = BrowserPool()