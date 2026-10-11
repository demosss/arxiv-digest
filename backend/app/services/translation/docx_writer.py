"""Сборка docx перевода по образцу: текст на всю ширину страницы, формулы и рисунки —
в таблицах-контейнерах без рамок, таблицы — с рамкой и залитой шапкой.

Размеры взяты из файла-образца: A4, поля 2 / 1,5 / 2 / 2,5 см, шрифт Arial 11,
абзацный отступ 284 twips, контейнер формулы — колонки 8831 + 808 twips.
"""

import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from docx import Document as new_document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor, Twips

from app.core.logging import get_logger
from app.services.translation.languages import document_labels
from app.services.translation.model import (
    MATH_TOKEN_RE,
    Block,
    Document,
    Figure,
    Formula,
    Heading,
    Image,
    ListItem,
    Math,
    Paragraph,
    References,
    Table,
)
from app.services.translation.omml import MathConversionError, latex_to_mathml, mathml_to_omml

log = get_logger(__name__)

FONT = "Arial"
MONO_FONT = "Courier New"
HEADING_COLOR = RGBColor(0x1F, 0x38, 0x64)
HEADER_FILL = "D9E2F3"
PAGE_WIDTH, PAGE_HEIGHT = 11906, 16838
MARGIN_TOP, MARGIN_RIGHT, MARGIN_BOTTOM, MARGIN_LEFT = 1134, 850, 1134, 1417
TEXT_WIDTH = PAGE_WIDTH - MARGIN_LEFT - MARGIN_RIGHT
FIRST_LINE_INDENT = 284
FORMULA_COLUMNS = (8831, 808)
TABLE_WIDTH = 9355
MIN_COLUMN_WIDTH = 500
# рисунок вписывается в ячейку контейнера и в высоту страницы
MAX_IMAGE_WIDTH_CM = 16.0
MAX_IMAGE_HEIGHT_CM = 21.0
MAX_FORMULA_IMAGE_WIDTH_CM = 13.5
# картинка без известного размера печатается из расчёта такого разрешения
DEFAULT_IMAGE_DPI = 150

_INVALID_XML_RE = re.compile(r"[^\x09\x0A\x0D\x20-퟿-�\U00010000-\U0010FFFF]")
_LABEL_RE = re.compile(r"^([A-Za-z]+)\.?\s*(.*)$")


@dataclass(frozen=True, slots=True)
class DocxMeta:
    arxiv_id: str
    version: int
    abs_url: str
    published_at: datetime | None = None
    categories: tuple[str, ...] = ()


@dataclass
class DocxStats:
    paragraphs: int = 0
    headings: int = 0
    figures: int = 0
    tables: int = 0
    formulas: int = 0
    # формулы, набранные для редактора формул, и оставшиеся только картинкой или текстом
    formulas_editable: int = 0
    inline_formulas: int = 0
    references: int = 0
    docx_size_bytes: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = dict(vars(self))
        data.pop("notes")
        return data


def _xml_text(text: str) -> str:
    return _INVALID_XML_RE.sub("", text)


def _set_fonts(style: Any, name: str) -> None:
    """Шрифт стиля; атрибуты темы убираются, иначе Word берёт шрифт из темы."""
    fonts = style.element.get_or_add_rPr().get_or_add_rFonts()
    for attribute in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
        fonts.attrib.pop(qn(f"w:{attribute}"), None)
    for attribute in ("ascii", "hAnsi", "cs", "eastAsia"):
        fonts.set(qn(f"w:{attribute}"), name)


def _no_borders(table: Any) -> None:
    borders = OxmlElement("w:tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        border = OxmlElement(f"w:{side}")
        border.set(qn("w:val"), "none")
        border.set(qn("w:sz"), "0")
        border.set(qn("w:space"), "0")
        border.set(qn("w:color"), "auto")
        borders.append(border)
    table._tbl.tblPr.append(borders)


def _table_width(table: Any, width: int) -> None:
    properties = table._tbl.tblPr
    element = properties.find(qn("w:tblW"))
    if element is None:
        element = OxmlElement("w:tblW")
        properties.append(element)
    element.set(qn("w:w"), str(width))
    element.set(qn("w:type"), "dxa")


def _shade(cell: Any, fill: str) -> None:
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), fill)
    cell._tc.get_or_add_tcPr().append(shading)


