# stealth-price-chat — Техническая документация (v3.0)

## 1. Обзор архитектуры

`stealth-price-chat` — автономный мульти-источниковый поисковик цен без использования LLM и платных API. Приложение собирает предложения с товарных маркетплейсов и агрегаторов, пропускает их через детерминированный конвейер очистки и возвращает топ-5 самых дешёвых валидных вариантов в формате веб-чата.

### Принципы проектирования

| Принцип | Описание |
|---------|----------|
| **Без LLM / без платных API** | Весь поиск и очистка реализованы на règles и детерминированных алгоритмах. |
| **Антибан-защита** | Гибридная схема: `curl_cffi` (TLS-маскирование под Chrome) + `Camoufox` (антидетект-браузер с постоянным профилем). |
| **Отказоустойчивость** | Падение любого источника не ломает общий поиск (`return_exceptions=True`, Circuit Breaker). |
| **Модульность** | Каждый источник — отдельный класс-адаптер, легко добавлять новые магазины. |
| **Кэширование** | In-Memory TTL-кэш (15 мин) + Singleflight для дублирующихся запросов. |

## 2. Дерево файлов проекта

```
stealth-price-chat/
├── .env                    # Переменные окружения (создано из .env.example)
├── .env.example            # Шаблон настроек
├── ARCHITECTURE.md         # Данный документ
├── requirements.txt        # Python-зависимости
├── test_scrapers.py        # CLI-тест скраперов
├── stealth-price-chat-blueprint.md  # Техническая спецификация (Blueprint v3.0)
├── app/
│   ├── __init__.py
│   ├── config.py           # Pydantic-settings конфигурация
│   ├── models.py           # Pydantic контракты данных (API схемы)
│   ├── proxy_manager.py    # Ротатор прокси с карантином (Circuit Breaker)
│   ├── browser_pool.py     # Singleton-менеджер Camoufox (persistent контекст)
│   ├── pipeline.py         # 6-этапный конвейер очистки данных
│   ├── orchestrator.py     # Scatter-Gather оркестратор, TTLCache, Singleflight
│   ├── main.py             # FastAPI приложение, lifespan, эндпоинты
│   └── scrapers/
│       ├── __init__.py
│       ├── base.py         # BaseScraper (абстракция) + CircuitBreaker + BlockedException
│       ├── wb_scraper.py   # Wildberries (curl_cffi, JSON API)
│       ├── avito_scraper.py# Авито (Camoufox + XHR перехват / DOM)
│       ├── ozon_scraper.py # Ozon (Camoufox + entrypoint API / DOM fallback)
│       └── aggregator_scraper.py # price.ru (curl_cffi + BeautifulSoup4)
├── static/
│   └── index.html          # Single-file фронтенд (HTML + CSS + Vanilla JS)
└── browser_profile/        # Папка persistent-профиля Camoufox (куки, localStorage)
```

## 3. Стек технологий

### Backend (Python)

| Компонент | Версия | Назначение |
|-----------|--------|------------|
| FastAPI | `>=0.115.0` | Веб-фреймворк, роутинг, валидация, OpenAPI |
| Uvicorn | `>=0.30.0` | ASGI-сервер для запуска FastAPI |
| Pydantic | `>=2.9.0` | Валидация данных, модели API |
| pydantic-settings | `>=2.5.0` | Загрузка настроек из `.env` |
| curl_cffi | `>=0.7.3` | HTTP-клиент с TLS-impersonation (Chrome 124) |
| Camoufox | `>=0.4.11` | Антидетект-браузер (пропатченный Firefox) |
| Playwright | `>=1.48.0` | Внутренняя зависимость Camoufox для управления браузером |
| BeautifulSoup4 | `>=4.12.3` | Парсинг HTML для агрегатора |
| lxml | `>=5.3.0` | Парсер HTML для BeautifulSoup4 |
| cachetools | `>=5.5.0` | TTLCache для кэширования поисковых запросов |
| python-dotenv | `>=1.0.1` | Загрузка переменных из `.env` |

### Фронтенд

| Компонент | Назначение |
|-----------|------------|
| Vanilla JS (ES6+) | Логика интерфейса, работа с API, локальное хранилище |
| CSS3 (переменные) | Тёмная тема, адаптивная вёрстка, анимации |
| localStorage | Сохранение истории поисков между сессиями |

## 4. Компоненты системы

### 4.1 `app/config.py` — Конфигурация

Реализовано на `pydantic-settings.BaseSettings`. Все параметры загружаются из `.env`.

| Параметр | По умолчанию | Описание |
|----------|--------------|----------|
| `HEADLESS_MODE` | `true` | Режим браузера Camoufox (`true` — без окна, `false` — с окном для ручной капчи) |
| `PROXY_LIST` | `""` | Список прокси через запятую (HTTP / SOCKS5) |
| `REQUEST_TIMEOUT` | `15` | Таймаут HTTP-запроса в секундах |
| `MAX_RETRIES` | `3` | Количество повторов при 429/403 |
| `CACHE_TTL_SECONDS` | `900` | TTL кэша поиска (15 минут) |
| `PROXY_BAN_MINUTES` | `10` | Время карантина забаненного прокси |
| `PORT` | `8000` | Порт FastAPI сервера |

