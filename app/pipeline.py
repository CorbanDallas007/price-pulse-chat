import re
import statistics
import time
from typing import List, Set

from app.models import ProductOffer, SearchRequest


# Базовый список стоп-слов из спецификации
STOP_WORDS: Set[str] = {
    "чехол", "кабель", "переходник", "стекло", "пленка", "плёнка",
    "наклейка", "коробка", "подставка", "держатель", "геймпад",
    "джойстик", "ремешок", "амбушюры", "корпус", "аккумулятор",
    "блок питания", "куллер", "кулер", "вентилятор", "ремонт",
    "уценка", "витринный", "аренда", "прокат", "инструкция",
    "на запчасти", "не работает", "разбит", "битый", "обмен",
}

# Слова, указывающие на б/у состояние
USED_WORDS: Set[str] = {"б/у", "бу", "подержанный", "б у"}


def parse_price(raw_price: str) -> int:
    """Этап 1: нормализация цены. Убирает всё кроме цифр, отбрасывает <= 100."""
    if not raw_price:
        return 0
    cleaned = re.sub(r"[\s\xa0\u2009₽руб.от]", "", raw_price)
    digits = re.sub(r"\D", "", cleaned)
    if not digits:
        return 0
    price = int(digits)
    return price if price > 100 else 0


def clean_url(url: str) -> str:
    """Этап 2: удаляет query-параметры трекинга из URL."""
    tracking = {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "yclid", "erid", "from", "ref", "source",
    }
    try:
        from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
        parsed = urlparse(url)
        query = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() not in tracking]
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), parsed.fragment))
    except Exception:
        return url


def extract_query_tokens(query: str) -> Set[str]:
    """Извлекает значимые токены из запроса (игнорируя предлоги короче 2 символов)."""
    tokens = set()
    for word in query.lower().split():
        word = re.sub(r"[^\w\s]", "", word)
        if len(word) >= 2:
            tokens.add(word)
    return tokens


def has_model_tokens(query: str, title: str) -> bool:
    """Этап 3: строгий токен-матчинг. Числовые и модельные токены из запроса должны быть в заголовке."""
    query_tokens = extract_query_tokens(query)
    if not query_tokens:
        return True
    title_lower = title.lower()
    for token in query_tokens:
        if token not in title_lower:
            return False
    return True


def filter_stop_words(
    offers: List[ProductOffer],
    query: str,
    condition: str,
) -> List[ProductOffer]:
    """Этап 4: контекстный фильтр стоп-слов + фильтр б/у."""
    query_tokens = extract_query_tokens(query)
    active_stop_words = STOP_WORDS - query_tokens  # слова из запроса не блокируются
    result: List[ProductOffer] = []
    for offer in offers:
        title_lower = offer.title.lower()
        # Стоп-слова
        blocked = any(sw in title_lower for sw in active_stop_words)
        if blocked:
            continue
        # Если condition == "new", отсеиваем б/у
        if condition == "new":
            if offer.is_used:
                continue
            if any(uw in title_lower for uw in USED_WORDS):
                continue
        result.append(offer)
    return result


def filter_price_outliers(offers: List[ProductOffer]) -> List[ProductOffer]:
    """Этап 5: статистический отсев ценовых выбросов по медиане."""
    if len(offers) < 4:
        return offers
    prices = sorted(o.price for o in offers)
    median_price = statistics.median(prices)
    floor = median_price * 0.40
    ceiling = median_price * 3.0
    return [o for o in offers if floor <= o.price <= ceiling]


def deduplicate(offers: List[ProductOffer]) -> List[ProductOffer]:
    """Этап 2 (продолжение): удаление полных дублей по (marketplace, normalized_url)."""
    seen: set = set()
    unique: List[ProductOffer] = []
    for offer in offers:
        key = (offer.marketplace.lower(), clean_url(offer.url))
        if key in seen:
            continue
        seen.add(key)
        unique.append(offer)
    return unique


def run_pipeline(
    offers: List[ProductOffer],
    request: SearchRequest,
) -> List[ProductOffer]:
    """Запуск полного 6-этапного конвейера очистки."""
    # Этап 1: нормализация цены (отсекаем <= 100)
    normalized: List[ProductOffer] = []
    for o in offers:
        price = parse_price(str(o.price))
        if price > 0:
            o.price = price
            normalized.append(o)

    # Этап 2: очистка URL + дедупликация
    deduped = deduplicate(normalized)

    # Этап 3: строгий токен-матчинг
    relevant = [o for o in deduped if has_model_tokens(request.query, o.title)]

    # Этап 4: контекстный фильтр стоп-слов + б/у
    filtered = filter_stop_words(relevant, request.query, request.condition)

    # Этап 5: статистический отсев выбросов по медиане
    outlier_filtered = filter_price_outliers(filtered)

    # Этап 6: финальная сортировка по возрастанию цены
    outlier_filtered.sort(key=lambda x: x.price)
    return outlier_filtered