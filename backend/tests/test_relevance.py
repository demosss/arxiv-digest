import pytest

from app.models import Relevance
from app.services.relevance import RelevanceJudge, parse_verdict
from tests.factories import FakeLLM


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"relevance": "yes", "reason": "Статья про ODD."}', Relevance.RELEVANT),
        ('{"relevance": "partial", "reason": "Смежная тема."}', Relevance.PARTIAL),
        ('{"relevance": "no", "reason": "Другая область."}', Relevance.IRRELEVANT),
        (
            '<think>hm</think>\n```json\n{"relevance": "No", "reason": "x"}\n```',
            Relevance.IRRELEVANT,
        ),
        ('Ответ: {"relevance": "частично", "reason": "x"}', Relevance.PARTIAL),
    ],
)
def test_verdict_is_parsed(text: str, expected: Relevance) -> None:
    verdict = parse_verdict(text)

    assert verdict is not None
    assert verdict.relevance == expected


@pytest.mark.parametrize(
    "text",
    ["", "Статья релевантна.", '{"relevance": "maybe", "reason": "x"}', '{"relevant": true', "[1]"],
)
def test_answer_out_of_format_is_not_a_verdict(text: str) -> None:
    assert parse_verdict(text) is None


def test_reason_is_collapsed_and_limited() -> None:
    verdict = parse_verdict('{"relevance": "no", "reason": "  много   слов ' + "я" * 900 + '"}')

    assert verdict is not None
    assert verdict.reason.startswith("много слов ")
    assert len(verdict.reason) == 500


def test_judge_sends_topic_and_paper_and_retries_once() -> None:
    llm = FakeLLM(["не знаю", '{"relevance": "no", "reason": "Математика."}'])

    verdict = RelevanceJudge(llm).judge(
        topic="ODD", description="Operational Design Domain", title="Odd cycles", abstract="Graphs."
    )

    assert verdict is not None
    assert (verdict.relevance, verdict.reason) == (Relevance.IRRELEVANT, "Математика.")
    assert [call["purpose"] for call in llm.calls] == ["relevance", "relevance"]
    first, second = (call["user"] for call in llm.calls)
    for fragment in ("Тема дайджеста: ODD", "Operational Design Domain", "Odd cycles", "Graphs."):
        assert fragment in first
    assert "Предыдущий ответ отклонён" in second


def test_judge_gives_up_after_two_invalid_answers() -> None:
    llm = FakeLLM(["не знаю", "всё ещё не знаю"])

    assert RelevanceJudge(llm).judge(topic="ODD", description=None, title="T", abstract="A") is None
    assert len(llm.calls) == 2
