from itertools import pairwise
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from app.services import prompts
from app.services.llm_client import LLMClient, LLMError, LLMUnavailableError
from app.services.llm_summarizer import (
    Summarizer,
    SummaryValidationError,
    clean_response,
    split_into_chunks,
    validate_response,
)
from tests.factories import RUSSIAN_NOTE, RUSSIAN_SUMMARY, FakeLLM

REQUEST = httpx.Request("POST", "http://llm.test/v1/chat/completions")


def _paragraphs(count: int, size: int = 900) -> list[str]:
    return [f"Paragraph {i:03d}. " + "word " * ((size - 16) // 5) for i in range(count)]


# --- чанкинг ---


def test_short_text_is_single_chunk() -> None:
    assert split_into_chunks("  Short text.\n\nSecond paragraph.  ", 1000) == [
        "Short text.\n\nSecond paragraph."
    ]
    assert split_into_chunks("   ", 1000) == []


def test_chunks_respect_limit_and_paragraph_boundaries() -> None:
    paragraphs = _paragraphs(30)
    text = "\n\n".join(paragraphs)

    chunks = split_into_chunks(text, 5000)

    assert len(chunks) > 1
    assert all(len(chunk) <= 5000 for chunk in chunks)
    # каждый абзац целиком попал хотя бы в один чанк и порядок сохранён
    positions = [
        next(i for i, chunk in enumerate(chunks) if p.strip() in chunk) for p in paragraphs
    ]
    assert positions == sorted(positions)
    # чанк начинается с перекрытия или с начала абзаца, но не с середины слова
    assert all(chunk.split("\n\n")[-1].startswith("Paragraph") for chunk in chunks)


def test_chunks_overlap_by_about_five_percent() -> None:
    text = "\n\n".join(_paragraphs(30))

    chunks = split_into_chunks(text, 5000)

    for previous, current in pairwise(chunks):
        overlap = current.split("\n\n")[0]
        assert 0 < len(overlap) <= 250
        assert previous.endswith(overlap)


def test_overlap_can_be_disabled() -> None:
    paragraphs = _paragraphs(12)

    chunks = split_into_chunks("\n\n".join(paragraphs), 3000, overlap_ratio=0)

    assert "\n\n".join(chunks) == "\n\n".join(p.strip() for p in paragraphs)


def test_long_paragraph_is_split_by_sentences() -> None:
    sentences = [f"Sentence number {i} ends here." for i in range(400)]

    chunks = split_into_chunks(" ".join(sentences), 2000)

    assert len(chunks) > 1
    assert all(len(chunk) <= 2000 for chunk in chunks)
    body_ends = [chunk.rstrip()[-5:] for chunk in chunks]
    assert all(end == "here." for end in body_ends)
    assert all(sentence in " ".join(chunks) for sentence in sentences)


def test_text_without_spaces_is_split_hard() -> None:
    chunks = split_into_chunks("x" * 10_000, 1000)

    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert sum(len(chunk) for chunk in chunks) >= 10_000


# --- очистка и проверка ответа ---


def test_clean_response_strips_reasoning_and_markdown() -> None:
    raw = (
        "<think>Let me think...\nstep 1</think>\n\n"
        "## Резюме\n**Задача:** текст.\n\n\n\n__Метод:__ ещё."
    )

    assert clean_response(raw) == "Резюме\nЗадача: текст.\n\nМетод: ещё."


def test_unclosed_think_block_means_empty_answer() -> None:
    assert clean_response("<think>reasoning that hit the token limit") == ""


@pytest.mark.parametrize(
    ("text", "final", "expected"),
    [
        ("", True, "пустой"),
        ("As an AI language model, I cannot summarize this paper. " * 6, True, "служебную фразу"),
        ("Извините, но я не могу выполнить этот запрос. " * 8, True, "служебную фразу"),
        ("Sure! Here is the summary of the paper you asked for. " * 6, True, "служебную фразу"),
        ("Задача: кратко.", True, "слишком короткий"),
        ("Problem: the authors study language models. " * 10, True, "на русском"),
        ("ok", False, "слишком короткий"),
    ],
)
def test_invalid_responses_are_rejected(text: str, final: bool, expected: str) -> None:
    reason = validate_response(text, final=final)

    assert reason is not None and expected in reason


def test_valid_responses_pass() -> None:
    assert validate_response(RUSSIAN_SUMMARY, final=True) is None
    assert validate_response(RUSSIAN_NOTE, final=False) is None
    # конспект может быть и на английском
    assert validate_response("The fragment describes the training setup.", final=False) is None


# --- map-reduce ---


def test_single_chunk_uses_one_call() -> None:
    llm = FakeLLM()

    result = Summarizer(llm, context_chars=24000).summarize(
        "A Paper Title", "The abstract.", "Body paragraph one.\n\nBody paragraph two."
    )

    assert result.text == RUSSIAN_SUMMARY
    assert result.chunks_count == 1
    assert result.llm_calls == 1
    assert result.model == "fake-model"
    assert (result.prompt_tokens, result.completion_tokens, result.duration_ms) == (100, 40, 250)
    call = llm.calls[0]
    assert call["purpose"] == "summary"
    assert call["system"] == prompts.SYSTEM_PROMPT
    assert "Название: A Paper Title" in call["user"]
    assert "Аннотация: The abstract." in call["user"]
    assert "Body paragraph two." in call["user"]
    assert "150–250 слов" in call["user"]
    for section in prompts.SUMMARY_SECTIONS:
        assert f"{section}:" in call["user"]


def test_long_text_is_map_reduced() -> None:
    paragraphs = _paragraphs(12)
    llm = FakeLLM(
        [
            "Конспект первого фрагмента.",
            "Конспект второго фрагмента.",
            "Конспект третьего фрагмента.",
        ]
    )

    result = Summarizer(llm, context_chars=5000).summarize(
        "A Paper Title", "The abstract.", "\n\n".join(paragraphs)
    )

    assert result.chunks_count == 3
    assert result.llm_calls == 4
    assert [call["purpose"] for call in llm.calls] == ["map 1/3", "map 2/3", "map 3/3", "reduce"]
    assert (result.prompt_tokens, result.completion_tokens, result.duration_ms) == (400, 160, 1000)

    first_map = llm.calls[0]["user"]
    assert "фрагмент 1 из 3" in first_map
    assert "«A Paper Title»" in first_map
    assert "Paragraph 000." in first_map
    assert "Paragraph 011." not in first_map
    assert "Paragraph 011." in llm.calls[2]["user"]

    reduce = llm.calls[3]["user"]
    assert "Название: A Paper Title" in reduce
    assert "Аннотация: The abstract." in reduce
    assert "[Фрагмент 1]\nКонспект первого фрагмента." in reduce
    assert "[Фрагмент 3]\nКонспект третьего фрагмента." in reduce
    # полный текст в reduce не передаётся — только конспекты
    assert "Paragraph 000." not in reduce
    assert result.text == RUSSIAN_SUMMARY


def test_invalid_answer_is_retried_once() -> None:
    llm = FakeLLM(["As an AI, I cannot do that.", RUSSIAN_SUMMARY])

    result = Summarizer(llm, context_chars=24000).summarize("T", "A", "Body text.")

    assert result.text == RUSSIAN_SUMMARY
    assert result.llm_calls == 2
    assert result.prompt_tokens == 200
    assert "Предыдущий ответ отклонён" not in llm.calls[0]["user"]
    assert "Предыдущий ответ отклонён: ответ содержит служебную фразу" in llm.calls[1]["user"]


def test_two_invalid_answers_fail() -> None:
    llm = FakeLLM(["", "<think>still thinking"])

    with pytest.raises(SummaryValidationError, match="ответ пустой"):
        Summarizer(llm, context_chars=24000).summarize("T", "A", "Body text.")

    assert len(llm.calls) == 2


def test_notes_are_collapsed_when_they_do_not_fit() -> None:
    paragraphs = _paragraphs(12)
    long_note = "Очень подробный конспект фрагмента. " * 60
    llm = FakeLLM([long_note, long_note, long_note, "Сжатый конспект первых фрагментов."])

    result = Summarizer(llm, context_chars=5000).summarize("T", "A", "\n\n".join(paragraphs))

    purposes = [call["purpose"] for call in llm.calls]
    assert purposes[:3] == ["map 1/3", "map 2/3", "map 3/3"]
    assert purposes[3].startswith("collapse")
    assert purposes[-1] == "reduce"
    assert "Сжатый конспект первых фрагментов." in llm.calls[-1]["user"]
    assert result.text == RUSSIAN_SUMMARY


def test_empty_text_is_an_error() -> None:
    with pytest.raises(LLMError, match="нет текста"):
        Summarizer(FakeLLM(), context_chars=24000).summarize("T", "A", "   ")


# --- клиент OpenAI-совместимого API ---


def _completion(text: str = "ok", finish_reason: str = "stop") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=finish_reason)
        ],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
    )


