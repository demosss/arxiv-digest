from pathlib import Path

import pymupdf
import pytest

from app.services.translation.html_source import HtmlParseError, html_url, parse_html
from app.services.translation.images import from_pixmap, make_image
from app.services.translation.model import (
    Document,
    Figure,
    Formula,
    Heading,
    Image,
    ListItem,
    Paragraph,
    References,
    Table,
)

FIXTURE = Path(__file__).parent / "fixtures" / "arxiv_paper.html"
BASE_URL = "https://arxiv.org/html/2610.00001v1"


def _png(width: int = 64, height: int = 36) -> bytes:
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height))
    pixmap.set_rect(pixmap.irect, (30, 90, 200))
    return pixmap.tobytes("png")


class Loader:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, url: str) -> Image | None:
        self.urls.append(url)
        return make_image(_png())


def _parse(loader: Loader | None = None) -> Document:
    return parse_html(FIXTURE.read_bytes(), base_url=BASE_URL, load_image=loader or Loader())


@pytest.fixture(autouse=True)
def _short_documents_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.translation.html_source.MIN_TEXT_CHARS", 100)


def test_title_abstract_and_keywords() -> None:
    document = _parse()

    # формула в названии — меткой, сноска с благодарностями отброшена
    assert document.title == "Roll prediction with <f1/>-centralization"
    assert document.math[1].latex == "\\alpha"
    assert len(document.abstract) == 1
    assert document.abstract[0].startswith("Reliable roll prediction")
    assert document.keywords == "Unmanned surface vehicle, Roll prediction, Multi-task learning"
    assert document.source == "html"


def test_headings_keep_numbers_apart_from_text() -> None:
    headings = [block for block in _parse().blocks if isinstance(block, Heading)]

    assert [(h.level, h.number, h.text) for h in headings] == [
        (1, "1", "Introduction"),
        (1, "2", "Methodology"),
        (2, "2.1", "Adaptive Centralization"),
        # метка приложения переводится вместе с названием
        (1, "", "Appendix A. Extra results"),
    ]


def test_paragraph_with_inline_math_citation_and_footnote() -> None:
    document = _parse()
    paragraphs = [block for block in document.blocks if isinstance(block, Paragraph)]

    first = paragraphs[0]
    # число с процентом остаётся текстом, переменная — меткой; мусор LaTeXML выброшен
    assert first.text == (
        "Small vehicles are susceptible to waves (Zhang et al., 2024). "
        "The accuracy reaches 95% on the test set¹ and the model uses a window <f2/> of samples."
    )
    assert document.math[2].latex == "x_{t}"
    assert "<msub" in (document.math[2].mathml or "")
    assert paragraphs[1].footnote
    assert paragraphs[1].text == "¹ Measured on real sea trials."


def test_equations_get_numbers_and_aligned_parts_are_joined() -> None:
    formulas = [block for block in _parse().blocks if isinstance(block, Formula)]

    assert [formula.number for formula in formulas] == ["(1)", "(2)", ""]
    assert formulas[0].math is not None
    assert formulas[0].math.latex == "z_{t}=\\frac{x_{t}-\\mu}{\\sigma},"
    # левая и правая части выровненной строки — одна формула
    joined = formulas[1].math
    assert joined is not None and joined.mathml is not None
    assert joined.mathml.count("<mrow><semantics>") == 2
    assert "\\sum_{t=1}^{T}" in (joined.latex or "")


def test_figure_is_loaded_by_absolute_url() -> None:
    loader = Loader()
    figures = [block for block in _parse(loader).blocks if isinstance(block, Figure)]

    assert loader.urls == ["https://arxiv.org/html/2610.00001v1/overall.png"]
    assert len(figures) == 1
    assert (figures[0].label, figures[0].caption) == ("Figure 1", "Overall structure")
    image = figures[0].images[0]
    assert (image.ext, image.width_px, image.height_px) == ("png", 64, 36)
    # ширина в документе — по размеру из HTML (384 px = 4 дюйма)
    assert image.width_cm == pytest.approx(10.16)


def test_lists_keep_markers() -> None:
    items = [block for block in _parse().blocks if isinstance(block, ListItem)]

    assert [(item.marker, item.text) for item in items] == [
        ("1.", "We propose a reliability-aware paradigm."),
        ("2.", "We design a confidence head."),
        ("", "A bulleted remark."),
    ]


