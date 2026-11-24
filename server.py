"""stealth-price-chat backend — FastAPI + curl_cffi.

Multi-source price search engine with anti-ban protection.
No LLM / AI is used for data extraction.
"""

import asyncio
import logging
import os
import random
import re
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, urljoin, urlparse

from curl_cffi.requests import AsyncSession, RequestsError
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

PROXY_LIST: List[str] = [p.strip() for p in os.getenv("PROXY_LIST", "").split(",") if p.strip()]
SERP_API_KEY: str = os.getenv("SERP_API_KEY", "")

CACHE_TTL_SECONDS = 15 * 60
COOKIE_WARM_INTERVAL = 30 * 60
REQUEST_DELAY_MIN = 0.7
REQUEST_DELAY_MAX = 1.8
SEMAPHORE_PER_DOMAIN = 2
RETRY_MAX = 3
PROXY_BAN_MINUTES = 10
OUTLIER_THRESHOLD = 0.30
MAX_RESULTS = 5

STOP_WORDS = [
    "чехол", "кабель", "стекло", "наклейка", "коробка",
    "подставка", "геймпад", "джойстик", "ремонт", "уценка",
]

MARKETPLACE_COLORS: Dict[str, str] = {
    "ozon": "#f82762",
    "wildberries": "#ff3f3f",
    "yandex market": "#ffa000",
    "dns": "#005daa",
    "citilink": "#008000",
    "price.ru": "#6c5ce7",
    "aggregator": "#8e44ad",
    "serp": "#2ecc71",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("stealth-price-chat")


class SearchRequest(BaseModel):
    query: str


class Offer(BaseModel):
    title: str
    price: float
    url: str
    marketplace: str
    source: str


class SearchResponse(BaseModel):
    query: str
    results: List[Offer]
    total_found: int
    sources: List[str]

class TtlCache:
    def __init__(self, ttl_seconds: int, max_size: int = 1000):
        self.ttl = ttl_seconds
        self.max_size = max_size
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = asyncio.Lock()

    @staticmethod
    def _normalize(query: str) -> str:
        return re.sub(r"\s+", " ", query.strip().lower())

    async def get(self, query: str) -> Optional[Any]:
        key = self._normalize(query)
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, value = entry
            if time.monotonic() - ts > self.ttl:
                del self._store[key]
                return None
            return value

    async def set(self, query: str, value: Any) -> None:
        key = self._normalize(query)
        async with self._lock:
            self._store[key] = (time.monotonic(), value)
            if len(self._store) > self.max_size:
                self._store.popitem(last=False)


class ProxyRotator:
    def __init__(self, proxies: List[str]):
        self.proxies = proxies
        self._index = 0
        self._banned_until: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def get_proxy(self) -> Optional[str]:
        if not self.proxies:
            return None
        async with self._lock:
            now = time.monotonic()
            n = len(self.proxies)
            for _ in range(n):
                proxy = self.proxies[self._index % n]
                self._index += 1
                if now >= self._banned_until.get(proxy, 0):
                    return proxy
            return None

    def mark_banned(self, proxy: Optional[str], minutes: int = PROXY_BAN_MINUTES) -> None:
        if not proxy:
            return
        self._banned_until[proxy] = time.monotonic() + minutes * 60
        logger.warning("[proxy] %s banned for %d minutes", proxy, minutes)

class BaseScraper(ABC):
    def __init__(self, name: str, base_url: str, proxy_rotator: Optional[ProxyRotator] = None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.domain = urlparse(base_url).netloc
        self.proxy_rotator = proxy_rotator
        self.session: Optional[AsyncSession] = None
        self.semaphore = asyncio.Semaphore(SEMAPHORE_PER_DOMAIN)
        self.last_warm: float = 0.0
        self._session_lock = asyncio.Lock()

    async def _get_session(self) -> AsyncSession:
        async with self._session_lock:
            if self.session is None or self.session.is_closed:
                proxy = await self.proxy_rotator.get_proxy() if self.proxy_rotator else None
                self.session = AsyncSession(impersonate="chrome124", proxy=proxy)
                logger.debug("[%s] new curl_cffi session (proxy=%s)", self.name, proxy)
            return self.session

    async def close(self) -> None:
        async with self._session_lock:
            if self.session is not None and not self.session.is_closed:
                await self.session.close()
                self.session = None

    async def _warm_cookies(self) -> None:
        now = time.monotonic()
        if now - self.last_warm < COOKIE_WARM_INTERVAL:
            return
        try:
            session = await self._get_session()
            await session.get(self.base_url, timeout=15)
            self.last_warm = now
            logger.info("[%s] cookies warmed", self.name)
        except Exception as exc:
            logger.warning("[%s] cookie warm-up failed: %s", self.name, exc)

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        await self._warm_cookies()
        await asyncio.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
        proxy = await self.proxy_rotator.get_proxy() if self.proxy_rotator else None

        async with self.semaphore:
            last_exc: Optional[Exception] = None
            for attempt in range(1, RETRY_MAX + 1):
                try:
                    session = await self._get_session()
                    response = await session.request(method, url, timeout=20, **kwargs)
                    if response.status_code in (403, 429):
                        if self.proxy_rotator:
                            self.proxy_rotator.mark_banned(proxy)
                        raise RequestsError(f"HTTP {response.status_code}")
                    response.raise_for_status()
                    return response
                except Exception as exc:
                    last_exc = exc
                    if attempt < RETRY_MAX:
                        backoff = (2 ** attempt) + random.uniform(0.5, 1.5)
                        logger.warning(
                            "[%s] attempt %d/%d failed for %s: %s — retry in %.1fs",
                            self.name, attempt, RETRY_MAX, url, exc, backoff,
                        )
                        await asyncio.sleep(backoff)
            logger.error("[%s] request failed after %d attempts: %s", self.name, RETRY_MAX, url)
            raise last_exc

    @abstractmethod
    async def search(self, query: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @staticmethod
    def _clean_price(text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        digits = re.sub(r"[^\d.,]", "", text)
        if not digits:
            return None
        digits = digits.replace(" ", "").replace("\u00a0", "")
        if "," in digits and "." in digits:
            digits = digits.replace(".", "").replace(",", ".")
        elif "," in digits:
            digits = digits.replace(",", ".")
        try:
            return float(digits)
        except ValueError:
            return None

    @staticmethod
    def _has_stop_word(title: str) -> bool:
        title_lower = title.lower()
        return any(word in title_lower for word in STOP_WORDS)

    @staticmethod
    def _extract_marketplace(url: str, fallback: str) -> str:
        try:
            host = urlparse(url).netloc.lower()
        except Exception:
            return fallback
        host = host.replace("www.", "")
        for key in ("ozon", "wildberries", "yandex", "dns", "citilink", "price"):
            if key in host:
                return key
        return fallback


class AggregatorScraper(BaseScraper):
    def __init__(self, proxy_rotator: Optional[ProxyRotator] = None):
        super().__init__("Price.ru Aggregator", "https://price.ru", proxy_rotator)

    async def search(self, query: str) -> List[Dict[str, Any]]:
        offers: List[Dict[str, Any]] = []
        try:
            search_url = f"{self.base_url}/catalog/search?search={quote_plus(query)}"
            response = await self._request("GET", search_url)
            html = response.text

            card_pattern = re.compile(
                r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                re.DOTALL | re.IGNORECASE,
            )
            for m in card_pattern.finditer(html):
                href, inner = m.group(1), m.group(2)
                title = re.sub(r"<[^>]+>", " ", inner)
                title = re.sub(r"\s+", " ", title).strip()
                if not title or len(title) < 3:
                    continue

                price_match = re.search(
                    r"(\d[\d\s\u00a0]*[.,]?\d{0,2})\s*(?:₽|rub|руб\.)",
                    inner,
                    re.IGNORECASE,
                )
                price = self._clean_price(price_match.group(1)) if price_match else None
                if price is None or price <= 0:
                    continue

                full_url = urljoin(search_url, href)
                offers.append({
                    "title": title[:200],
                    "price": price,
                    "url": full_url,
                    "marketplace": self._extract_marketplace(full_url, "aggregator"),
                    "source": self.name,
                })
        except Exception as exc:
            logger.warning("[%s] search failed: %s", self.name, exc)
        return offers


class DirectJsonScraper(BaseScraper):
    def __init__(self, proxy_rotator: Optional[ProxyRotator] = None):
        super().__init__("Wildberries JSON API", "https://www.wildberries.ru", proxy_rotator)

    async def search(self, query: str) -> List[Dict[str, Any]]:
        offers: List[Dict[str, Any]] = []
        try:
            search_url = (
                f"https://search.wb.ru/exactmatch/ru/common/v4/search?"
                f"query={quote_plus(query)}&resulttype=catalog&"
                f"sort=popular&spp=0&suppressSpellcheck=false"
            )
            headers = {
                "Accept": "application/json",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
                "Origin": "https://www.wildberries.ru",
                "Referer": "https://www.wildberries.ru/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            }
            response = await self._request("GET", search_url, headers=headers)
            data = response.json()

            products = (
                data.get("data", {}).get("products", [])
                if isinstance(data, dict)
                else []
            )
            for item in products:
                title = item.get("name") or item.get("title") or ""
                if not title:
                    continue

                price = None
                sizes = item.get("sizes") or []
                for size in sizes:
                    price_obj = size.get("price") or {}
                    basic = price_obj.get("basic")
                    if basic:
                        price = basic / 100.0
                        break
                if price is None:
                    price_obj = item.get("price") or {}
                    basic = price_obj.get("basic")
                    if basic:
                        price = basic / 100.0
                if price is None or price <= 0:
                    continue

                product_id = item.get("id") or item.get("root") or ""
                full_url = (
                    f"https://www.wildberries.ru/catalog/{product_id}/detail.aspx"
                    if product_id
                    else self.base_url
                )

                offers.append({
                    "title": title[:200],
                    "price": price,
                    "url": full_url,
                    "marketplace": "wildberries",
                    "source": self.name,
                })
        except Exception as exc:
            logger.warning("[%s] search failed: %s", self.name, exc)
        return offers


class SerpScraper(BaseScraper):
    def __init__(self, proxy_rotator: Optional[ProxyRotator] = None):
        super().__init__("SERP API", "https://serpapi.com", proxy_rotator)

    async def search(self, query: str) -> List[Dict[str, Any]]:
        if not SERP_API_KEY:
            return []
        offers: List[Dict[str, Any]] = []
        try:
            params = {
                "api_key": SERP_API_KEY,
                "query": query,
                "engine": "google_shopping",
                "gl": "ru",
                "hl": "ru",
            }
            response = await self._request(
                "GET",
                "https://serpapi.com/search",
                params=params,
            )
            data = response.json()
            shopping_results = data.get("shopping_results") or []
            for item in shopping_results:
                title = item.get("title") or ""
                price_raw = item.get("price") or "0"
                price = self._clean_price(price_raw)
                link = item.get("link") or item.get("product_link") or ""
                if not title or price is None or price <= 0 or not link:
                    continue
                offers.append({
                    "title": title[:200],
                    "price": price,
                    "url": link,
                    "marketplace": self._extract_marketplace(link, "serp"),
                    "source": self.name,
                })
        except Exception as exc:
            logger.warning("[%s] search failed: %s", self.name, exc)
        return offers


class PriceSearchEngine:
    def __init__(self):
        self.proxy_rotator = ProxyRotator(PROXY_LIST) if PROXY_LIST else None
        self.cache = TtlCache(CACHE_TTL_SECONDS)
        self.scrapers: List[BaseScraper] = [
            AggregatorScraper(self.proxy_rotator),
            DirectJsonScraper(self.proxy_rotator),
            SerpScraper(self.proxy_rotator),
        ]

    async def close(self) -> None:
        await asyncio.gather(*(s.close() for s in self.scrapers), return_exceptions=True)

    @staticmethod
    def _deduplicate(offers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set()
        unique: List[Dict[str, Any]] = []
        for offer in offers:
            key = offer["url"]
            if key in seen:
                continue
            seen.add(key)
            unique.append(offer)
        return unique

    @staticmethod
    def _filter_stop_words(offers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [o for o in offers if not BaseScraper._has_stop_word(o["title"])]

    @staticmethod
    def _remove_price_outliers(offers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not offers:
            return []
        prices = sorted(o["price"] for o in offers)
        median = prices[len(prices) // 2]
        floor = median * OUTLIER_THRESHOLD
        return [o for o in offers if o["price"] >= floor]

    async def search(self, query: str) -> SearchResponse:
        cached = await self.cache.get(query)
        if cached is not None:
            return cached

        logger.info("Search started: %r", query)

        results: List[Dict[str, Any]] = []
        sources_used: List[str] = []

        async def _run(scraper: BaseScraper) -> None:
            nonlocal results
            try:
                found = await scraper.search(query)
                results.extend(found)
                if found:
                    sources_used.append(scraper.name)
            except Exception as exc:
                logger.error("[%s] unexpected error: %s", scraper.name, exc)

        await asyncio.gather(*(_run(s) for s in self.scrapers), return_exceptions=True)

        results = self._deduplicate(results)
        results = self._filter_stop_words(results)
        results = self._remove_price_outliers(results)
        results.sort(key=lambda o: o["price"])
        top5 = results[:MAX_RESULTS]

        offers = [Offer(**o) for o in top5]
        response = SearchResponse(
            query=query,
            results=offers,
            total_found=len(results),
            sources=sources_used,
        )

        await self.cache.set(query, response)
        logger.info("Search finished: %r -> %d offers (top %d)", query, len(results), len(offers))
        return response


app = FastAPI(title="stealth-price-chat", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

engine = PriceSearchEngine()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await engine.close()


@app.post("/api/search", response_model=SearchResponse)
async def search(request: SearchRequest) -> SearchResponse:
    if not request.query or not request.query.strip():
        raise HTTPException(status_code=400, detail="Query must not be empty")
    return await engine.search(request.query.strip())


@app.get("/api/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