class FakeOpenAI:
    def __init__(self, replies: list[Any], models: Any = None) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self._models = models
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=self._list_models)

    def with_options(self, **_: Any) -> "FakeOpenAI":
        return self

    def _create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def _list_models(self) -> Any:
        if isinstance(self._models, Exception):
            raise self._models
        return SimpleNamespace(data=[SimpleNamespace(id=name) for name in self._models or []])


def _client(fake: FakeOpenAI, sleeps: list[float] | None = None, **kwargs: Any) -> LLMClient:
    return LLMClient(
        fake,  # type: ignore[arg-type]
        model="qwen-test",
        base_url="http://llm.test/v1",
        temperature=0.2,
        max_tokens=512,
        sleep=(sleeps if sleeps is not None else []).append,
        **kwargs,
    )


def test_complete_sends_configured_parameters() -> None:
    fake = FakeOpenAI([_completion("Ответ модели")])

    response = _client(fake, extra_body={"reasoning_effort": "none"}).complete(
        "system text", "user text", purpose="summary"
    )

    assert response.text == "Ответ модели"
    assert (response.prompt_tokens, response.completion_tokens) == (11, 7)
    assert response.finish_reason == "stop"
    request = fake.requests[0]
    assert request["model"] == "qwen-test"
    assert request["temperature"] == 0.2
    assert request["max_tokens"] == 512
    assert request["extra_body"] == {"reasoning_effort": "none"}
    assert request["messages"] == [
        {"role": "system", "content": "system text"},
        {"role": "user", "content": "user text"},
    ]