### 4.2 `app/models.py` — Контракты данных

| Модель | Поля | Назначение |
|--------|------|------------|
| `SearchRequest` | `query` (str, 2-120), `condition` (`new`/`all`) | Входящий запрос поиска |
| `ProductOffer` | `title`, `price` (int), `currency`, `marketplace`, `url`, `is_used`, `source_adapter` | Единое предложение товара |
| `ScraperStatus` | `adapter`, `status` (`ok`/`timeout`/`blocked`/`error`), `items_found`, `elapsed_ms` | Статус работы скрапера |
| `SearchResponse` | `query`, `condition`, `cached`, `elapsed_ms`, `sources`, `items` | Ответ API |

### 4.3 `app/scrapers/base.py` — Базовый класс скраперов

- **`BaseScraper`** — абстрактный класс с методом `safe_search()`, который оборачивает `search()` в try/except, перехватывает `BlockedException`, логирует ошибки и возвращает `[]` при сбое.
- **`CircuitBreaker`** — отслеживает неудачи по доменам. При превышении лимита (`max_failures=3`) домен блокируется на `ban_minutes` (10 мин).
- **`BlockedException`** — выбрасывается при 403/429/капче/антиботе.

### 4.4 Адаптеры источников (`app/scrapers/`)

| Адаптер | Источник | Движок | Стратегия |
|---------|----------|--------|-----------|
| `WBScraper` | Wildberries | `curl_cffi` (chrome124) | Прямой JSON API `search.wb.ru/exactmatch` |
| `AggregatorScraper` | price.ru | `curl_cffi` + BeautifulSoup4 | HTML-парсинг карточек товаров |
| `AvitoScraper` | Авито | `Camoufox` | Перехват XHR-ответов + парсинг DOM карточек |
| `OzonScraper` | Ozon | `Camoufox` | Перехват `/api/entrypoint-api.bx/page/json/v2` + DOM fallback |

Все скраперы наследуют `BaseScraper` и возвращают список `ProductOffer`.

### 4.5 `app/browser_pool.py` — Пул браузеров Camoufox

- Singleton-паттерн, один экземпляр браузера на всё приложение.
- Параметры запуска: `headless=HEADLESS_MODE`, `persistent_context=True`, `user_data_dir="./browser_profile"`, `humanize=True`, `os="windows"`.
- `asyncio.Semaphore(2)` ограничивает одновременное открытие вкладок.
- Страница закрывается в `finally` после каждого поиска, контекст браузера остаётся живым (сохраняются куки).

### 4.6 `app/proxy_manager.py` — Ротатор прокси

- Поддержка HTTP и SOCKS5 прокси.
- Карантин забаненных прокси на настроенное время (`PROXY_BAN_MINUTES`).
- Автоматическое возвращение прокси в ротацию после истечения карантина.

### 4.7 `app/pipeline.py` — Конвейер очистки (6 этапов)

| Этап | Название | Действие |
|------|----------|----------|
| 1 | Нормализация цены | Удаление пробелов, `₽`, `руб.`, `от`; извлечение цифр; отсев `<= 100` руб. |
| 2 | Очистка URL + дедупликация | Удаление UTM/трекинг-параметров; удаление дублей по `(marketplace, url)` |
| 3 | Токен-матчинг | Числовые/модельные токены из запроса обязаны присутствовать в заголовке |
| 4 | Стоп-слова + б/у фильтр | Базовый список стоп-слов (исключая слова из запроса); при `condition=new` отсев б/у |
| 5 | Медианный отсев выбросов | Удаление цен `< median * 0.40` и `> median * 3.0` (если >= 4 товара) |
| 6 | Финальная сортировка | Сортировка по возрастанию цены, берутся топ-5 |

### 4.8 `app/orchestrator.py` — Оркестратор (Scatter-Gather)

- Запускает все скраперы параллельно через `asyncio.gather(return_exceptions=True)`.
- **TTLCache** (`cachetools`) — кэш ответов на 15 минут по ключу `query:condition`.
- **Singleflight** — если identical запрос уже выполняется, новые вызывающие ждут результат первого.
- Интегрирует `browser_pool` и `proxy_manager`.

### 4.9 `app/main.py` — FastAPI приложение

- `lifespan` — запуск `browser_pool` при старте, остановка при завершении.
- `POST /api/search` — основной эндпоинт поиска.
- `GET /api/health` — проверка работоспособности.
- `GET /` — раздача `static/index.html`.
- CORS разрешён для всех источников.

### 4.10 `static/index.html` — Фронтенд

