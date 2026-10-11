from datetime import UTC, datetime
from pathlib import Path

import pymupdf
from docx import Document as open_docx
from docx.oxml.ns import qn

from app.services.translation.docx_writer import DocxMeta, build_docx
from app.services.translation.images import from_pixmap
from app.services.translation.model import (
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
    TableCell,
)

FRACTION = "<math><mfrac><mi>a</mi><mi>b</mi></mfrac></math>"
META = DocxMeta(
    arxiv_id="2610.00001",
    version=2,
    abs_url="https://arxiv.org/abs/2610.00001",
    published_at=datetime(2026, 10, 3, 12, 0, tzinfo=UTC),
    categories=("cs.RO", "cs.LG"),
)


def _image(width: int = 320, height: int = 180) -> Image:
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height))
    pixmap.set_rect(pixmap.irect, (30, 90, 200))
    return from_pixmap(pixmap)


def _document() -> Document:
    document = Document(
        title="Прогноз бортовой качки",
        authors=["Alice Smith", "Bob Jones"],
        abstract=["Первый абзац аннотации.", "Второй абзац аннотации."],
        keywords="качка, прогноз",
        source="html",
        blocks=[
            Heading(level=1, text="Введение", number="1"),
            Paragraph(text="Здесь <f1/> обозначает вход, а <f2/> не удалось набрать."),
            Paragraph(text="Определение дано ниже.", lead="Определение 1."),
            ListItem(text="Маркированный пункт."),
            ListItem(text="Нумерованный пункт.", marker="1."),
            Figure(images=[_image()], caption="Общая структура", label="Figure 1"),
            Formula(math=Math(mathml=FRACTION), number="(1)"),
            Formula(math=None, number="(2)", image=_image(400, 60), source_text="a b"),
            Heading(level=2, text="Данные", number="1.1"),
            Table(
                rows=[
                    [TableCell("Параметр", header=True), TableCell("Значение", header=True)],
                    [TableCell("Длина"), TableCell("7.5")],
                ],
                caption="Параметры судна",
                label="Table 1",
            ),
            Table(
                rows=[
                    [TableCell("Модель", rowspan=2), TableCell("Ошибка", colspan=2)],
                    [TableCell("RMSE"), TableCell("MAE")],
                    [TableCell("LSTM"), TableCell("0.1"), TableCell("0.2")],
                ]
            ),
            Paragraph(text="line one\nline two", preformatted=True),
            Paragraph(text="¹ Текст сноски.", footnote=True),
            References(title="References", items=["[1] Q. Zhang. Stabilization. 2024."]),
        ],
    )
    document.math = {
        1: Math(mathml="<math><msub><mi>x</mi><mi>t</mi></msub></math>"),
        2: Math(text="μ"),
    }
    return document


def _build(tmp_path: Path, language: str = "ru"):  # type: ignore[no-untyped-def]
    path = tmp_path / "out" / "paper.docx"
    stats = build_docx(_document(), language=language, meta=META, path=path)
    return open_docx(str(path)), stats, path


def test_page_and_head_follow_the_sample(tmp_path: Path) -> None:
    docx, stats, path = _build(tmp_path)

    section = docx.sections[0]
    assert (section.page_width.twips, section.page_height.twips) == (11906, 16838)
    assert (section.left_margin.twips, section.right_margin.twips) == (1417, 850)
    assert (section.top_margin.twips, section.bottom_margin.twips) == (1134, 1134)
    assert docx.styles["Normal"].font.name == "Arial"

    texts = [paragraph.text for paragraph in docx.paragraphs]
    assert texts[0] == "Прогноз бортовой качки"
    assert docx.paragraphs[0].style.name == "Title"
    assert texts[1] == "Alice Smith, Bob Jones"
    assert texts[2] == (
        "arXiv:2610.00001v2 [cs.RO], 03.10.2026. https://arxiv.org/abs/2610.00001. "
        "Машинный перевод."
    )
    assert texts[3] == "Аннотация. Первый абзац аннотации."
    assert docx.paragraphs[3].runs[0].bold
    assert docx.paragraphs[3].paragraph_format.first_line_indent.twips == 284
    assert texts[5] == "Ключевые слова: качка, прогноз"
    assert texts[6] == "1. Введение"
    assert docx.paragraphs[6].style.name == "Heading 1"
    assert stats.docx_size_bytes == path.stat().st_size
    assert not path.with_suffix(".docx.part").exists()


