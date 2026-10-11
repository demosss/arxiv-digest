import pytest

from app.services.llm_client import LLMError, LLMUnavailableError
from app.services.translation.model import (
    Document,
    Figure,
    Formula,
    Heading,
    Math,
    Paragraph,
    References,
    Table,
    TableCell,
)
from app.services.translation.translator import (
    Translator,
    clean_translation,
    needs_translation,
    split_text,
)
from tests.factories import ENGLISH_PARAGRAPH as SOURCE
from tests.factories import RUSSIAN_PARAGRAPH as RUSSIAN
from tests.factories import FakeTranslationLLM as FakeLLM


def _translator(llm: FakeLLM, language: str = "ru", chunk_chars: int = 3000) -> Translator:
    return Translator(llm, language=language, chunk_chars=chunk_chars, max_tokens=2048)


def _document(*blocks: object, **kwargs: object) -> Document:
    return Document(
        title=kwargs.pop("title", "Roll prediction for surface vehicles"),  # type: ignore[arg-type]
        authors=["Alice Smith"],
        abstract=kwargs.pop("abstract", []),  # type: ignore[arg-type]
        blocks=list(blocks),  # type: ignore[arg-type]
        source="html",
        **kwargs,  # type: ignore[arg-type]
    )


def test_document_is_translated_in_place() -> None:
    table = Table(
        rows=[
            [TableCell("Dimensions", header=True), TableCell("Value", header=True)],
            [TableCell("Length of the hull"), TableCell("7.5")],
            [TableCell("LSTM"), TableCell("<f1/>")],
        ],
        caption="Vehicle dimension parameters of the test platform.",
    )
    document = _document(
        Heading(level=1, text="Introduction", number="1"),
        Paragraph(text=SOURCE),
        Paragraph(text="for i in range(10): step()", preformatted=True),
        Figure(images=[], caption="Overall structure of the proposed prediction model."),
        table,
        References(title="References", items=["[1] Q. Zhang. Stabilization. 2024."]),
        abstract=[SOURCE],
        keywords="roll prediction, multi-task learning.",
    )
    llm = FakeLLM()

    stats = _translator(llm).translate(document)

    assert document.blocks[0].text == "Introduction (рус.)"  # type: ignore[union-attr]
    assert document.blocks[0].number == "1"  # type: ignore[union-attr]
    assert document.blocks[1].text == RUSSIAN  # type: ignore[union-attr]
    assert document.abstract == [RUSSIAN]
    # листинг, числа, обозначения, формулы и список литературы остаются как есть
    assert document.blocks[2].text == "for i in range(10): step()"  # type: ignore[union-attr]
    assert [[cell.text for cell in row] for row in table.rows] == [
        ["Dimensions (рус.)", "Value (рус.)"],
        ["Length of the hull (рус.)", "7.5"],
        ["LSTM", "<f1/>"],
    ]
    assert document.blocks[5].items == ["[1] Q. Zhang. Stabilization. 2024."]  # type: ignore[union-attr]
    assert stats.untranslated == 0
    assert stats.translated == stats.segments
    # короткие строки ушли одной пачкой, абзацы — по одному запросу
    purposes = [call["purpose"] for call in llm.calls]
    assert purposes.count("translate batch") == 1
    assert all(call["max_tokens"] == 2048 for call in llm.calls)
    assert "на русский язык" in str(llm.calls[0]["user"])


def test_formula_tokens_must_survive_translation() -> None:
    source = f"{SOURCE} Here <f1/> denotes the input sequence and <f2/> the mean."
    document = _document(Paragraph(text=source), title="T")
    document.math = {1: Math(text="x_t"), 2: Math(text="μ")}
    llm = FakeLLM([f"{RUSSIAN} Здесь <f1/> обозначает входную последовательность.", ""])
    llm.replies[1] = f"{RUSSIAN} Здесь < F1 > — входная последовательность, а <f2> — среднее."

    stats = _translator(llm).translate(document)

    # первый ответ потерял метку — запрос повторён с указанием причины; метки приведены к виду
    assert document.blocks[0].text.endswith(  # type: ignore[union-attr]
        "Здесь <f1/> — входная последовательность, а <f2/> — среднее."
    )
    assert "пропущены <f2/>" in str(llm.calls[1]["user"])
    assert stats.untranslated == 0


def test_lost_tokens_fall_back_to_plain_text_formulas() -> None:
    source = f"{SOURCE} Here <f1/> denotes the input sequence."
    document = _document(Paragraph(text=source), title="T")
    document.math = {1: Math(text="x_t")}
    llm = FakeLLM([RUSSIAN, RUSSIAN, f"{RUSSIAN} Здесь x_t обозначает вход."])

    stats = _translator(llm).translate(document)

    # модель дважды потеряла метку — третий запрос уже с формулой обычным текстом
    assert llm.calls[2]["purpose"] == "translate plain"
    assert "Here x_t denotes" in str(llm.calls[2]["user"])
    assert document.blocks[0].text.endswith("Здесь x_t обозначает вход.")  # type: ignore[union-attr]
    assert stats.untranslated == 0


