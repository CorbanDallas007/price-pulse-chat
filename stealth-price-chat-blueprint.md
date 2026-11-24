# Technical Blueprint & AI Agent Specification: `stealth-price-chat` (v3.0)

> **Назначение документа:** Исчерпывающий технический план (Implementation Blueprint) для ИИ-агента разработки (Cursor, Claude Code, Windsurf, Cline). Документ содержит строгую архитектуру, реальные поисковые эндпоинты, готовые Pydantic-модели, настройки антидетект-движка (`Camoufox` + `curl_cffi`) и пошаговый план генерации кода.

---

## 1. Системные ограничения и цели
* **Без LLM и платных API:** Приложение работает полностью автономно, без OpenAI/Anthropic API и без платных SERP-сервисов.
* **Zero-Config запуск:** После установки зависимостей и скачивания бинарника браузера приложение запускается одной командой `uvicorn app.main:app --reload`.
* **Устойчивость к блокировкам (Anti-Ban):** Используется гибридная схема: быстрые TLS-маскированные HTTP-запросы (`curl_cffi`) для открытых JSON-ручек и постоянный пул антидетект-браузера (`Camoufox`) с перехватом сетевого JSON-трафика (`XHR/Fetch Interception`) и сохранением сессионных кук на диск для площадок со сложным WAF (Авито, Ozon).

---

## 2. Строгая структура проекта (Directory Tree)

ИИ-агент должен создать строго следующую модульную структуру, чтобы избежать переполнения контекста и упростить отладку:

```text
stealth-price-chat/
├── .env.example                 # Шаблон настроек (опциональные прокси, таймауты)
├── requirements.txt             # Зависимости с точными пакетами
├── browser_profile/             # Папка для Persistent Context (куки и localStorage, в .gitignore)
├── app/
│   ├── __init__.py
│   ├── config.py                # Настройки приложения через pydantic-settings
│   ├── models.py                # Строгие Pydantic-схемы данных и API контрактов
│   ├── proxy_manager.py         # Ротатор прокси с карантином (Circuit Breaker)
│   ├── browser_pool.py          # Singleton-менеджер Camoufox (AsyncCamoufox) с блокировкой вкладок
│   ├── pipeline.py              # Детерминированная очистка, дедупликация, стоп-слова и медиана
│   ├── orchestrator.py          # Scatter-Gather оркестратор, TTLCache и Singleflight
│   ├── main.py                  # FastAPI приложение, Lifespan, эндпоинты API и статика
│   └── scrapers/
│       ├── __init__.py
│       ├── base.py              # Абстрактный класс BaseScraper + CircuitBreaker по доменам
│       ├── wb_scraper.py        # Прямой HTTP-адаптер Wildberries (curl_cffi)
│       ├── aggregator_scraper.py# Адаптер товарного агрегатора / поисковой выдачи (curl_cffi)
│       ├── avito_scraper.py     # Браузерный адаптер Авито (Camoufox + XHR Interception)
│       └── ozon_scraper.py      # Браузерный адаптер Ozon (Camoufox + XHR Interception / DOM)
├── static/
│   └── index.html               # Автономный Single-File Frontend (HTML + CSS + Vanilla JS)
└── test_scrapers.py             # CLI-утилита для изолированной проверки каждого парсера
```

---

## 3. Зависимости и команды инициализации

### `requirements.txt`
```text
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
pydantic>=2.9.0
pydantic-settings>=2.5.0
curl_cffi>=0.7.3
camoufox[geoip]>=0.4.11
playwright>=1.48.0
beautifulsoup4>=4.12.3
lxml>=5.3.0
cachetools>=5.5.0
python-dotenv>=1.0.1
```

### Команды разворачивания (выполнить перед запуском)
```bash
python -m venv .venv
source .venv/bin/activate  # Для Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m camoufox fetch
```
*(Критично: без выполнения `python -m camoufox fetch` бинарный файл пропатченного Firefox не будет загружен, и запуск `AsyncCamoufox` завершится ошибкой).*

