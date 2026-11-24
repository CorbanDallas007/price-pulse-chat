import asyncio
import logging
import random
from typing import List
from urllib.parse import quote_plus

from app.models import ProductOffer
from app.scrapers.base import BaseScraper, BlockedException

logger = logging.getLogger("stealth-price-chat")


class AvitoScraper(BaseScraper):
    """Авито — Camoufox + перехват XHR-ответов поиска."""

    def __init__(self, browser_pool):
        super().__init__(name="Авито", domain="avito.ru")
        self.browser_pool = browser_pool

    async def search(self, query: str) -> List[ProductOffer]:
        url = f"https://www.avito.ru/all?q={quote_plus(query)}&s=1"
        page, context, semaphore = await self.browser_pool.new_page()
        try:
            offers: List[ProductOffer] = []
            captured_json: list[str] = []

            def handle_response(response):
                try:
                    resource_type = response.request.resource_type
                    if resource_type in ("xhr", "fetch"):
                        url_str = response.url
                        if "/search/" in url_str or "/catalog/" in url_str:
                            captured_json.append(url_str)
                except Exception:
                    pass

            page.on("response", handle_response)

            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(1.2, 2.5))
            await page.mouse.wheel(0, random.randint(400, 900))
            await asyncio.sleep(1.0)

            title = await page.title()
            if "Доступ ограничен" in title or "Antibot" in title:
                raise BlockedException("Авито: доступ ограничен / антибот")

            # Попытка извлечь объявления из DOM
            cards = await page.query_selector_all("div[data-marker='item']")
            if not cards:
                cards = await page.query_selector_all("div.item")

            for card in cards[:30]:
                try:
                    title_el = await card.query_selector("h3, [data-marker='item-title']")
                    price_el = await card.query_selector("[data-marker='item-price'], .price")
                    link_el = await card.query_selector("a")
                    if not title_el or not price_el or not link_el:
                        continue
                    offer_title = (await title_el.inner_text()).strip()
                    price_text = (await price_el.inner_text()).strip()
                    price = int("".join(c for c in price_text if c.isdigit()))
                    if price <= 100:
                        continue
                    href = await link_el.get_attribute("href")
                    offer_url = f"https://www.avito.ru{href}" if href and href.startswith("/") else (href or "")
                    is_used = False  # Авито по умолчанию с mix, фильтрация б/у в pipeline
                    offers.append(
                        ProductOffer(
                            title=offer_title,
                            price=price,
                            currency="RUB",
                            marketplace="Авито",
                            url=offer_url,
                            is_used=is_used,
                            source_adapter="avito_scraper",
                        )
                    )
                except Exception as exc:
                    logger.debug("Ошибка парсинга карточки Авито: %s", exc)
                    continue

            return offers
        except BlockedException:
            raise
        except Exception as exc:
            logger.error("AvitoScraper ошибка: %s", exc)
            raise
        finally:
            await self.browser_pool.close_page(page, context, semaphore)