def test_inline_formula_is_an_equation_object(tmp_path: Path) -> None:
    docx, stats, _ = _build(tmp_path)

    paragraph = next(p for p in docx.paragraphs if p.text.startswith("Здесь"))
    assert len(paragraph._p.findall(qn("m:oMath"))) == 1
    # формула без MathML печатается запасным текстом
    assert paragraph.text == "Здесь  обозначает вход, а μ не удалось набрать."
    assert stats.inline_formulas == 1

    lead = next(p for p in docx.paragraphs if p.text.startswith("Определение 1."))
    assert lead.runs[0].bold and not lead.runs[-1].bold
    bullet = next(p for p in docx.paragraphs if p.text == "Маркированный пункт.")
    assert bullet.style.name == "List Bullet"
    assert any(p.text == "1. Нумерованный пункт." for p in docx.paragraphs)


def test_figures_and_formulas_sit_in_borderless_containers(tmp_path: Path) -> None:
    docx, stats, _ = _build(tmp_path)
    figure, formulas, data, _ = docx.tables

    # рисунок: картинка и под ней подпись со словом на языке перевода
    assert (len(figure.rows), len(figure.columns)) == (2, 1)
    assert figure.rows[0].cells[0]._tc.findall(f".//{qn('w:drawing')}")
    assert figure.rows[1].cells[0].text == "Рис. 1. Общая структура"
    for container in (figure, formulas):
        borders = container._tbl.tblPr.find(qn("w:tblBorders"))
        assert {border.get(qn("w:val")) for border in borders} == {"none"}
    assert data._tbl.tblPr.find(qn("w:tblBorders")) is None

    # две формулы подряд — строки одного контейнера: формула слева, номер справа
    assert (len(formulas.rows), len(formulas.columns)) == (2, 2)
    first, second = formulas.rows
    assert first.cells[0]._tc.findall(f".//{qn('m:oMathPara')}/{qn('m:oMath')}/{qn('m:f')}")
    assert first.cells[1].text == "(1)"
    assert [cell.width.twips for cell in first.cells] == [8831, 808]
    # формула, которую не удалось набрать, остаётся вырезкой из PDF
    assert not second.cells[0]._tc.findall(f".//{qn('m:oMath')}")
    assert second.cells[0]._tc.findall(f".//{qn('w:drawing')}")
    assert second.cells[1].text == "(2)"
    assert (stats.formulas, stats.formulas_editable, stats.figures) == (2, 1, 1)

    # между соседними таблицами есть абзац: иначе Word склеил бы их в одну
    body = [child.tag for child in docx.element.body]
    for index, tag in enumerate(body[:-1]):
        assert not (tag == qn("w:tbl") and body[index + 1] == qn("w:tbl"))


def test_data_tables_have_shaded_header_and_merged_cells(tmp_path: Path) -> None:
    docx, stats, _ = _build(tmp_path)
    data, spans = docx.tables[2], docx.tables[3]

    assert [[cell.text for cell in row.cells] for row in data.rows] == [
        ["Параметр", "Значение"],
        ["Длина", "7.5"],
    ]
    shading = data.rows[0].cells[0]._tc.tcPr.find(qn("w:shd"))
    assert shading.get(qn("w:fill")) == "D9E2F3"
    assert data.rows[1].cells[0]._tc.tcPr.find(qn("w:shd")) is None
    assert data.rows[0].cells[0].paragraphs[0].runs[0].bold
    assert any(p.text == "Таблица 1. Параметры судна" for p in docx.paragraphs)

    # объединённые ячейки; шапка не размечена, поэтому залита первая строка
    assert (len(spans.rows), len(spans.columns)) == (3, 3)
    assert spans.cell(0, 0)._tc is spans.cell(1, 0)._tc
    assert spans.cell(0, 1)._tc is spans.cell(0, 2)._tc
    assert spans.cell(0, 1)._tc.tcPr.find(qn("w:shd")) is not None
    assert spans.cell(2, 1).text == "0.1"
    assert stats.tables == 2


def test_references_stay_in_source_language(tmp_path: Path) -> None:
    docx, stats, _ = _build(tmp_path)

    texts = [paragraph.text for paragraph in docx.paragraphs]
    position = texts.index("Список литературы")
    assert docx.paragraphs[position].style.name == "Heading 1"
    assert texts[position + 1] == "[1] Q. Zhang. Stabilization. 2024."
    assert stats.references == 1
    assert "line one\nline two" in texts


def test_labels_follow_target_language(tmp_path: Path) -> None:
    docx, _, _ = _build(tmp_path, language="de")
    texts = [paragraph.text for paragraph in docx.paragraphs]

    assert texts[3].startswith("Zusammenfassung. ")
    assert "Literaturverzeichnis" in texts
    assert docx.tables[0].rows[1].cells[0].text == "Abb. 1. Общая структура"

    # для языка, введённого вручную, служебные слова остаются английскими
    docx, _, _ = _build(tmp_path, language="итальянский")
    assert any(p.text.startswith("Abstract. ") for p in docx.paragraphs)
