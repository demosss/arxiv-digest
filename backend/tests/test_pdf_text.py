from pathlib import Path

import pymupdf
import pytest

from app.services.pdf_text import (
    TextExtractionError,
    clean_text,
    cut_references,
    extract_text,
    strip_headers_and_footers,
)

BODY = "This paragraph is long enough to look like real body text of a paper. " * 12


def _page(number: int, body: str) -> str:
    return f"Preprint. Under review.\n\n{body}\n\n{number}"


def test_hyphenated_line_breaks_are_joined() -> None:
    text = clean_text(["We propose a trans-\nformer based archi-\ntecture for sum-\nmarization."])

    assert text == "We propose a transformer based architecture for summarization."


def test_hyphen_before_capital_or_digit_is_kept() -> None:
    text = clean_text(["The GPT-\n4 model and the Text-\nTo-Text setup."])

    assert "GPT- 4" in text
    assert "Text- To-Text" in text


def test_lines_are_joined_and_paragraphs_preserved() -> None:
    text = clean_text(["First  line\nsecond\tline.\n\n\n\nNext   paragraph\nends here."])

    assert text == "First line second line.\n\nNext paragraph ends here."


WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india"]


def test_repeated_headers_and_page_numbers_are_removed() -> None:
    pages = [
        _page(i, f"Unique content {word}.\nSecond line {word}.") for i, word in enumerate(WORDS, 1)
    ]

    cleaned = strip_headers_and_footers(pages)

    for index, (word, page) in enumerate(zip(WORDS, cleaned, strict=True), start=1):
        assert "Preprint. Under review." not in page
        assert f"Unique content {word}." in page
        assert f"Second line {word}." in page
        assert not page.strip().endswith(str(index))


def test_numbered_running_footer_is_detected() -> None:
    pages = [
        f"Body text {word} is here.\nIt continues {word}.\n\nPage {i} of 9"
        for i, word in enumerate(WORDS, 1)
    ]

    text = clean_text(pages)

    assert "Page" not in text
    assert "Body text echo is here. It continues echo." in text


def test_unique_edge_lines_are_kept() -> None:
    pages = [f"Section {i} title\n\nBody {i}." for i in ("one", "two", "three", "four")]

    text = clean_text(pages)

    assert "Section one title" in text
    assert "Section four title" in text


@pytest.mark.parametrize(
    "heading",
    ["References", "REFERENCES", "Bibliography", "7 References", "7. References", "VI. References"],
)
def test_references_section_is_cut(heading: str) -> None:
    text = (
        f"{BODY}\n\n5 Conclusion\n\nWe conclude.\n\n{heading}\n\n[1] A. Author. Some paper. 2020."
    )

    result = cut_references(text)

    assert result.endswith("We conclude.")
    assert "[1] A. Author" not in result


def test_references_heading_glued_to_first_entry_is_cut() -> None:
    text = f"{BODY}\n\nWe conclude.\n\nReferences [1] A. Author. Some paper. 2020."

    assert cut_references(text).endswith("We conclude.")


def test_references_in_table_of_contents_is_ignored() -> None:
    text = f"Contents\n\nReferences\n\n1 Introduction\n\n{BODY}\n\nThe end of the paper."

    assert cut_references(text) == text


def test_word_references_inside_sentence_is_not_a_heading() -> None:
    text = f"{BODY}\n\nReferences to prior work are given in Section 2.\n\nThe end."

    assert cut_references(text) == text


def test_text_without_references_is_unchanged() -> None:
    assert cut_references(BODY) == BODY


def test_clean_text_cuts_references_after_joining_pages() -> None:
    pages = [
        BODY,
        BODY,
        "Final remarks.\n\nReferences\n\n[1] Vaswani et al.\nAttention is all you need.",
    ]

    text = clean_text(pages)

    assert text.endswith("Final remarks.")
    assert "Vaswani" not in text


def _make_pdf(path: Path, pages: list[str]) -> None:
    document = pymupdf.open()
    for index, body in enumerate(pages, start=1):
        page = document.new_page()
        page.insert_text((72, 40), "Preprint. Under review.", fontsize=9)
        page.insert_textbox(pymupdf.Rect(72, 72, 520, 760), body, fontsize=10)
        page.insert_text((300, 800), str(index), fontsize=9)
    document.save(path)
    document.close()


def test_extract_text_from_real_pdf(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    _make_pdf(
        pdf,
        [
            "Introduction. " + BODY,
            "Method. " + BODY,
            "Results. " + BODY,
            "Conclusion. The method works.\n\nReferences\n\n[1] A. Author. A cited paper. 2020.",
        ],
    )

    extracted = extract_text(pdf)

    assert extracted.pages == 4
    assert "Introduction." in extracted.text
    assert "The method works." in extracted.text
    assert "A cited paper" not in extracted.text
    assert "Preprint. Under review." not in extracted.text
    assert extracted.raw_chars > len(extracted.text)


def test_extract_text_fails_on_pdf_without_text(tmp_path: Path) -> None:
    pdf = tmp_path / "scan.pdf"
    document = pymupdf.open()
    document.new_page()
    document.save(pdf)
    document.close()

    with pytest.raises(TextExtractionError, match="почти нет текста"):
        extract_text(pdf)


def test_extract_text_fails_on_broken_file(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.7 this is not really a pdf")

    with pytest.raises(TextExtractionError, match="Не удалось прочитать PDF"):
        extract_text(broken)
