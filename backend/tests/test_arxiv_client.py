from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.services.arxiv_client import (
    MAX_ATTEMPTS,
    ArxivClient,
    ArxivError,
    ArxivUnavailableError,
    parse_feed,
    parse_retry_after,
    pdf_filename,
)

FIXTURES = Path(__file__).parent / "fixtures"
FEED = (FIXTURES / "arxiv_feed.xml").read_bytes()
EMPTY_FEED = (FIXTURES / "arxiv_feed_empty.xml").read_bytes()
START = datetime(2024, 10, 5, 0, 0, tzinfo=UTC)
END = datetime(2024, 10, 5, 23, 59, 59, tzinfo=UTC)


def _feed(ids: list[str], total: int) -> bytes:
    entries = "".join(
        f"""<entry>
          <id>http://arxiv.org/abs/{arxiv_id}v1</id>
          <updated>2024-10-05T10:00:00Z</updated>
          <published>2024-10-05T10:00:00Z</published>
          <title>Paper {arxiv_id}</title>
          <summary>Abstract</summary>
          <author><name>Author</name></author>
        </entry>"""
        for arxiv_id in ids
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
      <opensearch:totalResults>{total}</opensearch:totalResults>
      {entries}
    </feed>""".encode()


def _client(handler: httpx.MockTransport, sleeps: list[float] | None = None) -> ArxivClient:
    recorded = sleeps if sleeps is not None else []
    return ArxivClient(
        httpx.Client(transport=handler),
        request_delay=0,
        user_agent="arxiv-digest-tests/1.0",
        sleep=recorded.append,
        backoff_base=1,
        overload_backoff_base=10,
    )


# --- парсинг Atom ---


def test_parse_feed_fixture() -> None:
    entries, total = parse_feed(FEED)

    assert total == 3
    assert [e.arxiv_id for e in entries] == ["2410.04321", "2410.04000", "hep-th/9901001"]

    first = entries[0]
    assert first.version == 2
    assert first.title == "Scaling Large Language Model Agents: A Systematic Study"
    assert first.authors == ["Alice Smith", "Bob Jones"]
    assert first.abstract.startswith("We study how large language model agents scale")
    assert "\n" not in first.abstract
    assert first.categories == ["cs.CL", "cs.AI"]
    assert first.published_at == datetime(2024, 10, 5, 17, 59, 58, tzinfo=UTC)
    assert first.updated_at == datetime(2024, 10, 7, 9, 15, tzinfo=UTC)
    assert first.pdf_url == "https://arxiv.org/pdf/2410.04321v2"
    assert first.abs_url == "https://arxiv.org/abs/2410.04321"

    old_style = entries[2]
    assert old_style.version == 3
    # ссылки на pdf в записи нет — адрес собирается из идентификатора
    assert old_style.pdf_url == "https://arxiv.org/pdf/hep-th/9901001v3"
    assert pdf_filename(old_style.arxiv_id) == "hep-th_9901001.pdf"


def test_parse_empty_feed() -> None:
    assert parse_feed(EMPTY_FEED) == ([], 0)


def test_parse_feed_raises_on_api_error_entry() -> None:
    error_feed = b"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>http://arxiv.org/api/errors#incorrect_id_format_for_1234</id>
        <title>Error</title>
        <summary>incorrect id format for 1234</summary>
      </entry>
    </feed>"""

    with pytest.raises(ArxivError, match="incorrect id format"):
        parse_feed(error_feed)


def test_parse_feed_raises_on_garbage() -> None:
    with pytest.raises(ArxivError):
        parse_feed(b"<html><body>Rate exceeded.</body></html")


# --- HTTP: пагинация, ретраи, скачивание ---


def test_search_paginates_by_100_and_sends_user_agent() -> None:
    requests: list[httpx.Request] = []
    ids = [f"2410.{i:05d}" for i in range(230)]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        start = int(request.url.params["start"])
        size = int(request.url.params["max_results"])
        return httpx.Response(200, content=_feed(ids[start : start + size], total=len(ids)))

    results = _client(httpx.MockTransport(handler)).search("all:LLM", max_results=500)

    assert [e.arxiv_id for e in results] == ids
    assert [(r.url.params["start"], r.url.params["max_results"]) for r in requests] == [
        ("0", "100"),
        ("100", "100"),
        ("200", "100"),
    ]
    first = requests[0]
    assert first.url.host == "export.arxiv.org"
    assert first.url.params["search_query"] == "all:LLM"
    assert first.url.params["sortBy"] == "submittedDate"
    assert first.url.params["sortOrder"] == "descending"
    assert first.headers["User-Agent"] == "arxiv-digest-tests/1.0"


def test_search_respects_max_results() -> None:
    ids = [f"2410.{i:05d}" for i in range(400)]
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        size = int(request.url.params["max_results"])
        sizes.append(size)
        return httpx.Response(200, content=_feed(ids[start : start + size], total=len(ids)))

    results = _client(httpx.MockTransport(handler)).search("all:LLM", max_results=150)

    assert len(results) == 150
    assert sizes == [100, 50]


def test_search_empty_period_is_not_an_error() -> None:
    client = _client(httpx.MockTransport(lambda request: httpx.Response(200, content=EMPTY_FEED)))

    assert client.search("all:LLM", max_results=200) == []


def test_search_retries_with_exponential_backoff() -> None:
    sleeps: list[float] = []
    responses = iter(
        [
            httpx.Response(503),
            httpx.ConnectError("connection refused"),
            httpx.Response(200, content=_feed(["2410.00001"], total=1)),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        item = next(responses)
        if isinstance(item, Exception):
            raise item
        return item

    results = _client(httpx.MockTransport(handler), sleeps).search("all:LLM", max_results=10)

    assert [e.arxiv_id for e in results] == ["2410.00001"]
    # 503 — перегрузка, пауза от overload_backoff_base; сетевая ошибка — от backoff_base
    assert sleeps == [10, 2]


def test_rate_limit_uses_overload_backoff() -> None:
    sleeps: list[float] = []

    with pytest.raises(ArxivUnavailableError, match="HTTP 429"):
        _client(httpx.MockTransport(lambda request: httpx.Response(429)), sleeps).search(
            "all:LLM", max_results=10
        )

    assert sleeps == [10, 20, 40]


def test_server_error_uses_regular_backoff() -> None:
    sleeps: list[float] = []

    with pytest.raises(ArxivUnavailableError, match="HTTP 502"):
        _client(httpx.MockTransport(lambda request: httpx.Response(502)), sleeps).search(
            "all:LLM", max_results=10
        )

    assert sleeps == [1, 2, 4]


def test_retry_after_extends_backoff() -> None:
    sleeps: list[float] = []
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "45"}),
            httpx.Response(200, content=_feed(["2410.00001"], total=1)),
        ]
    )
    client = _client(httpx.MockTransport(lambda request: next(responses)), sleeps)

    assert len(client.search("all:LLM", max_results=10)) == 1
    assert sleeps == [45]