def test_complete_retries_network_errors_with_backoff() -> None:
    sleeps: list[float] = []
    fake = FakeOpenAI(
        [
            openai.APIConnectionError(request=REQUEST),
            openai.APITimeoutError(request=REQUEST),
            _completion("после ретраев"),
        ]
    )

    response = _client(fake, sleeps).complete("s", "u", purpose="summary")

    assert response.text == "после ретраев"
    assert len(fake.requests) == 3
    assert sleeps == [2.0, 4.0]


def test_unreachable_llm_raises_clear_error() -> None:
    fake = FakeOpenAI([openai.APIConnectionError(request=REQUEST)] * 3)

    with pytest.raises(LLMUnavailableError) as exc_info:
        _client(fake).complete("s", "u", purpose="summary")

    message = str(exc_info.value)
    assert "LLM недоступна по адресу http://llm.test/v1" in message
    assert "qwen-test" in message
    assert "LLM_BASE_URL" in message
    assert len(fake.requests) == 3


def test_client_error_is_not_retried() -> None:
    error = openai.NotFoundError(
        "model 'qwen-test' not found", response=httpx.Response(404, request=REQUEST), body=None
    )
    fake = FakeOpenAI([error])

    with pytest.raises(LLMError, match="HTTP 404") as exc_info:
        _client(fake).complete("s", "u", purpose="summary")

    assert not isinstance(exc_info.value, LLMUnavailableError)
    assert len(fake.requests) == 1


def test_health_ok_when_model_is_listed() -> None:
    health = _client(FakeOpenAI([], models=["other", "qwen-test"])).health()

    assert health.ok is True
    assert health.model_available is True
    assert health.models == ["other", "qwen-test"]
    assert health.latency_ms is not None
    assert health.error is None


def test_health_reports_missing_model() -> None:
    health = _client(FakeOpenAI([], models=["other"])).health()

    assert health.ok is False
    assert health.model_available is False
    assert health.error == "Модель qwen-test не найдена на сервере"


def test_health_reports_connection_error() -> None:
    health = _client(FakeOpenAI([], models=openai.APIConnectionError(request=REQUEST))).health()

    assert health.ok is False
    assert health.error is not None and "нет соединения" in health.error


def test_health_falls_back_to_test_request() -> None:
    not_supported = openai.NotFoundError(
        "no /models endpoint", response=httpx.Response(404, request=REQUEST), body=None
    )
    fake = FakeOpenAI([_completion("ок")], models=not_supported)

    health = _client(fake).health()

    assert health.ok is True
    assert health.model_available is True
    assert fake.requests[0]["max_tokens"] == 1
