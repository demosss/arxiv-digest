import pytest

from app.core.config import ConfigError, Settings, load_settings


def test_missing_required_variable_fails_with_clear_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SECRET_KEY")
    monkeypatch.setenv("LLM_MODEL", "")

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    message = str(exc_info.value)
    assert "SECRET_KEY" in message
    assert "LLM_MODEL" in message
    assert ".env" in message


def test_database_urls_use_different_drivers() -> None:
    settings = Settings()

    assert settings.async_database_url.drivername == "postgresql+asyncpg"
    assert settings.sync_database_url.drivername == "postgresql+psycopg"
    assert settings.async_database_url.database == settings.sync_database_url.database


def test_broker_url_escapes_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RABBITMQ_PASSWORD", "p@ss/word")

    assert "p%40ss%2Fword@" in Settings().broker_url


def test_smtp_password_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().smtp_password_set is True
    monkeypatch.setenv("SMTP_PASSWORD", "")
    assert Settings().smtp_password_set is False