def _localized_label(label: str, figure_word: str, table_word: str) -> str:
    """«Figure 1» → «Рис. 1»: слово заменяется, номер остаётся; незнакомая метка не меняется."""
    match = _LABEL_RE.match(label.strip())
    if not match:
        return label.strip()
    word, number = match.group(1).lower(), match.group(2).strip()
    if word.startswith("fig"):
        return f"{figure_word} {number}".strip()
    if word.startswith("tab"):
        return f"{table_word} {number}".strip()
    return label.strip()


def _column_widths(grid: dict[tuple[int, int], Any], columns: int, size: int) -> list[int]:
    """Ширины колонок: не уже самого длинного слова, остаток — пропорционально длине текста."""
    # средняя ширина знака Arial — около половины кегля; поля ячейки — 108 twips с каждой стороны
    char_width = size * 11
    lengths = [4] * columns
    minimums = [MIN_COLUMN_WIDTH] * columns
    for (_, c), cell in grid.items():
        if cell.colspan != 1:
            continue
        text = MATH_TOKEN_RE.sub("xxx", cell.text)
        lengths[c] = max(lengths[c], min(len(text), 60))
        longest = max((len(word) for word in text.split()), default=0)
        minimums[c] = max(minimums[c], min(longest, 24) * char_width + 230)
    if sum(minimums) >= TABLE_WIDTH:
        return [round(TABLE_WIDTH * width / sum(minimums)) for width in minimums]
    spare = TABLE_WIDTH - sum(minimums)
    return [
        minimum + round(spare * length / sum(lengths))
        for minimum, length in zip(minimums, lengths, strict=True)
    ]