---

## 4. Строгие контракты данных (`app/models.py`)

ИИ-агент должен использовать эти модели без изменений, чтобы гарантировать совместимость всех слоёв:

```python
from typing import Literal, Optional
from pydantic import BaseModel, Field, HttpUrl


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=120, description="Поисковый запрос")
    condition: Literal["new", "all"] = Field(
        default="new",
        description="new — исключить б/у и Авито-б/у; all — включая б/у предложения"
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
```

---

## 5. Спецификация движков и реальных источников данных

### 5.1. Пул браузеров (`app/browser_pool.py`)
* Использовать `from camoufox.async_api import AsyncCamoufox`.
* Запускать один экземпляр браузера при старте FastAPI (`lifespan`) с параметрами:
  * `headless=True` (или `"virtual"` на Linux при наличии Xvfb)
  * `persistent_context=True` и `user_data_dir="./browser_profile"` — сохраняет cookies, кэш и доверие сайтов между перезапусками.
  * `humanize=True` — встроенная гуманизация движений мыши и таймингов в Camoufox.
  * `os="windows"` — эмуляция стандартного десктопного окружения Windows.
* Использовать `asyncio.Semaphore(2)` для ограничения одновременного открытия вкладок, чтобы не перегружать ОЗУ и CPU. После каждого поиска вкладка (`page`) обязательно закрывается в блоке `finally`, но сам контекст браузера остаётся жить.

### 5.2. Спецификация адаптеров (`app/scrapers/`)

#### 1. `wb_scraper.py` (Wildberries — прямой HTTP через `curl_cffi`)
* **Движок:** `curl_cffi.requests.AsyncSession(impersonate="chrome124")`.
* **Поисковый эндпоинт:**
  `GET https://search.wb.ru/exactmatch/ru/common/v7/search`
* **Параметры запроса:**
  ```python
  params = {
      "ab_testing": "false",
      "appType": "1",
      "curr": "rub",
      "dest": "-1257786",  # Москва (базовый ПВЗ)
      "query": query,
      "resultset": "catalog",
      "sort": "priceup",   # Сразу просим сортировку по возрастанию цены
      "spp": "30",
      "suppressSpellcheck": "false",
  }
  ```
* **Заголовки:** Обязательны `Accept: */*`, `Origin: https://www.wildberries.ru`, `Referer: https://www.wildberries.ru/catalog/0/search.aspx?search=...`.
* **Парсинг ответа:** JSON путь `data["data"]["products"]` (или `data["products"]`).
  * `id` -> артикул товара.
  * `name` -> название; `brand` -> бренд (склеивать `f"{brand} {name}"`).
  * Цена: брать `sizes[0]["price"]["product"] // 100` (цена в WB API отдаётся в копейках!).
  * Ссылка: `f"https://www.wildberries.ru/catalog/{product_id}/detail.aspx"`.

#### 2. `avito_scraper.py` (Авито — `Camoufox` + XHR Interception)
* **Стратегия без капчи:** Не парсить HTML-вёрстку напрямую регулярками, а перехватывать внутренний JSON-ответ поиска или аккуратно читать DOM после рендера.
* **URL поиска:** `https://www.avito.ru/all?q={quote_plus(query)}&s=1` (`s=1` — сортировка «Дешевле»). Если `condition == "new"`, добавлять параметр фильтра или отсеивать б/у на этапе парсинга.
* **Механизм перехвата:**
  * Подписаться на `page.on("response", handle_response)` до вызова `page.goto(...)`.
  * Отлавливать ответы, где `"api/11/items"` или `"api/9/items"` в `response.url` и `response.status == 200`.
  * Параллельно предусмотреть резервный парсинг DOM через локатор `[data-marker="item"]` (заголовок `[itemprop="name"]`, цена `[itemprop="price"]` атрибут `content`, ссылка `a[data-marker="item-title"]`), если JSON-структура API изменилась.
