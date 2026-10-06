"""Конфигурация сервиса: все параметры и секреты приходят из переменных окружения."""

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class ConfigError(SystemExit):
    """Невалидная конфигурация: процесс завершается с понятным сообщением без traceback."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Общие ---
    app_env: str = "production"
    tz: str = "Europe/Moscow"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    secret_key: SecretStr = Field(min_length=1)

    # --- Админ ---
    admin_username: str = Field(default="admin", min_length=1)
    admin_password: SecretStr = Field(min_length=1)

    # --- PostgreSQL ---
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "arxiv_digest"
    postgres_user: str = "arxiv"
    postgres_password: SecretStr = Field(min_length=1)

    # --- RabbitMQ ---
    rabbitmq_host: str = "rabbitmq"
    rabbitmq_port: int = 5672
    rabbitmq_user: str = "arxiv"
    rabbitmq_password: SecretStr = Field(min_length=1)

    # --- LLM ---
    llm_base_url: str = Field(min_length=1)
    llm_api_key: SecretStr = SecretStr("not-needed")
    llm_model: str = Field(min_length=1)
    llm_temperature: float = 0.2
    llm_max_tokens: int = Field(default=1024, gt=0)
    llm_context_chars: int = Field(default=24000, ge=1000)
    llm_timeout_seconds: float = Field(default=300, gt=0)
    llm_concurrency: int = Field(default=1, ge=1)
    llm_extra_body: dict[str, Any] = Field(default_factory=dict)

    # --- SMTP: из окружения берётся только пароль ---
    smtp_password: SecretStr | None = None

    # --- arXiv ---
    arxiv_request_delay_seconds: float = Field(default=3, ge=0)
    arxiv_max_results_per_topic: int = Field(default=200, gt=0)
    arxiv_user_agent: str = "arxiv-digest/1.0 (mailto:admin@example.com)"

    # --- Пайплайн ---
    send_empty_digest: bool = False
    pipeline_stale_hours: int = Field(default=6, gt=0)

    # --- Хранилище ---
    data_dir: Path = Path("/data")

    def _database_url(self, driver: str) -> URL:
        return URL.create(
            drivername=f"postgresql+{driver}",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    @property
    def async_database_url(self) -> URL:
        """URL для FastAPI (asyncpg)."""
        return self._database_url("asyncpg")

    @property
    def sync_database_url(self) -> URL:
        """URL для Celery и Alembic (psycopg v3)."""
        return self._database_url("psycopg")

    @property
    def broker_url(self) -> str:
        user = quote(self.rabbitmq_user, safe="")
        password = quote(self.rabbitmq_password.get_secret_value(), safe="")
        return f"amqp://{user}:{password}@{self.rabbitmq_host}:{self.rabbitmq_port}//"

    @property
    def smtp_password_set(self) -> bool:
        return bool(self.smtp_password and self.smtp_password.get_secret_value())

    @property
    def papers_dir(self) -> Path:
        return self.data_dir / "papers"

    @property
    def digests_dir(self) -> Path:
        return self.data_dir / "digests"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def secret_values(self) -> list[str]:
        """Значения всех секретов — для вычищения из логов."""
        secrets = [
            self.secret_key,
            self.admin_password,
            self.postgres_password,
            self.rabbitmq_password,
            self.llm_api_key,
            self.smtp_password,
        ]
        return [s.get_secret_value() for s in secrets if s is not None]


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        problems = [
            f"  {'.'.join(str(p) for p in err['loc']).upper()}: {err['msg']}"
            for err in exc.errors()
        ]
        raise ConfigError(
            "Ошибка конфигурации: проверьте переменные окружения (.env):\n" + "\n".join(problems)
        ) from exc


@lru_cache
def get_settings() -> Settings:
    return load_settings()