def test_table_cells_spans_and_header() -> None:
    table = next(block for block in _parse().blocks if isinstance(block, Table))

    assert (table.label, table.caption) == ("Table 1", "Vehicle dimension parameters")
    assert [[cell.text for cell in row] for row in table.rows] == [
        ["Dimensions", "Symbol and value"],
        ["Length (m)", "<f4/>", "7.5"],
        ["Draft (m)", "T", "0.54"],
    ]
    assert [cell.header for cell in table.rows[0]] == [True, True]
    assert table.rows[0][1].colspan == 2
    assert not table.rows[1][0].header


def test_theorem_title_becomes_paragraph_lead() -> None:
    paragraph = next(
        block for block in _parse().blocks if isinstance(block, Paragraph) and block.lead
    )

    assert paragraph.lead == "Definition 1."
    assert paragraph.text == "A sequence is centralized if its mean is zero."


def test_references_stay_as_source_text() -> None:
    document = _parse()
    references = next(block for block in document.blocks if isinstance(block, References))

    assert references.title == "References"
    assert references.items == [
        "[1] Q. Zhang and B. Wu. Stabilization of onboard systems. Ocean Engineering, 2024.",
        # у стиля «автор — год» метка не печатается, служебное «Cited by» отброшено
        "E. Tian, Y. Li. Docking control of unmanned vehicles.",
    ]
    # приложение идёт после списка литературы, как в оригинале
    position = document.blocks.index(references)
    assert isinstance(document.blocks[position + 1], Heading)


def test_page_without_article_or_text_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(HtmlParseError, match="нет статьи"):
        parse_html(
            b"<html><body><p>No HTML for this paper</p></body></html>",
            base_url=BASE_URL,
            load_image=Loader(),
        )

    monkeypatch.setattr("app.services.translation.html_source.MIN_TEXT_CHARS", 100_000)
    with pytest.raises(HtmlParseError, match="почти нет текста"):
        _parse()


def test_html_url_uses_stored_version() -> None:
    assert html_url("2610.00001", 3) == "https://arxiv.org/html/2610.00001v3"


def test_images_are_normalized_for_docx() -> None:
    assert make_image(b"not an image") is None

    large = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 4800, 1200))
    large.set_rect(large.irect, (255, 255, 255))
    image = from_pixmap(large)
    # слишком большая картинка уменьшается, пропорции сохраняются
    assert (image.width_px, image.height_px) == (2400, 600)

    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60" viewBox="0 0 120 60">'
        b'<rect width="120" height="60" fill="#3366cc"/></svg>'
    )
    rendered = make_image(svg)
    assert rendered is not None
    assert rendered.ext == "png"
    assert rendered.width_px == 240


def test_panels_before_shared_caption_become_one_figure() -> None:
    # часть составных рисунков LaTeXML размечает так: панели — соседние figure,
    # а общая подпись лежит в следующем за ними пустом figure
    panels = (
        b'<figure id="S9.F7.sf1" class="ltx_figure"><img src="a.png" class="ltx_graphics"/>'
        b'<figcaption class="ltx_caption"><span class="ltx_tag ltx_tag_figure">(a)</span>'
        b"</figcaption></figure>"
        b'<figure id="S9.F7.sf2" class="ltx_figure"><img src="b.png" class="ltx_graphics"/>'
        b'<figcaption class="ltx_caption"><span class="ltx_tag ltx_tag_figure">(b)</span>'
        b"</figcaption></figure>"
        b'<figure id="S9.F7" class="ltx_figure"><figcaption class="ltx_caption">'
        b'<span class="ltx_tag ltx_tag_figure">Figure 7: </span>Results for both setups'
        b"</figcaption></figure>"
    )
    content = FIXTURE.read_bytes().replace(b'<section id="bib"', panels + b'<section id="bib"')
    loader = Loader()

    document = parse_html(content, base_url=BASE_URL, load_image=loader)

    figures = [block for block in document.blocks if isinstance(block, Figure)]
    assert [(figure.label, len(figure.images)) for figure in figures] == [
        ("Figure 1", 1),
        ("Figure 7", 2),
    ]
    assert figures[1].caption == "Results for both setups"
    assert loader.urls[-2:] == [
        "https://arxiv.org/html/a.png",
        "https://arxiv.org/html/b.png",
    ]
