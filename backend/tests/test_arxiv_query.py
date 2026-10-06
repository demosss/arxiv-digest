from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

from app.services.arxiv_client import build_query_url
from app.services.arxiv_query import (
    MAX_QUERY_LENGTH,
    Severity,
    build_search_query,
    check_query,
    normalize_query,
    validate_query,
)

START = datetime(2024, 10, 5, 0, 0, tzinfo=UTC)
END = datetime(2024, 10, 5, 23, 59, 59, tzinfo=UTC)
PERIOD_CLAUSE = "submittedDate:[202410050000 TO 202410052359]"


# --- normalize_query ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("autonomous ship", 'all:"autonomous ship"'),
        ("COLREG", 'all:"COLREG"'),
        ('"autonomous ship"', 'all:"autonomous ship"'),
        ("  autonomous \t  ship \n", 'all:"autonomous ship"'),
        ("cats and dogs", 'all:"cats and dogs"'),
        ('all:"operational design domain"', 'all:"operational design domain"'),
        (
            "(ti:dataset OR ti:benchmark) AND (all:railway OR all:rail)",
            "(ti:dataset OR ti:benchmark) AND (all:railway OR all:rail)",
        ),
        ("  ti:dataset   AND\tall:railway ", "ti:dataset AND all:railway"),
        ("all:COLREG", "all:COLREG"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_query(raw: str, expected: str) -> None:
    assert normalize_query(raw) == expected


def test_normalize_is_idempotent() -> None:
    once = normalize_query("autonomous  ship")

    assert normalize_query(once) == once


# --- validate_query ---


def _errors(query: str) -> list[tuple[int, str]]:
    return [
        (issue.position, issue.message)
        for issue in validate_query(query)
        if issue.severity is Severity.ERROR
    ]


@pytest.mark.parametrize(
    "query",
    [
        "autonomous ship",
        'all:"operational design domain"',
        "all:COLREG OR all:COLREGs",
        "(ti:dataset OR ti:benchmark) AND (all:railway OR all:rail)",
        'abs:ODD AND (abs:autonomous OR abs:automated OR abs:"self-driving")',
        'all:"safety case" ANDNOT (cat:cs.CL OR au:smith)',
        '((ti:a OR ti:b) AND (abs:c ANDNOT co:"10 pages")) OR cat:eess.SY',
        'all:"ISO 21448"',
    ],
)
def test_valid_queries_have_no_issues(query: str) -> None:
    assert validate_query(query) == []


@pytest.mark.parametrize(
    ("query", "position", "fragment"),
    [
        ("(ti:dataset OR ti:benchmark", 0, "Незакрытая скобка"),
        ("ti:dataset) AND all:rail", 10, "Лишняя закрывающая скобка"),
        ('ti:"autonomous ship AND all:rail', 3, "Незакрытая кавычка"),
        ("title:dataset AND all:rail", 0, "Неизвестный префикс «title:»"),
        ("TI:dataset AND all:rail", 0, "префиксы пишутся строчными"),
        ("ti:dataset AND ()", 16, "Пустые скобки"),
        ('ti:"" AND all:rail', 3, "Пустые кавычки"),
        ("AND ti:dataset", 0, "в начале выражения"),
        ("ti:dataset OR", 11, "в конце выражения"),
        ("ti:dataset AND OR all:rail", 15, "Два оператора подряд"),
        ("(AND ti:dataset)", 1, "сразу после открывающей скобки"),
        ("(ti:dataset OR) AND all:rail", 12, "нет условия"),
        ("ti: AND all:rail", 3, "нужно слово или фраза"),
        ("ti:dataset AND railway", 15, "нужен префикс поля"),
        ("", 0, "Пустой запрос"),
        ("   ", 0, "Пустой запрос"),
    ],
)
def test_validation_errors_with_position(query: str, position: int, fragment: str) -> None:
    errors = _errors(query)

    assert any(pos == position and fragment in message for pos, message in errors), errors


def test_too_long_query_is_rejected() -> None:
    query = "all:" + "a" * MAX_QUERY_LENGTH

    assert any("длиннее 1000" in message for _, message in _errors(query))
    assert _errors("all:" + "a" * (MAX_QUERY_LENGTH - 4)) == []


def test_lowercase_operator_is_a_warning_with_suggestion() -> None:
    check = check_query("ti:dataset and all:railway or all:rail")

    assert check.errors == []
    assert [(w.position, w.message) for w in check.warnings] == [
        (11, "Оператор «and» написан строчными и не сработает: замените на AND"),
        (27, "Оператор «or» написан строчными и не сработает: замените на OR"),
    ]


def test_missing_operator_is_a_warning() -> None:
    check = check_query("ti:dataset all:railway")

    assert check.ok
    assert [w.position for w in check.warnings] == [11]


def test_check_query_returns_normalized_form() -> None:
    check = check_query('  "autonomous   ship" ')

    assert check.normalized == 'all:"autonomous ship"'
    assert check.ok and check.warnings == []


# --- build_search_query ---


def test_search_query_with_categories() -> None:
    query = build_search_query(
        "(ti:dataset OR ti:benchmark) AND all:railway", ["cs.CV", "cs.RO"], START, END
    )

    assert query == (
        "((ti:dataset OR ti:benchmark) AND all:railway)"
        f" AND (cat:cs.CV OR cat:cs.RO) AND {PERIOD_CLAUSE}"
    )


def test_search_query_without_categories_normalizes_phrase() -> None:
    assert build_search_query("autonomous ship", [], START, END) == (
        f'(all:"autonomous ship") AND {PERIOD_CLAUSE}'
    )


def test_search_query_period_is_converted_to_utc() -> None:
    moscow = ZoneInfo("Europe/Moscow")

    query = build_search_query(
        "all:LLM",
        [],
        datetime(2024, 10, 5, 3, 0, tzinfo=moscow),
        datetime(2024, 10, 6, 2, 59, tzinfo=moscow),
    )

    assert query.endswith(PERIOD_CLAUSE)


def test_search_query_is_url_encoded() -> None:
    search_query = build_search_query('(ti:dataset OR all:"rail way")', ["cs.CV"], START, END)

    url = build_query_url(search_query, start=100, max_results=50)

    raw_query = urlsplit(url).query
    for char in ' "():[]':
        assert char not in raw_query
    assert "%22rail+way%22" in raw_query or "%22rail%20way%22" in raw_query
    assert "%28" in raw_query and "%29" in raw_query and "%3A" in raw_query
    assert parse_qs(raw_query) == {
        "search_query": [search_query],
        "start": ["100"],
        "max_results": ["50"],
        "sortBy": ["submittedDate"],
        "sortOrder": ["descending"],
    }
