"""Обёртка над OpenAI-совместимым API локальной модели: вызовы, ретраи, healthcheck."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import openai
from openai import OpenAI

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 2.0
HEALTH_TIMEOUT_SECONDS = 10.0


class LLMError(Exception):
    """Ошибка вызова модели."""


class LLMUnavailableError(LLMError):
    """До модели не удалось достучаться: нет соединения, таймаут, сервер отвечает 5xx."""


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int
    finish_reason: str | None


@dataclass(frozen=True, slots=True)
class LLMHealth:
    ok: bool
    base_url: str
    model: str
    latency_ms: int | None = None
    # None — сервер не отдаёт список моделей, проверить наличие нельзя
    model_available: bool | None = None
    models: list[str] = field(default_factory=list)
    error: str | None = None


def _describe(exc: Exception) -> str:
    if isinstance(exc, openai.APITimeoutError):
        return "превышено время ожидания ответа"
    if isinstance(exc, openai.APIConnectionError):
        cause = exc.__cause__
        return f"нет соединения ({cause})" if cause else "нет соединения"
    if isinstance(exc, openai.APIStatusError):
        return f"HTTP {exc.status_code}: {exc.message}"
    return f"{type(exc).__name__}: {exc}"


class LLMClient:
    def __init__(
        self,
        client: OpenAI,
        *,
        model: str,
        base_url: str,
        temperature: float,
        max_tokens: int,
        extra_body: dict[str, Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self.model = model
        self.base_url = base_url
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._extra_body = extra_body or None
        self._sleep = sleep

    @classmethod
    def from_settings(cls, settings: Settings) -> "LLMClient":
        client = OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key.get_secret_value(),
            timeout=settings.llm_timeout_seconds,
            # ретраи делаются здесь, чтобы каждый из них попадал в лог
            max_retries=0,
        )
        return cls(
            client,
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            extra_body=settings.llm_extra_body,
        )

    def complete(self, system: str, user: str, *, purpose: str) -> LLMResponse:
        """Один запрос к модели с ретраями на сетевых ошибках, таймаутах, 429 и 5xx."""
        log.debug("llm_request", purpose=purpose, system_prompt=system, user_prompt=user)
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            started = time.perf_counter()
            try:
                completion = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                    extra_body=self._extra_body,
                )
            except (
                openai.APIConnectionError,
                openai.RateLimitError,
                openai.InternalServerError,
            ) as exc:
                last_error = exc
                duration_ms = round((time.perf_counter() - started) * 1000)
                event = "llm_timeout" if isinstance(exc, openai.APITimeoutError) else "llm_error"
                log.warning(
                    event,
                    purpose=purpose,
                    model=self.model,
                    attempt=attempt,
                    error=_describe(exc),
                    duration_ms=duration_ms,
                )
                if attempt < MAX_ATTEMPTS:
                    backoff = BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
                    log.info("llm_retry", purpose=purpose, attempt=attempt, backoff_seconds=backoff)
                    self._sleep(backoff)
                continue
            except openai.APIStatusError as exc:
                log.error("llm_error", purpose=purpose, model=self.model, error=_describe(exc))
                raise LLMError(f"LLM вернула ошибку — {_describe(exc)}") from exc

            duration_ms = round((time.perf_counter() - started) * 1000)
            choice = completion.choices[0] if completion.choices else None
            text = (choice.message.content or "") if choice else ""
            usage = completion.usage
            response = LLMResponse(
                text=text,
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                duration_ms=duration_ms,
                finish_reason=choice.finish_reason if choice else None,
            )
            log.info(
                "llm_call",
                purpose=purpose,
                model=self.model,
                attempt=attempt,
                prompt_chars=len(system) + len(user),
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                finish_reason=response.finish_reason,
                duration_ms=duration_ms,
            )
            if response.finish_reason == "length":
                log.warning("llm_response_truncated", purpose=purpose, max_tokens=self._max_tokens)
            log.debug("llm_response", purpose=purpose, response=text)
            return response

        assert last_error is not None
        raise LLMUnavailableError(
            f"LLM недоступна по адресу {self.base_url} (модель {self.model}): "
            f"{_describe(last_error)}. Проверьте, что модель запущена, "
            "и значения LLM_BASE_URL / LLM_MODEL"
        ) from last_error

    def health(self) -> LLMHealth:
        """Проверка доступности: models.list(), а если его нет — короткий тестовый запрос."""
        from app.services.prompts import HEALTH_PROMPT

        started = time.perf_counter()
        client = self._client.with_options(timeout=HEALTH_TIMEOUT_SECONDS)
        try:
            try:
                models = [m.id for m in client.models.list().data]
                model_available: bool | None = self.model in models if models else None
            except openai.APIStatusError:
                client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": HEALTH_PROMPT}],
                    max_tokens=1,
                )
                models, model_available = [], True
        except openai.OpenAIError as exc:
            log.warning("llm_health_failed", base_url=self.base_url, error=_describe(exc))
            return LLMHealth(
                ok=False, base_url=self.base_url, model=self.model, error=_describe(exc)
            )

        latency_ms = round((time.perf_counter() - started) * 1000)
        error = None
        if model_available is False:
            error = f"Модель {self.model} не найдена на сервере"
        return LLMHealth(
            ok=model_available is not False,
            base_url=self.base_url,
            model=self.model,
            latency_ms=latency_ms,
            model_available=model_available,
            models=models[:50],
            error=error,
        )
