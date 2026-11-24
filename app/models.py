from typing import Literal, Optional
from pydantic import BaseModel, Field, HttpUrl


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=120, description="Поисковый запрос")
    condition: Literal["new", "all"] = Field(
        default="new",
        description="new — исключить б/у и Авито-б/у; all — включая б/у предложения",
    )


class ProductOffer(BaseModel):
    title: str
    price: int = Field(..., gt=0, description="Цена в рублях, целое число")
    currency: str = "RUB"
    marketplace: str = Field(..., description="Ozon, Wildberries, Авито, Яндекс Маркет, DNS, Ситилинк и др.")
    url: str
    is_used: bool = False
    source_adapter: str


class ScraperStatus(BaseModel):
    adapter: str
    status: Literal["ok", "timeout", "blocked", "error"]
    items_found: int = 0
    elapsed_ms: int = 0


class SearchResponse(BaseModel):
    query: str
    condition: str
    cached: bool = False
    elapsed_ms: int
    sources: list[ScraperStatus]
    items: list[ProductOffer]