class _Writer:
    def __init__(self, document: Document, language: str, meta: DocxMeta) -> None:
        self._source = document
        self._labels = document_labels(language)
        self._meta = meta
        self._doc = new_document()
        self._after_table = False
        self.stats = DocxStats()
        self._setup()

    # --- оформление ---

    def _setup(self) -> None:
        section = self._doc.sections[0]
        section.page_width, section.page_height = Twips(PAGE_WIDTH), Twips(PAGE_HEIGHT)
        section.top_margin, section.bottom_margin = Twips(MARGIN_TOP), Twips(MARGIN_BOTTOM)
        section.left_margin, section.right_margin = Twips(MARGIN_LEFT), Twips(MARGIN_RIGHT)

        styles = self._doc.styles
        normal = styles["Normal"]
        _set_fonts(normal, FONT)
        normal.font.size = Pt(11)
        normal.paragraph_format.space_after = Pt(10)
        normal.paragraph_format.line_spacing = 1.15

        title = styles["Title"]
        _set_fonts(title, FONT)
        title.font.size = Pt(16)
        title.font.bold = True
        title.font.color.rgb = HEADING_COLOR
        for name, size in (("Heading 1", 14), ("Heading 2", 13), ("Heading 3", 12)):
            style = styles[name]
            _set_fonts(style, FONT)
            style.font.size = Pt(size)
            style.font.bold = True
            style.font.italic = False
            style.font.color.rgb = HEADING_COLOR
            style.paragraph_format.space_before = Pt(12)
            style.paragraph_format.keep_with_next = True
        _set_fonts(styles["List Bullet"], FONT)

        properties = self._doc.core_properties
        properties.title = _xml_text(MATH_TOKEN_RE.sub("", self._source.title))[:250]
        properties.author = "arXiv Digest"

    def _paragraph(
        self,
        container: Any = None,
        *,
        style: str | None = None,
        align: Any = WD_ALIGN_PARAGRAPH.JUSTIFY,
        indent: bool = True,
        space_before: float | None = None,
        space_after: float | None = None,
        keep_with_next: bool = False,
    ) -> Any:
        paragraph = (container or self._doc).add_paragraph(style=style)
        if container is None:
            self._after_table = False
        fmt = paragraph.paragraph_format
        if align is not None:
            fmt.alignment = align
        if indent:
            fmt.first_line_indent = Twips(FIRST_LINE_INDENT)
        if space_before is not None:
            fmt.space_before = Pt(space_before)
        if space_after is not None:
            fmt.space_after = Pt(space_after)
        if keep_with_next:
            fmt.keep_with_next = True
        return paragraph

    def _run(
        self,
        paragraph: Any,
        text: str,
        *,
        bold: bool = False,
        italic: bool = False,
        size: float | None = None,
        font: str | None = None,
    ) -> None:
        if not text:
            return
        run = paragraph.add_run(_xml_text(text))
        run.bold = bold or None
        run.italic = italic or None
        if size is not None:
            run.font.size = Pt(size)
        if font is not None:
            run.font.name = font

    def _omml(self, math: Math) -> Any | None:
        """Элемент m:oMath для формулы либо None, если её не удалось набрать."""
        try:
            mathml = math.mathml or (latex_to_mathml(math.latex) if math.latex else None)
            return parse_xml(mathml_to_omml(mathml)) if mathml else None
        except (MathConversionError, ValueError) as exc:
            log.warning("docx_formula_failed", error=str(exc), latex=(math.latex or "")[:200])
            return None

    def _fill(self, paragraph: Any, text: str, **fmt: Any) -> None:
        """Текст с метками строчных формул: формулы вставляются объектами редактора формул."""
        position = 0
        for match in MATH_TOKEN_RE.finditer(text):
            self._run(paragraph, text[position : match.start()], **fmt)
            position = match.end()
            math = self._source.math.get(int(match.group(1)))
            element = self._omml(math) if math is not None else None
            if element is not None:
                paragraph._p.append(element)
                self.stats.inline_formulas += 1
            elif math is not None:
                self._run(paragraph, math.text, **{**fmt, "italic": True})
        self._run(paragraph, text[position:], **fmt)

    def _image_width(self, image: Image, limit: float) -> Cm:
        width = image.width_cm or image.width_px / DEFAULT_IMAGE_DPI * 2.54
        width = min(width, limit)
        height = width * image.height_px / max(image.width_px, 1)
        if height > MAX_IMAGE_HEIGHT_CM:
            width *= MAX_IMAGE_HEIGHT_CM / height
        return Cm(max(width, 0.3))

    def _picture(self, paragraph: Any, image: Image, limit: float) -> bool:
        try:
            paragraph.add_run().add_picture(
                io.BytesIO(image.data), width=self._image_width(image, limit)
            )
        except Exception as exc:
            log.warning("docx_image_failed", error=f"{type(exc).__name__}: {exc}")
            return False
        return True

    def _container(self, rows: int, columns: tuple[int, ...]) -> Any:
        """Таблица-контейнер. Соседние таблицы Word склеил бы, поэтому между ними абзац."""
        if self._after_table:
            spacer = self._paragraph(indent=False, space_after=0)
            spacer.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
            spacer.paragraph_format.line_spacing = Pt(4)
        table = self._doc.add_table(rows=rows, cols=len(columns))
        table.style = "Table Grid"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.autofit = False
        _table_width(table, sum(columns))
        for row in table.rows:
            for cell, width in zip(row.cells, columns, strict=True):
                cell.width = Twips(width)
        self._after_table = True
        return table

    # --- содержимое ---

    def build(self) -> None:
        self._head()
        blocks = self._source.blocks
        index = 0
        while index < len(blocks):
            block = blocks[index]
            if isinstance(block, Formula):
                # идущие подряд формулы — строки одного контейнера
                end = index
                while end < len(blocks) and isinstance(blocks[end], Formula):
                    end += 1
                self._formulas([b for b in blocks[index:end] if isinstance(b, Formula)])
                index = end
                continue
            self._block(block)
            index += 1
        if self._after_table:
            self._paragraph(indent=False, space_after=0)

    def _head(self) -> None:
        source, labels, meta = self._source, self._labels, self._meta
        self._fill(
            self._paragraph(style="Title", align=WD_ALIGN_PARAGRAPH.CENTER, indent=False),
            source.title,
        )
        if source.authors:
            authors = self._paragraph(align=WD_ALIGN_PARAGRAPH.CENTER, indent=False)
            self._run(authors, ", ".join(source.authors), italic=True)
        details = [f"arXiv:{meta.arxiv_id}v{meta.version}"]
        if meta.categories:
            details[0] += f" [{meta.categories[0]}]"
        if meta.published_at is not None:
            details.append(f"{meta.published_at:%d.%m.%Y}")
        line = self._paragraph(align=WD_ALIGN_PARAGRAPH.CENTER, indent=False)
        self._run(line, f"{', '.join(details)}. {meta.abs_url}. {labels.note}", italic=True)
        for index, text in enumerate(source.abstract):
            paragraph = self._paragraph()
            if index == 0:
                self._run(paragraph, f"{labels.abstract} ", bold=True)
            self._fill(paragraph, text)
            self.stats.paragraphs += 1
        if source.keywords:
            paragraph = self._paragraph()
            self._run(paragraph, f"{labels.keywords} ", bold=True)
            self._fill(paragraph, source.keywords)

    def _block(self, block: Block) -> None:
        if isinstance(block, Heading):
            text = (
                f"{block.number}. {block.text}"
                if block.number and block.text
                else (block.text or block.number)
            )
            style = f"Heading {min(max(block.level, 1), 3)}"
            self._fill(self._paragraph(style=style, align=None), text)
            self.stats.headings += 1
        elif isinstance(block, Paragraph):
            self._text(block)
        elif isinstance(block, ListItem):
            self._list_item(block)
        elif isinstance(block, Figure):
            self._figure(block)
        elif isinstance(block, Table):
            self._table(block)
        elif isinstance(block, References):
            self._references(block)

    def _text(self, block: Paragraph) -> None:
        self.stats.paragraphs += 1
        if block.preformatted:
            paragraph = self._paragraph(align=WD_ALIGN_PARAGRAPH.LEFT, indent=False)
            paragraph.paragraph_format.line_spacing = 1.0
            for index, line in enumerate(block.text.split("\n")):
                if index:
                    paragraph.add_run().add_break()
                self._fill(paragraph, line, size=9, font=MONO_FONT)
            return
        if block.footnote:
            paragraph = self._paragraph(space_after=4)
            self._fill(paragraph, block.text, size=9)
            return
        paragraph = self._paragraph()
        if block.lead:
            self._fill(paragraph, f"{block.lead} ", bold=True)
        self._fill(paragraph, block.text)

    def _list_item(self, block: ListItem) -> None:
        self.stats.paragraphs += 1
        if block.marker:
            # нумерация берётся из оригинала: автонумерация Word сбивалась бы между списками
            paragraph = self._paragraph()
            self._run(paragraph, f"{block.marker} ")
        else:
            paragraph = self._paragraph(style="List Bullet")
            paragraph.paragraph_format.left_indent = Twips(0)
        paragraph.paragraph_format.space_after = Pt(4)
        self._fill(paragraph, block.text)

    def _caption_text(self, label: str, caption: str) -> str:
        label = _localized_label(label, self._labels.figure, self._labels.table) if label else ""
        if label and caption:
            return f"{label}{'' if label.endswith('.') else '.'} {caption}"
        return label or caption

    def _figure(self, block: Figure) -> None:
        caption = self._caption_text(block.label, block.caption)
        table = self._container(2 if caption else 1, (TEXT_WIDTH,))
        _no_borders(table)
        cell = table.rows[0].cells[0]
        paragraph = cell.paragraphs[0]
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.space_after = Pt(0)
        # панели одного рисунка идут в одну строку, пока помещаются
        limit = MAX_IMAGE_WIDTH_CM if len(block.images) == 1 else MAX_IMAGE_WIDTH_CM / 2 - 0.2
        placed = 0
        for image in block.images:
            wide = (image.width_cm or 0) > limit
            if self._picture(paragraph, image, MAX_IMAGE_WIDTH_CM if wide else limit):
                placed += 1
                self._run(paragraph, " ")
        if caption:
            paragraph = table.rows[1].cells[0].paragraphs[0]
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.space_after = Pt(6)
            self._fill(paragraph, caption, italic=True)
        self.stats.figures += 1 if placed else 0

    def _formulas(self, formulas: list[Formula]) -> None:
        table = self._container(len(formulas), FORMULA_COLUMNS)
        _no_borders(table)
        for row, formula in zip(table.rows, formulas, strict=True):
            body, number = row.cells
            paragraph = body.paragraphs[0]
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.space_before = Pt(3)
            paragraph.paragraph_format.space_after = Pt(3)
            element = self._omml(formula.math) if formula.math is not None else None
            self.stats.formulas += 1
            if element is not None:
                wrapper = OxmlElement("m:oMathPara")
                wrapper.append(element)
                paragraph._p.append(wrapper)
                self.stats.formulas_editable += 1
            if formula.image is not None:
                # вырезка оригинала из PDF: по ней сверяется восстановленная формула
                target = paragraph if element is None else body.add_paragraph()
                target.alignment = WD_ALIGN_PARAGRAPH.CENTER
                target.paragraph_format.space_after = Pt(3)
                self._picture(target, formula.image, MAX_FORMULA_IMAGE_WIDTH_CM)
            elif element is None:
                text = (formula.math.text if formula.math else "") or formula.source_text
                self._run(paragraph, text, italic=True)
            number.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            paragraph = number.paragraphs[0]
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.space_before = Pt(3)
            paragraph.paragraph_format.space_after = Pt(3)
            self._run(paragraph, formula.number)

    def _table(self, block: Table) -> None:
        # сетка с учётом объединённых ячеек: (строка, колонка) → ячейка источника
        grid: dict[tuple[int, int], Any] = {}
        occupied: set[tuple[int, int]] = set()
        for r, row in enumerate(block.rows):
            c = 0
            for cell in row:
                while (r, c) in occupied:
                    c += 1
                grid[(r, c)] = cell
                for dr in range(cell.rowspan):
                    for dc in range(cell.colspan):
                        occupied.add((r + dr, c + dc))
                c += cell.colspan
        if not occupied:
            return
        rows = max(r for r, _ in occupied) + 1
        columns = max(c for _, c in occupied) + 1

        caption = self._caption_text(block.label, block.caption)
        if caption:
            paragraph = self._paragraph(
                align=WD_ALIGN_PARAGRAPH.LEFT, indent=False, keep_with_next=True
            )
            self._fill(paragraph, caption, italic=True)

        size = 10 if columns < 8 else 8 if columns < 12 else 7
        widths = _column_widths(grid, columns, size)
        # разметка шапки есть не у всех таблиц: тогда шапкой считается первая строка
        mark_first_row = not any(cell.header for cell in grid.values())

        table = self._container(rows, tuple(widths))
        for (r, c), cell in grid.items():
            target = table.cell(r, c)
            last_row = min(r + cell.rowspan, rows) - 1
            last_column = min(c + cell.colspan, columns) - 1
            if (last_row, last_column) != (r, c):
                target = target.merge(table.cell(last_row, last_column))
            header = cell.header or (mark_first_row and r == 0)
            if header:
                _shade(target, HEADER_FILL)
            paragraph = target.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(0)
            paragraph.paragraph_format.line_spacing = 1.0
            self._fill(paragraph, cell.text, bold=header, size=size)
        self.stats.tables += 1

    def _references(self, block: References) -> None:
        self._fill(self._paragraph(style="Heading 1", align=None), self._labels.references)
        for item in block.items:
            paragraph = self._paragraph(align=WD_ALIGN_PARAGRAPH.LEFT, space_after=2)
            self._run(paragraph, item, size=9.5)
        self.stats.references += len(block.items)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".part")
        try:
            self._doc.save(str(temp_path))
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)
        self.stats.docx_size_bytes = path.stat().st_size


def build_docx(document: Document, *, language: str, meta: DocxMeta, path: Path) -> DocxStats:
    """Записывает перевод в docx и возвращает статистику по блокам."""
    writer = _Writer(document, language, meta)
    writer.build()
    writer.save(path)
    log.info("docx_built", path=str(path), **writer.stats.as_dict())
    return writer.stats