* **Anti-Ban пауза:** После загрузки страницы сделать плавный скролл вниз (`await page.mouse.wheel(0, random.randint(400, 900))`) и случайную паузу `asyncio.sleep(random.uniform(1.2, 2.5))`.
* **Детекция блокировки:** Если заголовок страницы содержит `"Доступ ограничен"` или HTTP статус `429`/`403`, выбросить исключение `BlockedException`, чтобы `CircuitBreaker` поставил адаптер Авито на паузу на 10 минут.

#### 3. `ozon_scraper.py` (Ozon — `Camoufox` + API Entrypoint Interception)
* **URL поиска:** `https://www.ozon.ru/search/?text={quote_plus(query)}&sorting=price` (`sorting=price` — сортировка по возрастанию цены).
* **Механизм сбора:**
  * Перехват XHR-ответов, содержащих `/api/entrypoint-api.bx/page/json/v2` (извлечение карточек из `widgetStates`), **либо** парсинг отрендеренных карточек в DOM (`div[data-widget="searchResultsV2"]` -> поиск ссылок `a[href*="/product/"]` и текстовых блоков с символом `₽`).
  * Ожидание появления контента через `await page.wait_for_selector('a[href*="/product/"]', timeout=8000)`.
  * При обнаружении экрана `"Antibot"` / `"Доступ ограничен"` — выброс `BlockedException`.

#### 4. `aggregator_scraper.py` (Товарный агрегатор — `curl_cffi`)
* **Задача:** Быстрый сбор цен с DNS, Ситилинка, Яндекс Маркета и Мегамаркета через открытую поисковую выдачу товарного агрегатора (например, `https://price.ru/search/?q={quote_plus(query)}` или поиск по товарным сниппетам).
* **Парсинг:** Извлечение названия магазина, заголовка товара, цены и прямой ссылки (с очисткой редиректов при возможности) через `BeautifulSoup4` (`lxml`).

---

## 6. Конвейер очистки данных (`app/pipeline.py`)

Все собранные объекты `ProductOffer` проходят строгую 6-этапную фильтрацию:

1. **Нормализация цены (`parse_price`):**
   * Удалить пробелы, неразрывные пробелы (`\xa0`, `\u2009`), символы `₽`, `руб.`, слово `от`.
   * Извлечь только цифры (`int(re.sub(r"\D", "", raw_price))`). Отбросить товары с ценой `<= 100` руб.
2. **Очистка URL и дедупликация:**
   * Удалить query-параметры трекинга (`utm_source`, `utm_medium`, `utm_campaign`, `yclid`, `erid`, `from`).
   * Исключить полные дубликаты по ключу `(marketplace.lower(), normalized_url)`.
3. **Строгий токен-матчинг модели (Relevance Check):**
   * Разбить исходный запрос `query` на значимые токены (игнорируя предлоги короче 2 символов).
   * Все **числовые и модельные токены** из запроса (например: `5060`, `16gb`, `15`, `pro`, `slim`, `v15`) **обязаны** присутствовать в `offer.title.lower()`. Это исключает подсовывание `RTX 4060` на запрос `RTX 5060 Ti`.
4. **Контекстный фильтр стоп-слов (Accessories & Junk Filter):**
   * Базовый список стоп-слов:
     `{"чехол", "кабель", "переходник", "стекло", "пленка", "плёнка", "наклейка", "коробка", "подставка", "держатель", "геймпад", "джойстик", "ремешок", "амбушюры", "корпус", "аккумулятор", "блок питания", "куллер", "кулер", "вентилятор", "ремонт", "уценка", "витринный", "аренда", "прокат", "инструкция", "на запчасти", "не работает", "разбит", "битый", "обмен"}`.
   * **Важное правило:** Перед проверкой из множества стоп-слов вычитаются слова, которые пользователь сам ввёл в `query` (например, если пользователь ищет `«геймпад DualSense»`, слово `геймпад` не блокируется).
   * Если `condition == "new"`, дополнительно отсеиваются предложения с `offer.is_used == True` или словами `б/у`, `бу`, `подержанный` в заголовке.