def test_failed_segment_keeps_original_text() -> None:
    document = _document(Paragraph(text=SOURCE), Paragraph(text=SOURCE + " More text."), title="T")
    llm = FakeLLM(["", "I cannot translate this text.", LLMError("HTTP 400: context overflow")])

    stats = _translator(llm).translate(document)

    # пустой и непереведённый ответы, затем ошибка модели: оба абзаца остались в оригинале
    assert document.blocks[0].text == SOURCE  # type: ignore[union-attr]
    assert document.blocks[1].text == SOURCE + " More text."  # type: ignore[union-attr]
    assert (stats.translated, stats.untranslated) == (0, 2)


def test_unavailable_model_aborts_translation() -> None:
    document = _document(Paragraph(text=SOURCE), title="T")
    llm = FakeLLM([LLMUnavailableError("LLM недоступна")])

    with pytest.raises(LLMUnavailableError):
        _translator(llm).translate(document)


def test_long_paragraph_is_split_and_truncated_answer_is_retried_in_halves() -> None:
    long_source = " ".join([SOURCE] * 4)
    document = _document(Paragraph(text=long_source), title="T")
    llm = FakeLLM()

    _translator(llm, chunk_chars=200).translate(document)

    assert len(llm.calls) == 4
    assert document.blocks[0].text == " ".join([RUSSIAN] * 4)  # type: ignore[union-attr]

    # ответ, упёршийся в лимит токенов, не принимается: фрагмент делится пополам
    truncated = FakeLLM()
    document = _document(Paragraph(text=" ".join([SOURCE] * 3)), title="T")
    calls = 0

    def complete(system: str, user: str, *, purpose: str, max_tokens: int | None = None):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        truncated.finish_reason = "length" if calls == 1 else "stop"
        return FakeLLM.complete(truncated, system, user, purpose=purpose, max_tokens=max_tokens)

    truncated.complete = complete  # type: ignore[method-assign]
    stats = _translator(truncated).translate(document)

    # оборванный ответ и три запроса по частям
    assert calls == 4
    assert stats.untranslated == 0
    assert document.blocks[0].text == " ".join([RUSSIAN] * 3)  # type: ignore[union-attr]


def test_batch_with_missing_lines_is_finished_one_by_one() -> None:
    document = _document(
        Heading(level=1, text="Introduction"),
        Heading(level=1, text="Related work"),
        Heading(level=1, text="Introduction"),
        title="T",
    )
    llm = FakeLLM(["1. Введение\nкакой-то текст без номера", "Смежные работы"])

    stats = _translator(llm).translate(document)

    assert [block.text for block in document.blocks] == [  # type: ignore[union-attr]
        "Введение",
        "Смежные работы",
        "Введение",
    ]
    # одинаковые строки переводятся один раз
    assert "1. Introduction\n2. Related work" in str(llm.calls[0]["user"])
    assert len(llm.calls) == 2
    assert stats.translated == 3


def test_pdf_formula_is_restored_from_extracted_text() -> None:
    restored = Formula(math=None, number="(1)", source_text="zt = xt − µ σ")
    broken = Formula(math=None, source_text="a b")
    ready = Formula(math=Math(latex="x"), number="(3)")
    document = _document(
        Paragraph(text="A straightforward choice is standardization:"),
        restored,
        broken,
        ready,
        title="T",
    )
    llm = FakeLLM(["$$ z_t = \\frac{x_t - \\mu}{\\sigma} $$", "\\frac{a}{", "\\frac{a}{"])

    stats = _translator(llm, language="en").translate(document)

    assert restored.math is not None
    assert restored.math.latex == "z_t = \\frac{x_t - \\mu}{\\sigma}"
    assert "<mfrac>" in (restored.math.mathml or "")
    # формула, которую модель дважды записала с ошибкой, остаётся только картинкой
    assert broken.math is None
    assert ready.math is not None and ready.math.latex == "x"
    assert (stats.formulas, stats.formulas_restored) == (3, 1)
    formula_calls = [call for call in llm.calls if call["purpose"] == "formula"]
    assert len(formula_calls) == 3
    assert "A straightforward choice" in str(formula_calls[0]["user"])
    assert "формула не разобрана" in str(formula_calls[2]["user"])


def test_free_text_language_goes_to_prompt_as_is() -> None:
    document = _document(Paragraph(text=SOURCE), title="T")
    llm = FakeLLM(["Le piccole imbarcazioni sono intrinsecamente soggette alle perturbazioni."])

    _translator(llm, language="итальянский").translate(document)

    assert "на язык «итальянский»" in str(llm.calls[0]["user"])
    assert document.blocks[0].text.startswith("Le piccole")  # type: ignore[union-attr]


def test_helpers() -> None:
    assert clean_translation("<think>hm</think>Перевод: **Текст**\n\nвторой") == "Текст второй"
    assert clean_translation("<think>не закончил") == ""
    assert clean_translation("<<<\nТекст\n>>>") == "Текст"
    assert not needs_translation("7.5")
    assert not needs_translation("<f3/>")
    assert not needs_translation("RMSE@10")
    assert needs_translation("Length of the hull")
    parts = split_text("One sentence here. Another sentence there. And a third one.", 30)
    assert parts == ["One sentence here.", "Another sentence there.", "And a third one."]
    assert all(len(part) <= 30 for part in parts)
