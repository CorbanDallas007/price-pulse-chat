"""CLI-утилита для изолированной проверки каждого парсера.

Запускает каждый скрапер по очереди на тестовом запросе "iPhone 15 128GB"
и выводит количество найденных товаров и время ответа каждого источника.
"""

import asyncio
import time
from curl_cffi.requests import AsyncSession

from app.config import settings
from app.browser_pool import browser_pool
from app.scrapers.wb_scraper import WBScraper
from app.scrapers.aggregator_scraper import AggregatorScraper
from app.scrapers.avito_scraper import AvitoScraper
from app.scrapers.ozon_scraper import OzonScraper

TEST_QUERY = "iPhone 15 128GB"


async def test_scraper(name: str, scraper) -> None:
    """Запускает один скрапер и печатает результат."""
    start = time.monotonic()
    try:
        offers = await scraper.safe_search(TEST_QUERY)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        print(f"[{name}] {len(offers)} товаров за {elapsed_ms} мс")
        for o in offers[:3]:
            print(f"  - {o.title[:60]} | {o.price} \u20bd | {o.marketplace}")
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        print(f"[{name}] ОШИБКА за {elapsed_ms} мс: {exc}")


async def main() -> None:
    print(f"Тестовый запрос: {TEST_QUERY}\n")

    session = AsyncSession(impersonate="chrome124")
    await browser_pool.start()

    try:
        scrapers = [
            ("Wildberries", WBScraper(session)),
            ("Агрегатор (price.ru)", AggregatorScraper(session)),
            ("Авито", AvitoScraper(browser_pool)),
            ("Ozon", OzonScraper(browser_pool)),
        ]

        for name, scraper in scrapers:
            await test_scraper(name, scraper)

        await session.close()
    finally:
        await browser_pool.stop()


if __name__ == "__main__":
    asyncio.run(main())