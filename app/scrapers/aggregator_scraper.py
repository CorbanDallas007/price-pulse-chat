import logging
from typing import List
from urllib.parse import quote_plus, urlparse, urlunparse, parse_qsl, urlencode

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

from app.models import ProductOffer
from app.scrapers.base import BaseScraper, BlockedException

logger = logging.getLogger("stealth-price-chat")


TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "yclid", "erid", "from", "ref", "source",
}


class AggregatorScraper(BaseScraper):
    """Товарный агрегатор (price.ru) — curl_cffi + BeautifulSoup4."""

    def __init__(self, session: AsyncSession):
        super().__init__(name="Агрегатор (price.ru)", domain="price.ru")
        self.session = session

    @staticmethod
    def _clean_url(url: str) -> str:
        parsed = urlparse(url)
        query = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() not in TRACKING_PARAMS]
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), parsed.fragment))

    async def search(self, query: str) -> List[ProductOffer]:
        url = f"https://price.ru/search/?q={quote_plus(query)}"
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        response = await self.session.get(url, headers=headers, timeout=15)
        if response.status_code in (403, 429):
            raise BlockedException(f"price.ru вернул {response.status_code}")
        response.raise_for_status()
        html = response.text
        soup = BeautifulSoup(html, "lxml")
        offers: List[ProductOffer] = []

        # Поиск карточек товаров на price.ru
        cards = soup.select("div.product-card, div.item, .product-item, [data-product]")
        for card in cards[:30]:
            try:
                title_el = card.select_one("a.product-title, a.item-title, h3, h2")
                price_el = card.select_one(".price, [class*='price'], span.price")
                link_el = card.select_one("a[href]")
                if not title_el or not price_el or not link_el:
                    continue
                title = title_el.get_text(strip=True)
                price_text = price_el.get_text(strip=True)
                price = int("".join(c for c in price_text if c.isdigit()))
                if price <= 100:
                    continue
                href = link_el.get("href", "")
                offer_url = self._clean_url(href)
                if offer_url.startswith("/"):
                    offer_url = f"https://price.ru{offer_url}"
                # Определяем маркетплейс по домену ссылки или по тексту
                marketplace = "Агрегатор"
                offers.append(
                    ProductOffer(
                        title=title,
                        price=price,
                        currency="RUB",
                        marketplace=marketplace,
                        url=offer_url,
                        is_used=False,
                        source_adapter="aggregator_scraper",
                    )
                )
            except Exception as exc:
                logger.debug("Ошибка парсинга карточки агрегатора: %s", exc)
                continue

        return offers