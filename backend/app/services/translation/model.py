"""Структура статьи для перевода: блоки в порядке чтения.

Строчные формулы в тексте заменены метками вида `<f12/>`; сами формулы лежат
в `Document.math` под тем же номером. Так модель переводит текст, не трогая формулы.
"""

import re
from dataclasses import dataclass, field

SOURCE_HTML = "html"
SOURCE_PDF = "pdf"

MATH_TOKEN_RE = re.compile(r"<\s*f\s*(\d+)\s*/?\s*>", re.IGNORECASE)


def math_token(index: int) -> str:
    return f"<f{index}/>"


@dataclass(slots=True)
class Image:
    data: bytes
    # формат, понятный python-docx: png или jpeg
    ext: str
    width_px: int
    height_px: int
    # ширина в исходной вёрстке, см; None — вписать по ширине страницы
    width_cm: float | None = None


@dataclass(slots=True)
class Math:
    mathml: str | None = None
    latex: str | None = None
    # запасной вид обычным текстом: если формулу не удалось набрать или сохранить в переводе
    text: str = ""


@dataclass(slots=True)
class Heading:
    level: int
    text: str
    # номер раздела («2.1»), в перевод не отправляется
    number: str = ""


@dataclass(slots=True)
class Paragraph:
    text: str
    # выделенное начало абзаца: «Theorem 1.», «Proof.»
    lead: str = ""
    footnote: bool = False
    # листинги и псевдокод: не переводятся, печатаются моноширинным шрифтом
    preformatted: bool = False


@dataclass(slots=True)
class ListItem:
    text: str
    # «1.», «(a)»; пустая строка — маркированный список
    marker: str = ""


@dataclass(slots=True)
class Figure:
    images: list[Image]
    caption: str = ""
    # подпись-метка из оригинала («Figure 1»); в переводе слово заменяется, номер остаётся
    label: str = ""


@dataclass(slots=True)
class Formula:
    math: Math | None
    number: str = ""
    # вырезка оригинала из PDF: печатается под набранной формулой для сверки
    image: Image | None = None
    # текст формулы, извлечённый из PDF: по нему LLM восстанавливает LaTeX
    source_text: str = ""


@dataclass(slots=True)
class TableCell:
    text: str
    colspan: int = 1
    rowspan: int = 1
    header: bool = False


@dataclass(slots=True)
class Table:
    rows: list[list[TableCell]]
    caption: str = ""
    label: str = ""


@dataclass(slots=True)
class References:
    title: str
    items: list[str]


Block = Heading | Paragraph | ListItem | Figure | Formula | Table | References


@dataclass(slots=True)
class Document:
    title: str
    authors: list[str]
    abstract: list[str]
    blocks: list[Block]
    source: str
    keywords: str = ""
    math: dict[int, Math] = field(default_factory=dict)

    def add_math(self, math: Math) -> str:
        """Регистрирует строчную формулу и возвращает её метку для вставки в текст."""
        index = len(self.math) + 1
        self.math[index] = math
        return math_token(index)

    def text_chars(self) -> int:
        """Объём переводимого текста: по нему видно, что разбор не вернул пустышку."""
        total = sum(len(part) for part in self.abstract)
        for block in self.blocks:
            if isinstance(block, Paragraph | ListItem | Heading):
                total += len(block.text)
        return total
