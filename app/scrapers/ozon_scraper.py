import asyncio
import logging
import random
from typing import List
from urllib.parse import quote_plus

from app.models import ProductOffer
from app.scrapers.base import BaseScraper, BlockedException

logger = logging.getLogger("stealth-price-chat")


class OzonScraper(BaseScraper):
    """Ozon — Camoufox + перехват XHR / парсинг DOM карточек."""

    def __init__(self, browser_pool):
        super().__init__(name="Ozon", domain="ozon.ru")
        self.browser_pool = browser_pool

    async def search(self, query: str) -> List[ProductOffer]:
        url = f"https://www.ozon.ru/search/?text={quote_plus(query)}&sorting=price"
        page, context, semaphore = await self.browser_pool.new_page()
        try:
            offers: List[ProductOffer] = []

            async def handle_response(response):
                try:
                    if "/api/entrypoint-api.bx/page/json/v2" in response.url:
                        body = await response.body()
                        text = body.decode("utf-8", errors="ignore")
                        if "widgetStates" in text:
                            import json
                            data = json.loads(text)
                            widget_states = data.get("widgetStates", {})
                            for key, value in widget_states.items():
                                if "search" in key.lower() or "products" in key.lower():
                                    try:
                                        items = json.loads(value)
                                        for item in items.get("items", []):
                                            if item.get("type") != "product":
                                                continue
                                            title = item.get("title", "")
                                            price = item.get("price", 0)
                                            if isinstance(price, dict):
                                                price = price.get("price", 0)
                                            if price <= 100:
                                                continue
                                            link = item.get("link", "")
                                            offer_url = f"https://www.ozon.ru{link}" if link.startswith("/") else link
                                            offers.append(
                                                ProductOffer(
                                                    title=title,
                                                    price=int(price),
                                                    currency="RUB",
                                                    marketplace="Ozon",
                                                    url=offer_url,
                                                    is_used=False,
                                                    source_adapter="ozon_scraper",
                                                )
                                            )
                                    except Exception:
                                        continue
                except Exception:
                    pass

            page.on("response", handle_response)

            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(1.2, 2.5))
            await page.mouse.wheel(0, random.randint(400, 900))

            try:
                await page.wait_for_selector('a[href*="/product/-"]', timeout=8000)
            except Exception:
                pass

            title = await page.title()
            if "Доступ ограничен" in title or "Antibot" in title or "antibot" in title.lower():
                raise BlockedException("Ozon: доступ ограничен / антибот")

            # Fallback: парсинг DOM карточек
            if not offers:
                product_links = await page.query_selector_all('a[href*="/product/-"]')
                for link_el in product_links[:30]:
                    try:
                        href = await link_el.get_attribute("href")
                        text = (await link_el.inner_text()).strip()
                        if not text or not href:
                            continue
                        offer_url = f"https://www.ozon.ru{href}" if href.startswith("/") else href
                        price_el = await link_el.query_selector("span:has-text('₽'), [class*='price']")
                        price = 0
                        if price_el:
                            price_text = await price_el.inner_text()
                            price = int("".join(c for c in price_text if c.isdigit()))
                        if price <= 100:
                            continue
                        offers.append(
                            ProductOffer(
                                title=text[:200],
                                price=price,
                                currency="RUB",
                                marketplace="Ozon",
                                url=offer_url,
                                is_used=False,
                                source_adapter="ozon_scraper",
                            )
                        )
                    except Exception:
                        continue

            return offers
        except BlockedException:
            raise
        except Exception as exc:
            logger.error("OzonScraper ошибка: %s", exc)
            raise
        finally:
            await self.browser_pool.close_page(page, context, semaphore)