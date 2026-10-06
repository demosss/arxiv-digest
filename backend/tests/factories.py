"""Вспомогательные объекты для тестов: статьи arXiv, поддельный источник, PDF с текстом."""

from datetime import UTC, datetime
from pathlib import Path

import fitz

from app.services.arxiv_client import ArxivEntry, ArxivError
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
    document = fitz.open()
    for body in pages or [PARAGRAPH, PARAGRAPH]:
        page = document.new_page()
        page.insert_textbox(fitz.Rect(72, 72, 520, 760), body, fontsize=10)
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
    ) -> None:
        self.results = results or {}
        self.failing_ids = failing_ids or set()
        self.queries: list[str] = []
        self.downloads: list[str] = []

    def search(self, query: str, max_results: int) -> list[ArxivEntry]:
        self.queries.append(query)
        for needle, entries in self.results.items():
            if needle in query:
                return entries[:max_results]
        return []

    def download_pdf(self, url: str, destination: Path) -> int:
        self.downloads.append(url)
        if any(arxiv_id in url for arxiv_id in self.failing_ids):
            raise ArxivError(f"Скачивание PDF: HTTP 404 ({url})")
        make_pdf(destination)
        return destination.stat().st_size
