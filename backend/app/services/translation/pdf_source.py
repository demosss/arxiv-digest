"""Разбор PDF статьи в блоки документа — запасной путь, когда у статьи нет HTML-версии.

В PDF структуры нет, поэтому всё здесь — эвристики по геометрии и шрифтам: колонки,
заголовки, склейка абзацев через границы колонок и страниц, формулы, рисунки и таблицы.
Формула вырезается картинкой, а её текст позже отдаётся LLM для восстановления в LaTeX.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any

import pymupdf

from app.core.logging import get_logger
from app.services.pdf_text import _PAGE_NUMBER_RE, _REFERENCES_RE, _edge_key
from app.services.translation.images import from_pixmap
from app.services.translation.model import (
    SOURCE_PDF,
    Block,
    Document,
    Figure,
    Formula,
    Heading,
    ListItem,
    Paragraph,
    References,
    Table,
    TableCell,
)

log = get_logger(__name__)

# find_tables печатает в stdout совет поставить pymupdf_layout, а в stdout воркера — только JSON
if hasattr(pymupdf, "no_recommend_layout"):
    pymupdf.no_recommend_layout()

RENDER_DPI = 200
CM_PER_POINT = 2.54 / 72
# доля высоты страницы сверху и снизу, где ищутся колонтитулы
EDGE_ZONE = 0.08
MIN_REPEAT_PAGES = 3
MIN_REPEAT_SHARE = 0.4
# заголовки разделов ищутся в начале статьи, чтобы отбросить шапку (название, авторы, аннотация)
FRONT_MATTER_PAGES = 2
MIN_TEXT_CHARS = 500

_MATH_FONT_RE = re.compile(
    r"cmmi|cmsy|cmex|cmbsy|cmmib|msam|msbm|math|symbol|mt-?extra|rsfs|eufm|esint|stmary|"
    r"txsy|txmi|txex|pxmi|pxsy|ntxmi|ntxsy|mtmi|mtsy|lasy|wasy|bbm|bbold|dsrom|euclid",
    re.IGNORECASE,
)
_BOLD_FONT_RE = re.compile(r"bold|black|heavy|cmbx|cmssbx|-bd|demi", re.IGNORECASE)
_ITALIC_FONT_RE = re.compile(r"ital|oblique|cmti|cmsl|-it\b", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]{4,}")
_EQUATION_NUMBER_RE = re.compile(r"\(\s*((?:[A-Z]\.)?\d{1,3}(?:\.\d{1,3})?[a-z]?)\s*\)\s*$")
# номер формулы в начале строки: так он оказывается после сортировки кусков по координатам
_LEADING_NUMBER_RE = re.compile(r"^\(\s*((?:[A-Z]\.)?\d{1,3}(?:\.\d{1,3})?[a-z]?)\s*\)\s+")
_INNER_NUMBER_RE = re.compile(r"(?<=\s)\(\s*(\d{1,3}[a-z]?)\s*\)(?=\s)")
_SENTENCE_START_RE = re.compile(
    r"^(where|with|here|and|the|is|are|which|denotes|when|if|then|for)\b", re.IGNORECASE
)
_CAPTION_RE = re.compile(
    r"^(fig(?:ure)?\.?|table|algorithm|listing)\s*([\dIVXLC]+[a-z]?)\s*[.:|—–-]?\s*", re.IGNORECASE
)
_NUMBERED_HEADING_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+(\S.*)$")
_ROMAN_HEADING_RE = re.compile(r"^([IVX]{1,5})\.\s+(\S.*)$")
_LETTER_HEADING_RE = re.compile(r"^([A-Z])\.\s+([A-Z].*)$")
_KNOWN_HEADING_RE = re.compile(
    r"^(abstract|introduction|related works?|background|preliminaries|methods?|methodology|"
    r"experiments?|evaluation|results|discussion|limitations|conclusions?|future work|"
    r"acknowledge?ments?|references|bibliography|appendix(?:\s+\w+)?|supplementary materials?)$",
    re.IGNORECASE,
)
_ABSTRACT_RE = re.compile(r"^abstract\b", re.IGNORECASE)
_KEYWORDS_RE = re.compile(r"^(key\s?words|index terms)\s*[:—–-]?\s*", re.IGNORECASE)
_BULLET_RE = re.compile(r"^(?:[•▪●◦∙‣⋄\uf0b7\uf09f\uf0a7\uf0d8]\s*|[–*·]\s+)")
_NUMBERED_ITEM_RE = re.compile(r"^(\(?\d{1,2}[.)]|\([a-z]\)|[a-z]\)|\([ivx]+\))\s+")
_REFERENCE_SPLIT_RE = re.compile(r"\n(?=\[\d{1,3}\]|\d{1,3}\.\s)")
_TERMINAL_RE = re.compile(r"[.!?:;][\"'”’»)\]]*$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


class PdfParseError(Exception):
    """PDF не читается или в нём нет текстового слоя."""


@dataclass(slots=True)
class _Line:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    bold: bool
    italic: bool
    # доля символов, набранных математическими шрифтами
    math: float

    @property
    def is_formula(self) -> bool:
        """Строка выключной формулы: много математики и почти нет обычных слов."""
        words = len(_WORD_RE.findall(self.text))
        numbered = bool(_EQUATION_NUMBER_RE.search(self.text))
        if _SENTENCE_START_RE.match(self.text.strip()) and not numbered:
            return False
        if self.math >= 0.5 and words <= 2:
            return True
        return numbered and self.math >= 0.2 and words <= 3


@dataclass(slots=True)
class _TextBlock:
    page: int
    lines: list[_Line]
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    column: int = 0

    def __post_init__(self) -> None:
        self.x0 = min(line.x0 for line in self.lines)
        self.y0 = min(line.y0 for line in self.lines)
        self.x1 = max(line.x1 for line in self.lines)
        self.y1 = max(line.y1 for line in self.lines)

    @property
    def text(self) -> str:
        return _join_lines(self.lines)

    @property
    def size(self) -> float:
        counts: Counter[float] = Counter()
        for line in self.lines:
            counts[round(line.size * 2) / 2] += len(line.text)
        return counts.most_common(1)[0][0]

    @property
    def bold(self) -> bool:
        return all(line.bold for line in self.lines)

    @property
    def italic(self) -> bool:
        return all(line.italic for line in self.lines)

    @property
    def rect(self) -> pymupdf.Rect:
        return pymupdf.Rect(self.x0, self.y0, self.x1, self.y1)


@dataclass(slots=True)
class _Float:
    """Рисунок или таблица на странице: занимает место в потоке чтения, как и текст."""

    page: int
    rect: pymupdf.Rect
    block: Figure | Table
    column: int = 0

    @property
    def y0(self) -> float:
        return float(self.rect.y0)


@dataclass(slots=True)
class _Page:
    index: int
    width: float
    height: float
    blocks: list[_TextBlock]
    floats: list[_Float] = field(default_factory=list)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _CONTROL_RE.sub("", text)).strip()


def _join_lines(lines: list[_Line]) -> str:
    """Строки абзаца одной строкой: переносы слов по дефису убираются."""
    text = ""
    for line in lines:
        part = line.text.strip()
        if not part:
            continue
        if text.endswith("-") and part[0].islower() and len(text) > 1 and text[-2].isalpha():
            text = text[:-1] + part
        else:
            text = f"{text} {part}" if text else part
    return _clean(text)


def _read_line(raw: dict[str, Any]) -> _Line | None:
    direction = raw.get("dir", (1.0, 0.0))
    # повёрнутый текст — штамп arXiv на полях и подписи осей
    if abs(direction[0] - 1.0) > 0.05 or abs(direction[1]) > 0.05:
        return None
    spans = [span for span in raw.get("spans", []) if span.get("text", "").strip()]
    if not spans:
        return None
    text = _CONTROL_RE.sub("", "".join(span["text"] for span in raw["spans"]))
    total = sum(len(span["text"].strip()) for span in spans) or 1
    weight: Counter[float] = Counter()
    bold = italic = math = 0
    for span in spans:
        chars = len(span["text"].strip())
        font = span.get("font", "")
        flags = int(span.get("flags", 0))
        weight[float(span.get("size", 0.0))] += chars
        bold += chars if flags & 16 or _BOLD_FONT_RE.search(font) else 0
        italic += chars if flags & 2 or _ITALIC_FONT_RE.search(font) else 0
        math += chars if _MATH_FONT_RE.search(font) else 0
    x0, y0, x1, y1 = raw["bbox"]
    return _Line(
        text=text,
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        size=weight.most_common(1)[0][0],
        bold=bold / total > 0.6,
        italic=italic / total > 0.6,
        math=math / total,
    )


def _merge_baseline(lines: list[_Line]) -> list[_Line]:
    """Собирает визуальные строки из кусков.

    PyMuPDF режет строку на больших пробелах, а индексы и степени отдаёт отдельными
    строками: кусок относится к строке, если перекрывается с ней по высоте.
    """
    rows: list[list[_Line]] = []
    for line in sorted(lines, key=lambda item: (item.y0, item.x0)):
        for row in reversed(rows[-3:]):
            # сравнение с самым высоким куском строки: иначе индексы сцепили бы соседние строки
            anchor = max(row, key=lambda item: item.y1 - item.y0)
            smaller = min(line.y1 - line.y0, anchor.y1 - anchor.y0)
            if _overlap(line.y0, line.y1, anchor.y0, anchor.y1) >= 0.6 * smaller:
                row.append(line)
                break
        else:
            rows.append([line])

    merged: list[_Line] = []
    for row in rows:
        row.sort(key=lambda item: item.x0)
        anchor = max(row, key=lambda item: len(item.text))
        total = sum(len(item.text) for item in row) or 1
        text = ""
        for index, item in enumerate(row):
            gap = item.x0 - row[index - 1].x1 if index else 0.0
            text += item.text if not index or gap < 0.15 * anchor.size else f" {item.text}"
        merged.append(
            _Line(
                text=text,
                x0=min(item.x0 for item in row),
                y0=min(item.y0 for item in row),
                x1=max(item.x1 for item in row),
                y1=max(item.y1 for item in row),
                size=anchor.size,
                bold=sum(len(item.text) for item in row if item.bold) / total > 0.6,
                italic=sum(len(item.text) for item in row if item.italic) / total > 0.6,
                math=sum(item.math * len(item.text) for item in row) / total,
            )
        )
    return merged


def _read_blocks(page: pymupdf.Page, index: int) -> list[_TextBlock]:
    flags = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP
    blocks: list[_TextBlock] = []
    for raw in page.get_text("dict", flags=flags)["blocks"]:
        if raw.get("type") != 0:
            continue
        lines = [line for line in map(_read_line, raw.get("lines", [])) if line is not None]
        if lines:
            blocks.append(_TextBlock(page=index, lines=_merge_baseline(lines)))
    return blocks


def _strip_edges(pages: list[_Page]) -> None:
    """Убирает номера страниц и повторяющиеся колонтитулы у краёв страниц."""

    def at_edge(page: _Page, block: _TextBlock) -> bool:
        return block.y1 < page.height * EDGE_ZONE or block.y0 > page.height * (1 - EDGE_ZONE)

    counts: Counter[str] = Counter()
    for page in pages:
        counts.update({_edge_key(b.text) for b in page.blocks if at_edge(page, b)})
    threshold = max(MIN_REPEAT_PAGES, int(len(pages) * MIN_REPEAT_SHARE))
    for page in pages:
        page.blocks = [
            block
            for block in page.blocks
            if not (
                at_edge(page, block)
                and (
                    _PAGE_NUMBER_RE.match(block.text) or counts[_edge_key(block.text)] >= threshold
                )
            )
        ]


def _body_size(pages: list[_Page]) -> float:
    counts: Counter[float] = Counter()
    for page in pages:
        for block in page.blocks:
            for line in block.lines:
                counts[round(line.size * 2) / 2] += len(line.text)
    return counts.most_common(1)[0][0] if counts else 10.0


class _Layout:
    """Границы полосы набора и колонки: общие для документа."""

    def __init__(self, pages: list[_Page], body_size: float) -> None:
        lefts, rights = [], []
        for page in pages:
            body = [b for b in page.blocks if abs(b.size - body_size) <= 0.6 and len(b.text) > 80]
            if body:
                lefts.append(min(b.x0 for b in body))
                rights.append(max(b.x1 for b in body))
        self.left = median(lefts) if lefts else 50.0
        self.right = median(rights) if rights else 545.0
        self.width = max(self.right - self.left, 1.0)
        self.middle = (self.left + self.right) / 2
        self._tolerance = 0.03 * self.width
        body_lines = [
            line
            for page in pages
            for block in page.blocks
            for line in block.lines
            if abs(line.size - body_size) <= 0.6 and len(line.text) > 30
        ]
        right_lines = [line for line in body_lines if line.x0 > self.middle - self._tolerance]
        self.two_column = bool(body_lines) and len(right_lines) / len(body_lines) >= 0.25

    def column(self, x0: float, x1: float) -> int:
        """0 — левая (или единственная) колонка, 1 — правая, -1 — на всю ширину."""
        if not self.two_column:
            return 0
        if x1 <= self.middle + self._tolerance:
            return 0
        if x0 >= self.middle - self._tolerance:
            return 1
        return -1

    def column_left(self, column: int) -> float:
        return self.middle if column == 1 else self.left


def _grow(rect: pymupdf.Rect, pad: float) -> pymupdf.Rect:
    return pymupdf.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _caption(block: _TextBlock) -> tuple[str, str, str] | None:
    """(вид: figure / table / other, метка, текст подписи) для блока-подписи."""
    text = block.text
    match = _CAPTION_RE.match(text)
    if not match:
        return None
    word = match.group(1).lower()
    kind = "figure" if word.startswith("fig") else "table" if word == "table" else "other"
    label = f"{'Figure' if kind == 'figure' else match.group(1).capitalize()} {match.group(2)}"
    return kind, label, text[match.end() :].strip()


def _graphics(page: pymupdf.Page) -> tuple[list[pymupdf.Rect], list[pymupdf.Rect]]:
    """Области с графикой (картинки и векторные рисунки) и горизонтальные линейки таблиц."""
    regions: list[pymupdf.Rect] = []
    rules: list[pymupdf.Rect] = []
    for info in page.get_image_info():
        rect = pymupdf.Rect(info["bbox"]) & page.rect
        if rect.width >= 20 and rect.height >= 20:
            regions.append(rect)
    try:
        drawings = page.get_drawings()
        clusters = page.cluster_drawings(drawings=drawings)
    except Exception as exc:
        log.debug("pdf_cluster_drawings_failed", page=page.number, error=str(exc))
        drawings, clusters = [], []
    for drawing in drawings:
        rect = pymupdf.Rect(drawing["rect"]) & page.rect
        if rect.height < 3 and rect.width > 60:
            rules.append(rect)
    for cluster in clusters:
        rect = pymupdf.Rect(cluster) & page.rect
        if rect.width >= 25 and rect.height >= 25:
            regions.append(rect)

    merged: list[pymupdf.Rect] = []
    for rect in sorted(regions, key=lambda r: (r.y0, r.x0)):
        for other in merged:
            if _grow(other, 8).intersects(rect):
                other |= rect
                break
        else:
            merged.append(pymupdf.Rect(rect))
    return merged, rules


def _render(page: pymupdf.Page, rect: pymupdf.Rect, pad: float = 3.0):  # type: ignore[no-untyped-def]
    clip = _grow(rect, pad) & page.rect
    image = from_pixmap(page.get_pixmap(clip=clip, dpi=RENDER_DPI, alpha=False))
    image.width_cm = clip.width * CM_PER_POINT
    return image


def _table_rows(raw: list[list[str | None]]) -> list[list[TableCell]]:
    rows = [[_clean(cell or "") for cell in row] for row in raw]
    rows = [row for row in rows if any(row)]
    if not rows:
        return []
    width = max(len(row) for row in rows)
    keep = [i for i in range(width) if any(i < len(row) and row[i] for row in rows)]
    return [
        [TableCell(text=row[i] if i < len(row) else "", header=index == 0) for i in keep]
        for index, row in enumerate(rows)
    ]


def _find_tables(
    page: pymupdf.Page, **options: Any
) -> list[tuple[pymupdf.Rect, list[list[TableCell]]]]:
    found = []
    try:
        tables = page.find_tables(**options).tables
    except Exception as exc:
        log.debug("pdf_find_tables_failed", page=page.number, error=str(exc))
        return []
    for table in tables:
        rows = _table_rows(table.extract())
        cells = [cell for row in rows for cell in row]
        if len(rows) < 2 or not cells or len(rows[0]) < 2:
            continue
        filled = sum(bool(cell.text) for cell in cells) / len(cells)
        longest = max(len(cell.text) for cell in cells)
        # сплошной текст, принятый за таблицу, выдаёт себя длинными «ячейками»
        if filled >= 0.4 and longest <= 400:
            found.append((pymupdf.Rect(table.bbox), rows))
    return found


def _text_table(page: pymupdf.Page, rect: pymupdf.Rect) -> list[list[TableCell]]:
    """Таблица без вертикальных линий: границы колонок — промежутки, пустые почти во всех строках.

    Шапка, объединяющая несколько колонок, перекрывает промежуток только в своей строке,
    поэтому промежутку достаточно быть пустым в 80% строк.
    """
    words = page.get_text("words", clip=rect)
    rows: list[tuple[float, list[Any]]] = []
    for word in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        center = (word[1] + word[3]) / 2
        if rows and abs(center - rows[-1][0]) < 0.6 * (word[3] - word[1]):
            rows[-1][1].append(word)
        else:
            rows.append((center, [word]))
    if len(rows) < 2:
        return []

    origin, width = int(rect.x0), int(rect.width) + 2
    free = [0] * width
    for _, row in rows:
        occupied = [False] * width
        for word in row:
            for x in range(max(int(word[0]) - origin, 0), min(int(word[2]) - origin + 1, width)):
                occupied[x] = True
        for x in range(width):
            free[x] += not occupied[x]
    needed = len(rows) if len(rows) <= 4 else round(0.8 * len(rows))
    heights = sorted(word[3] - word[1] for word in words)
    min_gap = max(4.0, 0.6 * heights[len(heights) // 2])

    boundaries: list[float] = []
    start: int | None = None
    for x in range(width + 1):
        empty = x < width and free[x] >= needed
        if empty and start is None:
            start = x
        elif not empty and start is not None:
            # промежутки у краёв таблицы — поля, а не границы колонок
            if start > 0 and x < width and x - start >= min_gap:
                boundaries.append(origin + (start + x) / 2)
            start = None
    if not boundaries:
        return []

    table: list[list[TableCell]] = []
    for index, (_, row) in enumerate(rows):
        cells = [""] * (len(boundaries) + 1)
        for word in sorted(row, key=lambda w: w[0]):
            column = sum((word[0] + word[2]) / 2 > boundary for boundary in boundaries)
            cells[column] = f"{cells[column]} {word[4]}".strip()
        table.append([TableCell(text=_clean(text), header=index == 0) for text in cells])
    return table


class _FloatFinder:
    """Находит на странице таблицы и рисунки и забирает относящийся к ним текст."""

    def __init__(self, pdf_page: pymupdf.Page, page: _Page, layout: _Layout, body: float) -> None:
        self._pdf_page = pdf_page
        self._page = page
        self._layout = layout
        self._body = body
        self._regions, self._rules = _graphics(pdf_page)
        self._used: set[int] = set()

    def run(self) -> None:
        captions = [
            (index, block, caption)
            for index, block in enumerate(self._page.blocks)
            if (caption := _caption(block)) is not None
        ]
        tables = _find_tables(self._pdf_page)
        for rect, rows in tables:
            self._add_table(rect, rows, captions)
        for index, block, (kind, label, text) in captions:
            if index in self._used:
                continue
            if kind == "table":
                self._table_by_rules(index, block, label, text)
            elif kind == "figure":
                self._figure(index, block, label, text)
        self._orphan_figures()
        self._page.blocks = [
            block for index, block in enumerate(self._page.blocks) if index not in self._used
        ]

    def _consume(self, rect: pymupdf.Rect, pad: float = 2.0) -> None:
        area = _grow(rect, pad)
        for index, block in enumerate(self._page.blocks):
            center = pymupdf.Point((block.x0 + block.x1) / 2, (block.y0 + block.y1) / 2)
            if index not in self._used and area.contains(center):
                self._used.add(index)

    def _add(self, rect: pymupdf.Rect, block: Figure | Table) -> None:
        column = self._layout.column(rect.x0, rect.x1)
        self._page.floats.append(_Float(self._page.index, pymupdf.Rect(rect), block, column))

    def _nearest_caption(
        self, rect: pymupdf.Rect, captions: list[tuple[int, _TextBlock, tuple[str, str, str]]]
    ) -> tuple[int, _TextBlock, tuple[str, str, str]] | None:
        best = None
        best_gap = 60.0
        for index, block, caption in captions:
            if index in self._used or caption[0] != "table":
                continue
            if _overlap(rect.x0, rect.x1, block.x0, block.x1) <= 0:
                continue
            gap = min(abs(rect.y0 - block.y1), abs(block.y0 - rect.y1))
            if gap < best_gap:
                best, best_gap = (index, block, caption), gap
        return best

    def _add_table(
        self,
        rect: pymupdf.Rect,
        rows: list[list[TableCell]],
        captions: list[tuple[int, _TextBlock, tuple[str, str, str]]],
    ) -> None:
        table = Table(rows=rows)
        nearest = self._nearest_caption(rect, captions)
        if nearest is not None:
            index, block, (_, label, text) = nearest
            table.label, table.caption = label, self._caption_text(index, block, text)
            self._used.add(index)
        self._consume(rect)
        self._regions = [r for r in self._regions if not _grow(rect, 5).intersects(r)]
        self._add(rect, table)

    def _caption_text(self, index: int, block: _TextBlock, text: str) -> str:
        """Подпись «TABLE I» с названием в следующем блоке (стиль IEEE) дополняется им."""
        if text:
            return text
        for other_index, other in enumerate(self._page.blocks):
            if other_index in self._used or other_index == index:
                continue
            below = 0 <= other.y0 - block.y1 < 1.5 * self._body
            aligned = _overlap(block.x0, block.x1, other.x0, other.x1) > 0
            if below and aligned and len(other.text) < 200:
                self._used.add(other_index)
                return other.text
        return ""

    def _table_by_rules(self, index: int, block: _TextBlock, label: str, text: str) -> None:
        """Таблица без вертикальных линий: её границы задают линейки под подписью."""
        below = sorted((r for r in self._rules if r.y0 > block.y0 - 4), key=lambda r: r.y0)
        # ниже следующей подписи начинается уже другая таблица или рисунок
        limit = min(
            (
                other.y0
                for other in self._page.blocks
                if other.y0 > block.y1 + 2 and _caption(other) is not None
            ),
            default=self._page.height,
        )
        group: list[pymupdf.Rect] = []
        left = right = 0.0
        for rule in below:
            if rule.y0 >= limit:
                break
            if not group:
                if rule.y0 - block.y1 > 4 * self._body:
                    break
                # верхняя линейка (или её части у составной таблицы) задаёт ширину таблицы
                top = [r for r in below if abs(r.y0 - rule.y0) < 2]
                left, right = min(r.x0 for r in top), max(r.x1 for r in top)
                group.append(rule)
            elif rule.x0 >= left - 5 and rule.x1 <= right + 5:
                if rule.y0 - group[-1].y1 > 0.4 * self._page.height:
                    break
                group.append(rule)
        if len(group) < 2:
            return
        rect = pymupdf.Rect(left, group[0].y0, right, group[-1].y1)
        caption = self._caption_text(index, block, text)
        self._used.add(index)
        rows = _text_table(self._pdf_page, rect)
        self._consume(rect)
        if rows:
            self._add(rect, Table(rows=rows, caption=caption, label=label))
        else:
            # колонки не распознаны — таблица остаётся картинкой, зато без искажений
            image = _render(self._pdf_page, rect)
            self._add(rect, Figure(images=[image], caption=caption, label=label))

    def _figure(self, index: int, block: _TextBlock, label: str, text: str) -> None:
        candidates = [
            region
            for region in self._regions
            if _overlap(region.x0, region.x1, block.x0 - 30, block.x1 + 30) > 0.3 * region.width
        ]
        above = sorted((r for r in candidates if r.y1 <= block.y0 + 5), key=lambda r: -r.y1)
        below = sorted((r for r in candidates if r.y0 >= block.y1 - 5), key=lambda r: r.y0)
        chosen: list[pymupdf.Rect] = []
        edge = block.y0
        for region in above:
            if edge - region.y1 > 45:
                break
            chosen.append(region)
            edge = min(edge, region.y0)
        if not chosen:
            edge = block.y1
            for region in below:
                if region.y0 - edge > 45:
                    break
                chosen.append(region)
                edge = max(edge, region.y1)
        if not chosen:
            return
        rect = pymupdf.Rect(chosen[0])
        for region in chosen[1:]:
            rect |= region
        # подписи осей и легенды рядом с рисунком — мелкий или короткий текст — входят в него
        for other_index, other in enumerate(self._page.blocks):
            if other_index in self._used or other_index == index:
                continue
            near = _grow(rect, 25).contains(other.rect)
            if near and (other.size < self._body - 0.4 or len(other.text) < 40):
                rect |= other.rect
        self._used.add(index)
        self._consume(rect)
        self._regions = [r for r in self._regions if not rect.intersects(r)]
        self._add(rect, Figure(images=[_render(self._pdf_page, rect)], caption=text, label=label))

    def _orphan_figures(self) -> None:
        """Крупная графика без подписи тоже попадает в документ."""
        page_area = self._page.width * self._page.height
        for region in self._regions:
            if region.width * region.height >= 0.04 * page_area:
                self._consume(region)
                self._add(region, Figure(images=[_render(self._pdf_page, region)]))


def _ordered(page: _Page, layout: _Layout) -> list[_TextBlock | _Float]:
    """Порядок чтения: полноширинный текст делит страницу на полосы, в полосе — по колонкам."""
    for block in page.blocks:
        block.column = layout.column(block.x0, block.x1)
    items: list[_TextBlock | _Float] = sorted([*page.blocks, *page.floats], key=lambda i: i.y0)
    if not layout.two_column:
        return items
    result: list[_TextBlock | _Float] = []
    band: list[_TextBlock | _Float] = []

    def flush() -> None:
        # рисунок на всю ширину колонки не разрывает: он идёт вместе с левой
        result.extend(item for item in band if item.column != 1)
        result.extend(item for item in band if item.column == 1)
        band.clear()

    for item in items:
        if isinstance(item, _TextBlock) and item.column == -1:
            flush()
            result.append(item)
        else:
            band.append(item)
    flush()
    return result


def _heading(block: _TextBlock, body: float) -> Heading | None:
    text = block.text
    if len(text) > 150 or len(block.lines) > 3 or len(text) < 3:
        return None
    larger = block.size >= body + 0.7
    upper = text.upper() == text and any(ch.isalpha() for ch in text)
    emphasized = block.bold or larger or upper

    match = _NUMBERED_HEADING_RE.match(text)
    subsection = (
        match is not None and "." in match.group(1) and len(block.lines) == 1 and len(text) < 90
    )
    if (
        match
        and (emphasized or block.italic or subsection)
        and match.group(2)[0].isupper()
        and not text.endswith((".", ",", ";", ":"))
    ):
        return Heading(
            level=min(3, match.group(1).count(".") + 1), text=match.group(2), number=match.group(1)
        )
    match = _ROMAN_HEADING_RE.match(text)
    if match and (upper or emphasized) and len(text) < 100:
        return Heading(level=1, text=match.group(2), number=match.group(1))
    match = _LETTER_HEADING_RE.match(text)
    if match and (block.italic or block.bold) and len(block.lines) == 1 and len(text) < 80:
        return Heading(level=2, text=match.group(2), number=match.group(1))
    if _KNOWN_HEADING_RE.match(text) and emphasized:
        return Heading(level=1, text=text)
    if block.bold and len(text) < 100 and len(block.lines) <= 2 and not text.endswith((".", ",")):
        if larger:
            return Heading(level=1 if block.size >= body + 2.5 else 2, text=text)
        if len(block.lines) == 1 and len(text) < 80 and text[0].isupper():
            return Heading(level=3, text=text)
    return None


def _split_references(blocks: list[_TextBlock]) -> list[str]:
    lines = [line for block in blocks for line in block.lines]
    if not lines:
        return []
    text = "\n".join(line.text.strip() for line in lines)
    pieces = _REFERENCE_SPLIT_RE.split(text)
    if len(pieces) < 2 or not re.match(r"\[\d{1,3}\]|\d{1,3}\.\s", pieces[0]):
        # записи без номеров: новая начинается со строки без втяжки
        left = min(line.x0 for line in lines)
        pieces, current = [], ""
        for line in lines:
            if current and line.x0 - left < 3 and any(l.x0 - left >= 3 for l in lines):  # noqa: E741
                pieces.append(current)
                current = ""
            current = f"{current}\n{line.text.strip()}" if current else line.text.strip()
        pieces.append(current)
    items = []
    for piece in pieces:
        joined = ""
        for part in piece.split("\n"):
            if joined.endswith("-") and part[:1].islower():
                joined = joined[:-1] + part
            else:
                joined = f"{joined} {part}" if joined else part
        joined = _clean(re.sub(r"^(\d{1,3})\.\s", r"[\1] ", joined))
        if len(joined) > 10:
            items.append(joined)
    return items


class _Assembler:
    """Собирает поток блоков страницы в документ: абзацы, формулы, списки, литература."""

    def __init__(self, pdf: pymupdf.Document, layout: _Layout, body: float) -> None:
        self._pdf = pdf
        self._layout = layout
        self._body = body
        self.blocks: list[Block] = []
        self.keywords = ""
        self._references: list[_TextBlock] | None = None
        self._references_title = "References"
        # последний абзац основного текста: (индекс в blocks, страница, колонка)
        self._last: tuple[int, int, int] | None = None
        # последняя формула: (индекс в blocks, страница, колонка, область на странице, строки)
        self._last_formula: tuple[int, int, int, pymupdf.Rect, list[_Line]] | None = None

    def add_float(self, item: _Float) -> None:
        self._flush_references()
        self.blocks.append(item.block)

    def add_text(self, block: _TextBlock) -> None:
        while len(block.lines) > 1 and self._references is None:
            # «2.1. Название» первой строкой блока: короткая строка с номером подраздела
            line = block.lines[0]
            match = _NUMBERED_HEADING_RE.match(line.text.strip())
            short = line.x1 < block.x1 - 0.15 * (block.x1 - block.x0)
            if not (
                match
                and "." in match.group(1)
                and short
                and len(line.text) < 90
                and match.group(2)[0].isupper()
                and not line.text.rstrip().endswith((".", ",", ";", ":"))
            ):
                break
            self._flush_references()
            self._last = None
            self.blocks.append(
                Heading(
                    level=min(3, match.group(1).count(".") + 1),
                    text=_clean(match.group(2)),
                    number=match.group(1),
                )
            )
            block = _TextBlock(page=block.page, lines=block.lines[1:], column=block.column)
        heading = _heading(block, self._body)
        if heading is not None:
            self._flush_references()
            self._last = None
            if _REFERENCES_RE.match(f"{heading.number} {heading.text}".strip()) or (
                _REFERENCES_RE.match(heading.text)
            ):
                self._references = []
                self._references_title = heading.text
            else:
                self.blocks.append(heading)
            return
        if self._references is not None:
            self._references.append(block)
            return
        text = block.text
        if _KEYWORDS_RE.match(text) and not self.keywords:
            self.keywords = _KEYWORDS_RE.sub("", text).rstrip(".")
            return
        if self._is_footnote(block) and not all(line.is_formula for line in block.lines):
            self.blocks.append(Paragraph(text=text, footnote=True))
            return
        self._body_block(block)

    def finish(self) -> None:
        self._flush_references()

    def _is_footnote(self, block: _TextBlock) -> bool:
        """Мелкий текст в нижней части страницы: сноски и сведения об авторах."""
        page_height = self._pdf[block.page].rect.height
        return block.size <= self._body - 1.0 and block.y0 > 0.6 * page_height

    def _flush_references(self) -> None:
        if self._references is None:
            return
        items = _split_references(self._references)
        if items:
            self.blocks.append(References(title=self._references_title, items=items))
        self._references = None

    def _body_block(self, block: _TextBlock) -> None:
        """Делит блок на абзацы и формулы; первый абзац может продолжать предыдущий."""
        left = self._layout.column_left(block.column) if block.column >= 0 else block.x0
        left = min(left, block.x0) if abs(left - block.x0) < 30 else block.x0
        indent = 0.6 * self._body
        group: list[_Line] = []
        formula: list[_Line] = []
        first = True

        def flush_text() -> None:
            nonlocal first
            if group:
                self._paragraph(block, group, continuation=first)
                first = False
                group.clear()

        def flush_formula() -> None:
            nonlocal first
            if formula:
                self._formula(block, formula)
                first = False
                formula.clear()

        for index, line in enumerate(block.lines):
            if line.is_formula:
                flush_text()
                formula.append(line)
                continue
            flush_formula()
            indented = indent < line.x0 - left < 4 * self._body
            previous = block.lines[index - 1] if index else None
            if group and _BULLET_RE.match(line.text.strip()):
                flush_text()
            elif group and _BULLET_RE.match(group[0].text.strip()):
                # строки пункта списка идут с втяжкой: красной строкой она не считается
                pass
            elif group and indented and previous is not None and previous.x0 - left <= indent:
                flush_text()
            group.append(line)
        flush_text()
        flush_formula()

    def _formula(self, block: _TextBlock, lines: list[_Line]) -> None:
        """Выключная формула: вырезка из страницы и её текст для восстановления в LaTeX.

        Числитель, знаменатель и номер PyMuPDF отдаёт разными блоками, поэтому формула,
        идущая сразу за предыдущей в той же колонке, считается её продолжением.
        """
        rect = pymupdf.Rect(
            min(line.x0 for line in lines),
            min(line.y0 for line in lines),
            max(line.x1 for line in lines),
            max(line.y1 for line in lines),
        )
        index = None
        last = self._last_formula
        if (
            last is not None
            and last[0] == len(self.blocks) - 1
            and last[1:3] == (block.page, block.column)
            and rect.y0 - last[3].y1 < 1.6 * self._body
        ):
            index = last[0]
            rect |= last[3]
            lines = [*last[4], *lines]
        page = self._pdf[block.page]
        lines = sorted(lines, key=lambda line: (line.y0, line.x0))
        text = _clean(" ".join(line.text for line in lines))
        number = ""
        for pattern in (_EQUATION_NUMBER_RE, _LEADING_NUMBER_RE, _INNER_NUMBER_RE):
            matches = list(pattern.finditer(text))
            if matches:
                match = matches[-1]
                number = f"({match.group(1)})"
                text = _clean(text[: match.start()] + " " + text[match.end() :])
                break
        formula = Formula(
            math=None, number=number, image=_render(page, rect, pad=4.0), source_text=text
        )
        if index is None:
            self.blocks.append(formula)
            index = len(self.blocks) - 1
        else:
            self.blocks[index] = formula
        self._last_formula = (index, block.page, block.column, rect, lines)
        self._last = None

    def _paragraph(self, block: _TextBlock, lines: list[_Line], *, continuation: bool) -> None:
        text = _join_lines(lines)
        if not text:
            return
        bullet = _BULLET_RE.match(text)
        numbered = _NUMBERED_ITEM_RE.match(text)
        if bullet:
            self.blocks.append(ListItem(text=text[bullet.end() :]))
            self._last = None
            return
        if numbered:
            self.blocks.append(ListItem(text=text[numbered.end() :], marker=numbered.group(1)))
            self._last = None
            return
        previous_block = self.blocks[-1] if self.blocks else None
        if (
            isinstance(previous_block, Paragraph)
            and len(previous_block.text) == 1
            and previous_block.text.isupper()
            and text[0].islower()
        ):
            # буквица: первая буква статьи набрана отдельным блоком
            previous_block.text += text
            self._last = (len(self.blocks) - 1, block.page, block.column)
            return
        if (
            isinstance(previous_block, ListItem)
            and text[0].islower()
            and not _TERMINAL_RE.search(previous_block.text)
        ):
            previous_block.text = f"{previous_block.text} {text}"
            return
        if continuation and self._continues(block, text):
            assert self._last is not None
            previous = self.blocks[self._last[0]]
            assert isinstance(previous, Paragraph)
            if previous.text.endswith("-") and text[0].islower():
                previous.text = previous.text[:-1] + text
            else:
                previous.text = f"{previous.text} {text}"
            self._last = (self._last[0], block.page, block.column)
            return
        self.blocks.append(Paragraph(text=text))
        self._last = (len(self.blocks) - 1, block.page, block.column)

    def _continues(self, block: _TextBlock, text: str) -> bool:
        """Абзац оборван границей колонки или страницы и продолжается этим блоком."""
        if self._last is None:
            return False
        index, page, column = self._last
        if (page, column) == (block.page, block.column):
            return False
        # между частями абзаца могут стоять только рисунки, таблицы и сноски
        for between in self.blocks[index + 1 :]:
            if not (
                isinstance(between, Figure | Table)
                or (isinstance(between, Paragraph) and between.footnote)
            ):
                return False
        previous = self.blocks[index]
        assert isinstance(previous, Paragraph)
        return not _TERMINAL_RE.search(previous.text)


def _skip_front_matter(
    items: list[_TextBlock | _Float], body: float, title: str, abstract: str
) -> list[_TextBlock | _Float]:
    """Отбрасывает шапку статьи: название, авторы и аннотация берутся из метаданных arXiv."""

    def norm(value: str) -> str:
        return re.sub(r"[^a-zа-яё0-9]+", "", value.lower())

    start = None
    for index, item in enumerate(items):
        if isinstance(item, _Float):
            continue
        if item.page >= FRONT_MATTER_PAGES:
            break
        heading = _heading(item, body)
        if heading is not None and not _ABSTRACT_RE.match(heading.text):
            # «заголовком» может оказаться и само название статьи, набранное крупно
            if norm(heading.text) and norm(heading.text) in norm(title):
                continue
            start = index
            break
    if start is None:
        # разделов в начале нет: убираем только то, что совпадает с названием и аннотацией
        known = norm(title) + norm(abstract)
        tail = norm(abstract)[-60:]
        cut = 0
        for index, item in enumerate(items):
            if isinstance(item, _TextBlock) and item.page == 0 and tail and tail in norm(item.text):
                cut = index + 1
        return [
            item
            for index, item in enumerate(items)
            if isinstance(item, _Float)
            or (index >= cut and not (len(norm(item.text)) > 15 and norm(item.text) in known))
        ]
    head = [
        item for item in items[:start] if isinstance(item, _Float) or _KEYWORDS_RE.match(item.text)
    ]
    return head + items[start:]


def parse_pdf(path: Path, *, title: str, authors: list[str], abstract: str) -> Document:
    """PDF статьи → документ. Название, авторы и аннотация приходят из метаданных arXiv."""
    try:
        pdf = pymupdf.open(path)
    except Exception as exc:
        raise PdfParseError(f"Не удалось открыть PDF: {exc}") from exc
    with pdf:
        pages = [
            _Page(
                index,
                pdf[index].rect.width,
                pdf[index].rect.height,
                _read_blocks(pdf[index], index),
            )
            for index in range(pdf.page_count)
        ]
        _strip_edges(pages)
        body = _body_size(pages)
        layout = _Layout(pages, body)
        items: list[_TextBlock | _Float] = []
        for page in pages:
            _FloatFinder(pdf[page.index], page, layout, body).run()
            items.extend(_ordered(page, layout))
        items = _skip_front_matter(items, body, title, abstract)

        assembler = _Assembler(pdf, layout, body)
        for item in items:
            if isinstance(item, _Float):
                assembler.add_float(item)
            else:
                assembler.add_text(item)
        assembler.finish()

    document = Document(
        title=title,
        authors=authors,
        abstract=[abstract] if abstract.strip() else [],
        blocks=assembler.blocks,
        source=SOURCE_PDF,
        keywords=assembler.keywords,
    )
    chars = document.text_chars()
    if chars < MIN_TEXT_CHARS:
        raise PdfParseError(
            f"В PDF почти нет текста ({chars} символов на {len(pages)} стр.): "
            "вероятно, это скан без текстового слоя"
        )
    log.info(
        "translation_pdf_parsed",
        pages=len(pages),
        two_column=layout.two_column,
        body_size=body,
        blocks=len(document.blocks),
        text_chars=chars,
    )
    return document