5. **Статистический отсев ценовых выбросов (Median Outlier Filter):**
   * Если после шага 4 осталось `>= 4` товаров, вычисляется медианная цена `median_price = statistics.median(p.price for p in offers)`.
   * Отбрасываются все предложения с ценой `< median_price * 0.40` (отсекает фейковые объявления на Авито с ценой «1 000 ₽» за предоплату или пустые коробки) и `> median_price * 3.0`.
6. **Финальная сортировка:**
   * `sorted(valid_offers, key=lambda x: x.price)[:5]`.

---

## 7. Требования к Frontend (`static/index.html`)

* **Единый файл:** Весь CSS и JS встроены в `index.html` без внешних CDN-зависимостей (чтобы интерфейс открывался даже без доступа к зарубежным CDN).
* **Функционал UI:**
  * Тёмная тема (`#0f172a` фон, `#1e293b` карточки, акцент `#3b82f6`).
  * Переключатель режима поиска рядом с полем ввода: `[ Только новые ]` / `[ Включая б/у (Авито) ]`.
  * Быстрые теги-примеры: `PS5 Slim`, `RTX 5060 Ti 16GB`, `iPhone 15 128GB`, `Dyson V15`.
  * Пошаговый статус-индикатор во время запроса (`Опрос агрегаторов и маркетплейсов...` -> `Фильтрация дублей и проверка цен...`).
  * Вывод **5 карточек**, отсортированных по возрастанию цены:
    * Цветной бейдж магазина (Ozon — `#005bff`, Wildberries — `#cb11ab`, Авито — `#00aaff`, Яндекс Маркет — `#ffcc00` с тёмным текстом, DNS — `#fc8507`, Ситилинк — `#ff5200`).
    * Бейдж `Б/У` (если применимо).
    * Название товара (максимум 2 строки, `text-overflow: ellipsis`).
    * Цена крупным шрифтом с пробелами (`52 490 ₽`).
    * Кнопка **«Купить»** (`<a target="_blank" rel="noopener noreferrer">`).
  * Под карточками — компактная строка телеметрии (например: *«Найдено за 2.1 сек • Проверено площадок: 4/4»*).
  * Сохранение истории сообщений в `localStorage` и кнопка «Очистить историю» в шапке.

---

## 8. Инструкция по генерации для ИИ-агента (Execution Rules)

При реализации проекта по данному Blueprint-документу ИИ-агент обязан соблюдать следующие правила:

1. **Пошаговая генерация без сокращений:** Создавать файлы последовательно согласно разделу 2 (`requirements.txt` -> `config.py` -> `models.py` -> `proxy_manager.py` -> `browser_pool.py` -> `scrapers/*` -> `pipeline.py` -> `orchestrator.py` -> `main.py` -> `static/index.html` -> `test_scrapers.py`). Запрещено писать `# TODO: реализовать парсинг` или оставлять пустые функции.
2. **Защита от падений (Resilience):** Любой сбой внутри отдельного скрапера (таймаут, изменение вёрстки, капча) должен перехватываться внутри `BaseScraper.safe_search()`, логироваться и возвращать пустой список `[]` со статусом `"blocked"` или `"error"`, не прерывая работу остальных источников.
3. **Асинхронная безопасность:** Не использовать блокирующие вызовы (`time.sleep`, `requests.get`) внутри `async def` функций — только `asyncio.sleep` и асинхронные сессии `curl_cffi` / `Camoufox`.
4. **Автономный тест:** В файле `test_scrapers.py` реализовать CLI-скрипт, который по очереди запускает каждый скрапер на тестовом запросе `"iPhone 15 128GB"` и выводит в консоль количество найденных товаров и время ответа каждого источника для быстрой диагностики.
