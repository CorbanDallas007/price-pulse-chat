import logging
from typing import List

from curl_cffi.requests import AsyncSession

from app.models import ProductOffer
from app.scrapers.base import BaseScraper, BlockedException

logger = logging.getLogger("stealth-price-chat")


class WBScraper(BaseScraper):
    """Wildberries — прямой HTTP через curl_cffi с impersonate=chrome124."""

    def __init__(self, session: AsyncSession):
        super().__init__(name="Wildberries", domain="wildberries.ru")
        self.session = session

    async def search(self, query: str) -> List[ProductOffer]:
        url = "https://search.wb.ru/exactmatch/ru/common/v7/search"
        params = {
            "ab_testing": "false",
            "appType": "1",
            "curr": "rub",
            "dest": "-1257786",
            "query": query,
            "resultset": "catalog",
            "sort": "priceup",
            "spp": "30",
            "suppressSpellcheck": "false",
        }
        headers = {
            "Accept": "*/*",
            "Origin": "https://www.wildberries.ru",
            "Referer": f"https://www.wildberries.ru/catalog/0/search.aspx?search={query}",
        }
        response = await self.session.get(url, params=params, headers=headers, timeout=15)
        if response.status_code in (403, 429):
            raise BlockedException(f"WB вернул {response.status_code}")
        response.raise_for_status()
        data = response.json()
        products = data.get("data", {}).get("products", [])
        if not products:
            products = data.get("products", [])
        offers: List[ProductOffer] = []
        for p in products:
            product_id = p.get("id")
            name = p.get("name", "")
            brand = p.get("brand", "")
            title = f"{brand} {name}".strip() if brand else name
            sizes = p.get("sizes", [])
            price = 0
            if sizes:
                price_info = sizes[0].get("price", {})
                price = price_info.get("product", 0) // 100
            if price <= 0:
                continue
            offers.append(
                ProductOffer(
                    title=title,
                    price=price,
                    currency="RUB",
                    marketplace="Wildberries",
                    url=f"https://www.wildberries.ru/catalog/{product_id}/detail.aspx",
                    is_used=False,
                    source_adapter="wb_scraper",
                )
            )
        return offers