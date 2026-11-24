from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Настройки приложения stealth-price-chat, загружаемые из .env."""

    # Режим работы браузера Camoufox: true — headless, false — с окном (для ручной капчи)
    HEADLESS_MODE: bool = Field(default=True, description="Headless режим Camoufox")

    # Список прокси через запятую (HTTP / SOCKS5)
    PROXY_LIST: str = Field(default="", description="Список прокси через запятую")

    # Таймаут HTTP-запросов в секундах
    REQUEST_TIMEOUT: int = Field(default=15, description="Таймаут запроса в секундах")

    # Максимальное количество попыток повтора при 429/403
    MAX_RETRIES: int = Field(default=3, description="Максимум повторов при блокировке")

    # TTL кэша поисковых запросов в секундах (15 минут)
    CACHE_TTL_SECONDS: int = Field(default=900, description="TTL кэша в секундах")

    # Время карантина забаненного прокси в минутах
    PROXY_BAN_MINUTES: int = Field(default=10, description="Минуты карантина прокси")

    # Порт сервера
    PORT: int = Field(default=8000, description="Порт FastAPI сервера")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()