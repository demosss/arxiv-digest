from pathlib import Path

import pymupdf
import pytest

from app.services.translation.model import (
    Document,
    Figure,
    Formula,
    Heading,
    ListItem,
    Paragraph,
    References,
    Table,
)
from app.services.translation.pdf_source import PdfParseError, parse_pdf

TITLE = "Roll Prediction for Surface Vehicles"
ABSTRACT = "We study reliable roll prediction of unmanned surface vehicles in rough seas."
SENTENCE = "Small vehicles are susceptible to wave-induced perturbations in rough seas. "
LEFT, RIGHT = 72, 523


def _line(
    page: pymupdf.Page, top: float, text: str, fontsize: float = 10, fontname: str = "helv"
) -> None:
    page.insert_text((LEFT, top + fontsize), text, fontsize=fontsize, fontname=fontname)


def _box(page: pymupdf.Page, top: float, height: float, text: str) -> None:
    written = page.insert_textbox(pymupdf.Rect(LEFT, top, RIGHT, top + height), text, fontsize=10)
    # insert_textbox молча ничего не пишет, если текст не поместился
    assert written >= 0, text[:40]


def _grid(page: pymupdf.Page, top: float, rows: list[list[str]]) -> None:
    """Таблица с линиями: сетка и текст в ячейках."""
    left, width, height = 150.0, 110.0, 18.0
    columns = len(rows[0])
    for r in range(len(rows) + 1):
        y = top + r * height
        page.draw_line((left, y), (left + columns * width, y))
    for c in range(columns + 1):
        x = left + c * width
        page.draw_line((x, top), (x, top + len(rows) * height))
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            page.insert_text((left + c * width + 4, top + r * height + 13), text, fontsize=9)


def make_paper(path: Path) -> None:
    pdf = pymupdf.open()
    first = pdf.new_page()
    _line(first, 60, TITLE, 18, "hebo")
    _line(first, 95, "Alice Smith, Bob Jones")
    _line(first, 115, "Abstract", fontname="hebo")
    _line(first, 132, ABSTRACT)
    _line(first, 175, "1 Introduction", 13, "hebo")
    _box(first, 198, 70, SENTENCE * 4)
    _line(first, 275, "The standardization is defined as follows:")
    first.insert_text((230, 305), "abg = s", fontname="symb", fontsize=10)
    first.insert_text((495, 305), "(1)", fontsize=10)
    _line(first, 318, "where the symbols denote the mean and the deviation of the sequence.")

    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 100))
    pixmap.set_rect(pixmap.irect, (30, 90, 200))
    first.insert_image(pymupdf.Rect(200, 345, 400, 445), pixmap=pixmap)
    _line(first, 452, "Figure 1: Overall structure of the model.", 9)

    _line(first, 480, "The contributions are as follows.")
    _line(first, 498, "• A reliability-aware prediction paradigm.")
    # абзац обрывается концом страницы и продолжается на следующей
    _box(first, 700, 70, SENTENCE * 3 + "The model is trained on long")
    first.insert_text((295, 815), "1", fontsize=9)

    second = pdf.new_page()
    _box(second, 60, 40, "sequences of roll angles collected at sea. " + SENTENCE)
    _line(second, 100, "Table 1: Vehicle parameters", 9)
    _grid(second, 120, [["Dimension", "Value"], ["Length", "7.5"], ["Draft", "0.54"]])
    _line(second, 200, "2 Results", 13, "hebo")
    _box(second, 222, 55, SENTENCE * 3)
    _line(second, 290, "References", 13, "hebo")
    _line(second, 315, "[1] Q. Zhang and B. Wu. Stabilization of onboard systems. 2024.")
    _line(second, 335, "[2] E. Tian and Y. Li. Docking control of unmanned vehicles. 2024.")
    second.insert_text((295, 815), "2", fontsize=9)
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(path)
    pdf.close()


@pytest.fixture
def document(tmp_path: Path) -> Document:
    path = tmp_path / "paper.pdf"
    make_paper(path)
    return parse_pdf(path, title=TITLE, authors=["Alice Smith"], abstract=ABSTRACT)


def test_front_matter_comes_from_metadata(document: Document) -> None:
    assert document.source == "pdf"
    assert (document.title, document.authors) == (TITLE, ["Alice Smith"])
    assert document.abstract == [ABSTRACT]
    texts = [block.text for block in document.blocks if isinstance(block, Paragraph)]
    # название, авторы и аннотация из самого PDF в текст не попадают, номера страниц тоже
    assert not any(TITLE in text or "Alice Smith" in text or ABSTRACT in text for text in texts)
    assert not any(text.strip() in ("1", "2") for text in texts)


def test_headings_and_references(document: Document) -> None:
    headings = [(b.level, b.number, b.text) for b in document.blocks if isinstance(b, Heading)]
    assert headings == [(1, "1", "Introduction"), (1, "2", "Results")]

    references = document.blocks[-1]
    assert isinstance(references, References)
    assert references.items == [
        "[1] Q. Zhang and B. Wu. Stabilization of onboard systems. 2024.",
        "[2] E. Tian and Y. Li. Docking control of unmanned vehicles. 2024.",
    ]


def test_paragraph_broken_by_page_end_is_joined(document: Document) -> None:
    paragraphs = [block.text for block in document.blocks if isinstance(block, Paragraph)]
    joined = next(text for text in paragraphs if "The model is trained on long" in text)

    assert "trained on long sequences of roll angles collected at sea." in joined
    assert joined.startswith(SENTENCE.strip())
    assert joined.endswith("perturbations in rough seas.")
    assert sum("sequences of roll angles" in text for text in paragraphs) == 1


def test_formula_is_cut_out_with_number_and_source_text(document: Document) -> None:
    formulas = [block for block in document.blocks if isinstance(block, Formula)]

    assert len(formulas) == 1
    formula = formulas[0]
    assert formula.number == "(1)"
    # набранной формулы ещё нет: её восстановит LLM по извлечённому тексту
    assert formula.math is None
    assert formula.source_text == "abg = s"
    assert formula.image is not None and formula.image.ext == "png"
    assert formula.image.width_px > 100
    index = document.blocks.index(formula)
    before, after = document.blocks[index - 1], document.blocks[index + 1]
    assert isinstance(before, Paragraph) and before.text.endswith("defined as follows:")
    assert isinstance(after, Paragraph) and after.text.startswith("where the symbols")


def test_figure_table_and_list(document: Document) -> None:
    figure = next(block for block in document.blocks if isinstance(block, Figure))
    assert (figure.label, figure.caption) == ("Figure 1", "Overall structure of the model.")
    assert len(figure.images) == 1
    assert figure.images[0].width_px > 400

    table = next(block for block in document.blocks if isinstance(block, Table))
    assert (table.label, table.caption) == ("Table 1", "Vehicle parameters")
    assert [[cell.text for cell in row] for row in table.rows] == [
        ["Dimension", "Value"],
        ["Length", "7.5"],
        ["Draft", "0.54"],
    ]
    assert all(cell.header for cell in table.rows[0])

    items = [block.text for block in document.blocks if isinstance(block, ListItem)]
    assert items == ["A reliability-aware prediction paradigm."]


def test_pdf_without_text_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    pdf = pymupdf.open()
    pdf.new_page()
    pdf.save(path)
    pdf.close()

    with pytest.raises(PdfParseError, match="почти нет текста"):
        parse_pdf(path, title=TITLE, authors=[], abstract="")
    with pytest.raises(PdfParseError, match="Не удалось открыть"):
        parse_pdf(tmp_path / "missing.pdf", title=TITLE, authors=[], abstract="")