- Единый файл без внешних CDN-зависимостей.
- Тёмная тема (`#0f172a` фон, `#1e293b` карточки, акцент `#3b82f6`).
- Переключатель `[Только новые]` / `[Включая б/у]`.
- Быстрые теги: `PS5 Slim`, `RTX 5060 Ti 16GB`, `iPhone 15 128GB`, `Dyson V15`.
- Пошаговый статус: `Опрос агрегаторов и маркетплейсов...` → `Фильтрация дублей и проверка цен...`.
- 5 карточек с цветным бейджем магазина, ценой в формате `52 490 ₽`, кнопкой «Купить» (`target="_blank"`).
- Телеметрия: время поиска, количество проверенных площадок.
- История поисков в `localStorage`, кнопка «Очистить историю».

## 5. Поток данных (Data Flow)

```
Пользователь вводит запрос в UI
        │
        ▼
POST /api/search  {"query": "...", "condition": "new"}
        │
        ▼
Orchestrator.search()
        │
        ├─► Проверка TTLCache (hit? → вернуть кэш)
        │
        ├─► Singleflight (уже в работе? → ждать результат)
        │
        ▼
asyncio.gather([wb, aggregator, avito, ozon].safe_search(query))
        │
        ├─► WBScraper: curl_cffi → JSON API → ProductOffer[]
        ├─► AggregatorScraper: curl_cffi → HTML → BeautifulSoup → ProductOffer[]
        ├─► AvitoScraper: Camoufox → XHR/DOM → ProductOffer[]
        └─► OzonScraper: Camoufox → entrypoint API / DOM → ProductOffer[]
        │
        ▼
Объединение всех предложений
        │
        ▼
Pipeline.run_pipeline() — 6 этапов очистки
        │
        ▼
Топ-5 предложений, сортировка по цене
        │
        ▼
SearchResponse → JSON клиенту
        │
        ▼
UI отображает 5 карточек + телеметрию
```

## 6. Антибан-защита

| Мера | Реализация |
|------|------------|
| TLS-impersonation | `curl_cffi.requests.AsyncSession(impersonate="chrome124")` — подмена JA3/JA4 отпечатка под Chrome 124 |
| Антидетект-браузер | `Camoufox` с `humanize=True` (гуманизация движений мыши и таймингов) |
| Постоянный профиль | `user_data_dir="./browser_profile"` — куки и localStorage сохраняются между перезапусками |
| Ротация прокси | `ProxyManager` с карантином забаненных прокси на 10 минут |
| Circuit Breaker | Домены с Repeated 403/429 блокируются на 10 минут |
| Семафор вкладок | `asyncio.Semaphore(2)` — не более 2 одновременных вкладок браузера |
| Случайные задержки | `asyncio.sleep(random.uniform(1.2, 2.5))` после загрузки страниц |
| Прокрутка страницы | `page.mouse.wheel(0, random.randint(400, 900))` для имитации пользователя |

## 7. Конфигурация и запуск

### 7.1 Установка зависимостей

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m camoufox fetch
```

> **Важно:** `python -m camoufox fetch` обязателен для скачивания бинарника пропатченного Firefox. Без него `AsyncCamoufox` не запустится.

### 7.2 Настройка `.env`

Скопируйте `.env.example` в `.env` и при необходимости отредактируйте:

```
HEADLESS_MODE=true          # false — окно браузера для ручной капчи
PROXY_LIST=                 # прокси через запятую (опционально)
REQUEST_TIMEOUT=15
MAX_RETRIES=3
CACHE_TTL_SECONDS=900
PROXY_BAN_MINUTES=10
PORT=8000
```

### 7.3 Запуск сервера

```powershell
uvicorn app.main:app --reload
```

Сервер: http://localhost:8000

### 7.4 Тест скраперов

```powershell
python test_scrapers.py
```

Проверяет каждый источник на запросе `"iPhone 15 128GB"` и выводит количество товаров и время ответа.

## 8. API endpoints

| Метод | Путь | Тело/Query | Ответ |
|-------|------|------------|-------|
| `GET` | `/api/health` | — | `{"status": "ok"}` |
| `POST` | `/api/search` | `{"query": "str", "condition": "new|all"}` | `SearchResponse` |
| `GET` | `/` | — | `static/index.html` |

## 9. Добавление нового магазина

1. Создать файл `app/scrapers/<name>_scraper.py`.
2. Наследоваться от `BaseScraper`.
3. Реализовать абстрактный метод `async def search(self, query) -> List[ProductOffer]`.
4. Зарегистрировать скрапер в `Orchestrator._execute_search()` в списке `scrapers`.
5. Запустить `python test_scrapers.py` для проверки.

## 10. Ограничения и план развития

- **Ограничения:** поиск зависит от структуры HTML/JSON целевых сайтов; при изменении вёрстки адаптеры требуют обновления.
- **Планы:** добавление новых адаптеров (DNS, Ситилинк, Яндекс Маркет), расширение пула прокси, поддержка региональных настроек.