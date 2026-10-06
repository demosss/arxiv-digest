"""Извлечение текста из PDF (PyMuPDF) и его очистка перед суммаризацией."""

import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import fitz

from app.core.logging import get_logger

log = get_logger(__name__)

# сколько строк сверху и снизу страницы проверяется на колонтитулы
EDGE_LINES = 2
# колонтитул — строка на краю страницы, повторяющаяся минимум на стольких страницах / их доле
MIN_REPEAT_PAGES = 3
MIN_REPEAT_SHARE = 0.4
# заголовок списка литературы ищется только в хвосте: в начале это может быть оглавление
REFERENCES_MIN_POSITION = 0.3
# меньше этого — скорее всего скан без текстового слоя
MIN_TEXT_CHARS = 500

_PAGE_NUMBER_RE = re.compile(r"^(page\s+)?\d{1,4}(\s*(/|of)\s*\d{1,4})?$", re.IGNORECASE)
_HYPHEN_BREAK_RE = re.compile(r"(?<=[a-zа-яё])-\n(?=[a-zа-яё])")
_REFERENCES_RE = re.compile(
    r"^[ \t]*(?:\d{1,2}\.?[ \t]+|[IVXLC]+\.[ \t]+)?"
    r"(?:references(?:[ \t]+and[ \t]+notes)?|bibliography|literature[ \t]+cited"
    r"|список[ \t]+литературы)"
    r"[ \t]*:?[ \t]*(?=\n|$|\[1\]|1\.[ \t])",
    re.IGNORECASE | re.MULTILINE,
)


class TextExtractionError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ExtractedText:
    text: str
    pages: int
    raw_chars: int


def _edge_key(line: str) -> str:
    """Нормализованный вид строки для поиска повторов: «Page 3 of 12» и «Page 4 of 12» совпадают."""
    return re.sub(r"\d+", "#", " ".join(line.lower().split()))


def _edge_depth(lines_count: int) -> int:
    """Сколько строк с каждого края считать колонтитулом: на коротких страницах — меньше."""
    return min(EDGE_LINES, max(1, lines_count // 3))


def strip_headers_and_footers(pages: list[str]) -> list[str]:
    """Убирает с краёв страниц номера страниц и повторяющиеся колонтитулы."""
    split_pages = [page.splitlines() for page in pages]

    counts: Counter[str] = Counter()
    for lines in split_pages:
        non_empty = [line for line in lines if line.strip()]
        depth = _edge_depth(len(non_empty))
        counts.update({_edge_key(line) for line in non_empty[:depth] + non_empty[-depth:]})
    threshold = max(MIN_REPEAT_PAGES, int(len(pages) * MIN_REPEAT_SHARE))
    repeated = {key for key, count in counts.items() if count >= threshold}

    cleaned: list[str] = []
    for lines in split_pages:
        non_empty_idx = [i for i, line in enumerate(lines) if line.strip()]
        depth = _edge_depth(len(non_empty_idx))
        edge_idx = set(non_empty_idx[:depth] + non_empty_idx[-depth:])
        kept = [
            line
            for i, line in enumerate(lines)
            if not (
                i in edge_idx
                and (_PAGE_NUMBER_RE.match(line.strip()) or _edge_key(line) in repeated)
            )
        ]
        cleaned.append("\n".join(kept))
    return cleaned


def cut_references(text: str) -> str:
    """Отрезает список литературы: всё после первого заголовка References в хвосте текста."""
    min_position = int(len(text) * REFERENCES_MIN_POSITION)
    matches = [m for m in _REFERENCES_RE.finditer(text) if m.start() >= min_position]
    if not matches:
        return text
    return text[: matches[0].start()].rstrip()


def clean_text(pages: list[str]) -> str:
    """Склеивает страницы в связный текст: абзацы разделены пустой строкой."""
    text = "\n\n".join(strip_headers_and_footers(pages))
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("­", "").replace("\x00", "")
    text = re.sub(r"[ \t\f\v ]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = _HYPHEN_BREAK_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # заголовок References ищется до склейки строк: он должен стоять в начале строки
    text = cut_references(text)
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    return re.sub(r" {2,}", " ", text).strip()


def extract_pages(pdf_path: Path) -> list[str]:
    """Текст по страницам; блоки PyMuPDF (абзацы) разделены пустой строкой."""
    try:
        with fitz.open(pdf_path) as document:
            pages: list[str] = []
            for page in document:
                blocks = page.get_text("blocks", sort=True)
                texts = [block[4].strip() for block in blocks if block[6] == 0 and block[4].strip()]
                pages.append("\n\n".join(texts))
            return pages
    except Exception as exc:
        raise TextExtractionError(f"Не удалось прочитать PDF: {exc}") from exc


def extract_text(pdf_path: Path) -> ExtractedText:
    started = time.perf_counter()
    pages = extract_pages(pdf_path)
    raw_chars = sum(len(page) for page in pages)
    text = clean_text(pages)
    log.info(
        "pdf_text_extracted",
        pages=len(pages),
        raw_chars=raw_chars,
        text_chars=len(text),
        duration_ms=round((time.perf_counter() - started) * 1000),
    )
    if len(text) < MIN_TEXT_CHARS:
        raise TextExtractionError(
            f"В PDF почти нет текста ({len(text)} символов на {len(pages)} стр.): "
            "вероятно, это скан без текстового слоя"
        )
    return ExtractedText(text=text, pages=len(pages), raw_chars=raw_chars)
