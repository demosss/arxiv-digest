import pytest
from lxml import etree

from app.services.translation.omml import (
    M_NS,
    MathConversionError,
    clean_latex,
    latex_to_omml,
    mathml_to_omml,
)

NS = {"m": M_NS}


def _omml(mathml: str) -> etree._Element:
    return etree.fromstring(mathml_to_omml(mathml))


def _texts(element: etree._Element) -> list[str]:
    return [node.text or "" for node in element.iterfind(".//m:t", NS)]


def test_fraction_and_subscripts() -> None:
    root = _omml(
        "<math><mrow><msub><mi>z</mi><mi>t</mi></msub><mo>=</mo>"
        "<mfrac><mrow><msub><mi>x</mi><mi>t</mi></msub><mo>−</mo><mi>μ</mi></mrow><mi>σ</mi></mfrac>"
        "</mrow></math>"
    )

    assert root.tag == f"{{{M_NS}}}oMath"
    assert len(root.findall(".//m:sSub", NS)) == 2
    fraction = root.find(".//m:f", NS)
    assert fraction is not None
    assert _texts(fraction.find("m:num", NS)) == ["x", "t", "−", "μ"]
    assert _texts(fraction.find("m:den", NS)) == ["σ"]


def test_sum_takes_following_expression_as_operand() -> None:
    root = _omml(
        "<math><munderover><mo>∑</mo><mrow><mi>t</mi><mo>=</mo><mn>1</mn></mrow><mi>T</mi>"
        "</munderover><msub><mi>X</mi><mi>t</mi></msub><mo>=</mo><mn>5</mn></math>"
    )

    nary = root.find("m:nary", NS)
    assert nary is not None
    assert nary.find("m:naryPr/m:chr", NS).get(f"{{{M_NS}}}val") == "∑"
    assert nary.find("m:naryPr/m:limLoc", NS).get(f"{{{M_NS}}}val") == "undOvr"
    assert _texts(nary.find("m:sub", NS)) == ["t", "=", "1"]
    assert _texts(nary.find("m:sup", NS)) == ["T"]
    # под знак суммы попадает X_t, а «= 5» остаётся снаружи
    assert _texts(nary.find("m:e", NS)) == ["X", "t"]
    assert _texts(root)[-2:] == ["=", "5"]


def test_sum_without_operand_is_not_a_nary_with_empty_box() -> None:
    root = _omml("<math><munder><mo>∑</mo><mi>i</mi></munder></math>")

    assert root.find(".//m:nary", NS) is None
    assert root.find("m:limLow", NS) is not None


def test_roots_accents_and_stretchy_brackets() -> None:
    root = _omml(
        "<math><msqrt><mi>x</mi></msqrt><mroot><mi>y</mi><mn>3</mn></mroot>"
        '<mover accent="true"><mi>y</mi><mo>^</mo></mover><mover><mi>x</mi><mo>¯</mo></mover>'
        "<mrow><mo>(</mo><mi>a</mi><mo>+</mo><mi>b</mi><mo>)</mo></mrow>"
        '<mrow><mo stretchy="false">(</mo><mi>c</mi><mo stretchy="false">)</mo></mrow></math>'
    )

    radicals = root.findall("m:rad", NS)
    assert radicals[0].find("m:radPr/m:degHide", NS) is not None
    assert _texts(radicals[1].find("m:deg", NS)) == ["3"]
    assert root.find("m:acc/m:accPr/m:chr", NS).get(f"{{{M_NS}}}val") == "\u0302"
    assert root.find("m:bar/m:barPr/m:pos", NS).get(f"{{{M_NS}}}val") == "top"
    # растягиваемые скобки — объект m:d, обычные остаются знаками
    delimiters = root.findall("m:d", NS)
    assert len(delimiters) == 1
    assert _texts(delimiters[0]) == ["a", "+", "b"]
    assert _texts(root)[-3:] == ["(", "c", ")"]


def test_cases_matrix_and_text_styles() -> None:
    root = _omml(
        "<math><mrow><mo>{</mo><mtable>"
        "<mtr><mtd><mn>1</mn></mtd><mtd><mtext>if x</mtext></mtd></mtr>"
        "<mtr><mtd><mn>0</mn></mtd></mtr></mtable></mrow>"
        '<mi>sin</mi><mi mathvariant="bold">R</mi><mi mathvariant="double-struck">R</mi></math>'
    )

    delimiter = root.find("m:d", NS)
    assert delimiter.find("m:dPr/m:endChr", NS).get(f"{{{M_NS}}}val") == ""
    rows = delimiter.findall(".//m:mr", NS)
    assert [len(row.findall("m:e", NS)) for row in rows] == [2, 2]
    assert delimiter.find(".//m:nor", NS) is not None
    styles = [node.get(f"{{{M_NS}}}val") for node in root.iterfind("m:r/m:rPr/m:sty", NS)]
    assert styles == ["p", "b"]
    assert root.find("m:r/m:rPr/m:scr", NS).get(f"{{{M_NS}}}val") == "double-struck"


def test_annotation_is_ignored_and_empty_formula_is_an_error() -> None:
    root = _omml(
        "<math><semantics><mi>μ</mi>"
        '<annotation encoding="application/x-tex">\\mu</annotation></semantics></math>'
    )

    assert _texts(root) == ["μ"]
    with pytest.raises(MathConversionError):
        mathml_to_omml("<math></math>")


def test_latex_is_cleaned_and_converted() -> None:
    assert clean_latex("$$ a^2 + b^2 = c^2 \\tag{3} $$") == "a^2 + b^2 = c^2"
    assert clean_latex("\\begin{equation}\\label{eq:x} x = 1 \\end{equation}") == "x = 1"
    assert clean_latex("```latex\n\\[ y \\]\n```") == "y"

    root = etree.fromstring(latex_to_omml("\\frac{a}{b} = \\sqrt{x_i}"))

    assert root.find(".//m:f", NS) is not None
    assert root.find(".//m:rad", NS) is not None
    assert root.find(".//m:sSub", NS) is not None
    with pytest.raises(MathConversionError):
        latex_to_omml("   ")
