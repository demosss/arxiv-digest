"""Вспомогательные объекты для тестов: статьи arXiv, поддельный источник, PDF с текстом."""

from datetime import UTC, datetime
from pathlib import Path

import pymupdf

from app.services.arxiv_client import ArxivEntry, ArxivError
from app.services.llm_client import LLMHealth, LLMResponse
from app.services.pdf_text import ExtractedText

PERIOD_START = datetime(2024, 10, 5, 0, 0, tzinfo=UTC)
PERIOD_END = datetime(2024, 10, 5, 23, 59, 59, tzinfo=UTC)
PARAGRAPH = "This paragraph is long enough to look like real body text of a paper. " * 12


def make_entry(arxiv_id: str, title: str = "", abstract: str = "") -> ArxivEntry:
    return ArxivEntry(
        arxiv_id=arxiv_id,
        version=1,
        title=title or f"Paper {arxiv_id}",
        authors=["Alice Smith", "Bob Jones"],
        abstract=abstract or "An abstract about large language models.",
        categories=["cs.CL"],
        published_at=datetime(2024, 10, 5, 12, 0, tzinfo=UTC),
        updated_at=datetime(2024, 10, 5, 12, 0, tzinfo=UTC),
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v1",
        abs_url=f"https://arxiv.org/abs/{arxiv_id}",
    )


def make_pdf(path: Path, pages: list[str] | None = None) -> None:
    document = pymupdf.open()
    for body in pages or [PARAGRAPH, PARAGRAPH]:
        page = document.new_page()
        page.insert_textbox(pymupdf.Rect(72, 72, 520, 760), body, fontsize=10)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)
    document.close()


def fake_extractor(path: Path) -> ExtractedText:
    text = f"Extracted text of {path.name}. " + PARAGRAPH
    return ExtractedText(text=text, pages=2, raw_chars=len(text) + 50)


class FakeSource:
    """Источник статей вместо arXiv: результаты выбираются по подстроке поискового запроса."""

    def __init__(
        self,
        results: dict[str, list[ArxivEntry]] | None = None,
        failing_ids: set[str] | None = None,
        failing_queries: set[str] | None = None,
    ) -> None:
        self.results = results or {}
        self.failing_ids = failing_ids or set()
        # подстроки запросов, на которые arXiv отвечает ошибкой
        self.failing_queries = failing_queries or set()
        self.queries: list[str] = []
        self.limits: list[int] = []
        self.downloads: list[str] = []

    def search(self, query: str, max_results: int) -> list[ArxivEntry]:
        self.queries.append(query)
        self.limits.append(max_results)
        if any(needle in query for needle in self.failing_queries):
            raise ArxivError("arXiv API вернул ошибку: incorrect query")
        for needle, entries in self.results.items():
            if needle in query:
                return entries[:max_results]
        return []

    def search_page(self, query: str, max_results: int) -> tuple[list[ArxivEntry], int]:
        entries = self.search(query, 10_000)
        return entries[:max_results], len(entries)

    def download_pdf(self, url: str, destination: Path) -> int:
        self.downloads.append(url)
        if any(arxiv_id in url for arxiv_id in self.failing_ids):
            raise ArxivError(f"Скачивание PDF: HTTP 404 ({url})")
        make_pdf(destination)
        return destination.stat().st_size


RUSSIAN_SUMMARY = (
    "Задача: авторы исследуют, как большие языковые модели (large language models) "
    "справляются с многошаговыми рассуждениями при ограниченном контексте.\n\n"
    "Метод: предложен подход, в котором модель сначала строит план решения, а затем "
    "последовательно выполняет его шаги, сверяясь с промежуточными результатами.\n\n"
    "Результаты: на двенадцати наборах данных точность выросла на 7,5 процентных пункта "
    "по сравнению с базовой моделью.\n\n"
    "Значимость: работа показывает, что качество рассуждений можно повысить без дообучения."
)
RUSSIAN_ABSTRACT = (
    "Авторы исследуют, как большие языковые модели справляются с многошаговыми рассуждениями. "
    "Предложен подход, в котором модель сначала строит план, а затем выполняет его по шагам. "
    "На двенадцати наборах данных точность выросла на 7,5 процентных пункта. "
    "Метод не требует дообучения и применим к уже развёрнутым моделям."
)
RUSSIAN_NOTE = "Конспект фрагмента: описан метод планирования и результаты на 12 бенчмарках."


class FakeLLM:
    """Модель-заглушка: отдаёт заранее заданные ответы и запоминает все запросы."""

    model = "fake-model"

    def __init__(self, replies: list[str | Exception] | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[dict[str, str]] = []

    def complete(self, system: str, user: str, *, purpose: str) -> LLMResponse:
        self.calls.append({"system": system, "user": user, "purpose": purpose})
        if self.replies:
            reply = self.replies.pop(0)
        elif purpose == "abstract":
            reply = RUSSIAN_ABSTRACT
        else:
            reply = RUSSIAN_SUMMARY if purpose in ("summary", "reduce") else RUSSIAN_NOTE
        if isinstance(reply, Exception):
            raise reply
        return LLMResponse(
            text=reply,
            prompt_tokens=100,
            completion_tokens=40,
            duration_ms=250,
            finish_reason="stop",
        )

    def health(self) -> LLMHealth:
        return LLMHealth(ok=True, base_url="http://llm.test/v1", model=self.model, latency_ms=5)
