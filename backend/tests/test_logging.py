import json
import logging

import pytest

from app.core.config import get_settings
from app.core.logging import REDACTED, SecretMasker, configure_logging, get_logger


def _last_json_line(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    parsed: dict[str, object] = json.loads(lines[-1])
    return parsed


def test_masker_redacts_sensitive_keys_recursively() -> None:
    masker = SecretMasker()

    result = masker.scrub(
        {
            "event": "login",
            "password": "hunter2",
            "payload": {"new_password": "x", "SMTP_PASSWORD": "y", "llm_api_key": "z"},
            "items": [{"access_token": "abc"}],
            "prompt_tokens": 42,
        }
    )

    assert result["password"] == REDACTED
    assert result["payload"] == {
        "new_password": REDACTED,
        "SMTP_PASSWORD": REDACTED,
        "llm_api_key": REDACTED,
    }
    assert result["items"] == [{"access_token": REDACTED}]
    assert result["prompt_tokens"] == 42
    assert result["event"] == "login"


def test_masker_scrubs_known_secret_values_and_bearer_tokens() -> None:
    masker = SecretMasker(["super-secret-value", "short"])

    assert masker.scrub("amqp://user:super-secret-value@host") == f"amqp://user:{REDACTED}@host"
    assert masker.scrub("Authorization: Bearer eyJhbGciOi.payload.sig") == (
        f"Authorization: Bearer {REDACTED}"
    )
    # короткие значения по содержимому не маскируются
    assert masker.scrub("a short word") == "a short word"


def test_structlog_output_is_json_with_service_and_masked_secrets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = get_settings()
    configure_logging("worker")

    get_logger("test").info(
        "smtp_connect",
        smtp_password=settings.smtp_password.get_secret_value() if settings.smtp_password else "",
        url=f"http://llm/?key={settings.llm_api_key.get_secret_value()}",
        arxiv_id="2410.00001",
    )

    record = _last_json_line(capsys)
    assert record["service"] == "worker"
    assert record["event"] == "smtp_connect"
    assert record["level"] == "info"
    assert record["arxiv_id"] == "2410.00001"
    assert record["smtp_password"] == REDACTED
    assert settings.llm_api_key.get_secret_value() not in str(record["url"])
    assert "timestamp" in record


def test_stdlib_records_go_through_the_same_pipeline(capsys: pytest.CaptureFixture[str]) -> None:
    settings = get_settings()
    configure_logging("beat")

    secret = settings.secret_key.get_secret_value()
    logging.getLogger("third.party").warning("connecting with %s", secret)

    record = _last_json_line(capsys)
    assert record["service"] == "beat"
    assert record["logger"] == "third.party"
    assert secret not in str(record["event"])
    assert REDACTED in str(record["event"])


def test_traceback_is_scrubbed(capsys: pytest.CaptureFixture[str]) -> None:
    settings = get_settings()
    configure_logging("api")
    secret = settings.postgres_password.get_secret_value()

    try:
        raise RuntimeError(f"cannot connect: password={secret}")
    except RuntimeError:
        get_logger("test").exception("db_error")

    record = _last_json_line(capsys)
    assert "RuntimeError" in str(record["exception"])
    assert secret not in str(record["exception"])