def test_short_retry_after_does_not_shorten_backoff() -> None:
    sleeps: list[float] = []
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, content=_feed(["2410.00001"], total=1)),
        ]
    )
    client = _client(httpx.MockTransport(lambda request: next(responses)), sleeps)

    client.search("all:LLM", max_results=10)

    assert sleeps == [10]


def test_retry_after_is_capped() -> None:
    sleeps: list[float] = []
    responses = iter(
        [
            httpx.Response(503, headers={"Retry-After": "86400"}),
            httpx.Response(200, content=_feed(["2410.00001"], total=1)),
        ]
    )
    client = _client(httpx.MockTransport(lambda request: next(responses)), sleeps)

    client.search("all:LLM", max_results=10)

    assert sleeps == [300]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("120", 120.0),
        (" 7 ", 7.0),
        ("Mon, 05 Oct 2024 12:01:30 GMT", 90.0),
        ("Mon, 05 Oct 2024 11:00:00 GMT", 0.0),
        ("soon", None),
    ],
)
def test_parse_retry_after(value: str | None, expected: float | None) -> None:
    now = datetime(2024, 10, 5, 12, 0, tzinfo=UTC)

    assert parse_retry_after(value, now) == expected


def test_search_gives_up_after_max_attempts() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429)

    with pytest.raises(ArxivUnavailableError, match="HTTP 429"):
        _client(httpx.MockTransport(handler)).search("all:LLM", max_results=10)

    assert len(calls) == MAX_ATTEMPTS


def test_search_does_not_retry_client_errors() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(400)

    with pytest.raises(ArxivError, match="HTTP 400") as exc_info:
        _client(httpx.MockTransport(handler)).search("all:LLM", max_results=10)

    assert not isinstance(exc_info.value, ArxivUnavailableError)
    assert len(calls) == 1


def test_search_retries_spurious_empty_page() -> None:
    pages = iter([_feed([], total=2), _feed(["2410.00001", "2410.00002"], total=2)])
    client = _client(httpx.MockTransport(lambda request: httpx.Response(200, content=next(pages))))

    assert len(client.search("all:LLM", max_results=10)) == 2


def test_requests_are_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    ids = [f"2410.{i:05d}" for i in range(150)]

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        return httpx.Response(200, content=_feed(ids[start : start + 100], total=len(ids)))

    client = ArxivClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        request_delay=3,
        user_agent="tests",
        sleep=sleeps.append,
    )
    client.search("all:LLM", max_results=200)

    # пауза выдерживается перед вторым запросом
    assert len(sleeps) == 1
    assert 2.5 < sleeps[0] <= 3


def test_download_pdf(tmp_path: Path) -> None:
    body = b"%PDF-1.7\n" + b"x" * 2048
    client = _client(httpx.MockTransport(lambda request: httpx.Response(200, content=body)))
    destination = tmp_path / "papers" / "2410.00001.pdf"

    size = client.download_pdf("https://arxiv.org/pdf/2410.00001v1", destination)

    assert size == len(body)
    assert destination.read_bytes() == body
    assert not destination.with_suffix(".pdf.part").exists()


def test_download_rejects_non_pdf_response(tmp_path: Path) -> None:
    client = _client(
        httpx.MockTransport(lambda request: httpx.Response(200, content=b"<html>captcha</html>"))
    )
    destination = tmp_path / "2410.00001.pdf"

    with pytest.raises(ArxivError, match="не PDF"):
        client.download_pdf("https://arxiv.org/pdf/2410.00001v1", destination)

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []
