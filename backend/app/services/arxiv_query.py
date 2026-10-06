"""Поисковые выражения arXiv API: нормализация, проверка и сборка итогового `search_query`.

Поддерживается подмножество синтаксиса arXiv: префиксы полей (`ti:`, `abs:`, `all:`, `au:`,
`cat:`, `co:`), операторы `AND` / `OR` / `ANDNOT` заглавными, скобки, фразы в двойных кавычках.
Строка без префиксов и операторов считается фразой: `autonomous ship` → `all:"autonomous ship"`.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

FIELD_PREFIXES = ("ti", "abs", "all", "au", "cat", "co")
OPERATORS = ("AND", "OR", "ANDNOT")
MAX_QUERY_LENGTH = 1000

_DATE_FORMAT = "%Y%m%d%H%M"
_DELIMITERS = '()"'


class TokenKind(StrEnum):
    FIELD = "field"  # префикс поля вместе с двоеточием: `ti:`
    WORD = "word"
    PHRASE = "phrase"  # текст в двойных кавычках
    OPERATOR = "operator"
    LPAREN = "lparen"
    RPAREN = "rparen"


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    text: str
    position: int
    end: int
    closed: bool = True  # для фразы: есть ли закрывающая кавычка


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class QueryIssue:
    """Замечание к запросу; `position` — индекс символа (с нуля) в нормализованной строке."""

    position: int
    message: str
    severity: Severity = Severity.ERROR


@dataclass(frozen=True, slots=True)
class QueryCheck:
    normalized: str
    errors: list[QueryIssue]
    warnings: list[QueryIssue]

    @property
    def ok(self) -> bool:
        return not self.errors


def tokenize(query: str) -> list[Token]:
    tokens: list[Token] = []
    index = 0
    length = len(query)
    while index < length:
        char = query[index]
        if char.isspace():
            index += 1
        elif char == "(":
            tokens.append(Token(TokenKind.LPAREN, char, index, index + 1))
            index += 1
        elif char == ")":
            tokens.append(Token(TokenKind.RPAREN, char, index, index + 1))
            index += 1
        elif char == '"':
            closing = query.find('"', index + 1)
            end = length if closing == -1 else closing + 1
            text = query[index + 1 : length if closing == -1 else closing]
            tokens.append(Token(TokenKind.PHRASE, text, index, end, closed=closing != -1))
            index = end
        else:
            end = index
            while end < length and not query[end].isspace() and query[end] not in _DELIMITERS:
                end += 1
            chunk = query[index:end]
            prefix, colon, value = chunk.partition(":")
            if colon:
                field_end = index + len(prefix) + 1
                tokens.append(Token(TokenKind.FIELD, prefix, index, field_end))
                if value:
                    tokens.append(Token(TokenKind.WORD, value, field_end, end))
            elif chunk in OPERATORS:
                tokens.append(Token(TokenKind.OPERATOR, chunk, index, end))
            else:
                tokens.append(Token(TokenKind.WORD, chunk, index, end))
            index = end
    return tokens


def normalize_query(query: str) -> str:
    """Убирает лишние пробелы; строку без префиксов и операторов превращает в `all:"фраза"`."""
    collapsed = " ".join(query.split())
    if not collapsed:
        return ""
    tokens = tokenize(collapsed)
    if any(token.kind in (TokenKind.FIELD, TokenKind.OPERATOR) for token in tokens):
        return collapsed
    phrase = " ".join(collapsed.replace('"', " ").split())
    # пустые кавычки и скобки остаются как есть, чтобы проверка сообщила о них
    return f'all:"{phrase}"' if any(char.isalnum() for char in phrase) else collapsed


def _is_lowercase_operator(token: Token) -> bool:
    return token.kind is TokenKind.WORD and token.text.upper() in OPERATORS


def validate_query(query: str) -> list[QueryIssue]:
    """Замечания к запросу (ошибки и предупреждения) с позициями в нормализованной строке."""
    normalized = normalize_query(query)
    if not normalized:
        return [QueryIssue(0, "Пустой запрос")]

    issues: list[QueryIssue] = []

    def error(position: int, message: str) -> None:
        issues.append(QueryIssue(position, message))

    def warning(position: int, message: str) -> None:
        issues.append(QueryIssue(position, message, Severity.WARNING))

    if len(normalized) > MAX_QUERY_LENGTH:
        error(MAX_QUERY_LENGTH, f"Запрос длиннее {MAX_QUERY_LENGTH} символов")

    tokens = tokenize(normalized)
    open_parens: list[int] = []
    # что стоит слева от текущей позиции: None — начало, иначе "operand" | "operator" | "lparen"
    previous: str | None = None
    last_operator: Token | None = None
    index = 0

    def operand(position: int) -> None:
        nonlocal previous
        if previous == "operand":
            warning(position, "Между условиями нет оператора: добавьте AND, OR или ANDNOT")
        previous = "operand"

    while index < len(tokens):
        token = tokens[index]
        index += 1

        if token.kind is TokenKind.PHRASE and not token.closed:
            error(token.position, "Незакрытая кавычка")
        if token.kind is TokenKind.PHRASE and token.closed and not token.text.strip():
            error(token.position, "Пустые кавычки")

        if token.kind is TokenKind.FIELD:
            if token.text not in FIELD_PREFIXES:
                hint = (
                    f": префиксы пишутся строчными ({token.text.lower()}:)"
                    if token.text.lower() in FIELD_PREFIXES
                    else f" (допустимы: {', '.join(p + ':' for p in FIELD_PREFIXES)})"
                )
                error(token.position, f"Неизвестный префикс «{token.text}:»{hint}")
            value = tokens[index] if index < len(tokens) else None
            if (
                value is not None
                and value.kind in (TokenKind.WORD, TokenKind.PHRASE)
                and value.position == token.end
            ):
                index += 1
                if value.kind is TokenKind.PHRASE and not value.closed:
                    error(value.position, "Незакрытая кавычка")
                elif value.kind is TokenKind.PHRASE and not value.text.strip():
                    error(value.position, "Пустые кавычки")
            else:
                error(
                    token.end,
                    f"После «{token.text}:» нужно слово или фраза в кавычках, без пробела",
                )
            operand(token.position)

        elif token.kind is TokenKind.OPERATOR:
            if previous is None:
                error(token.position, f"Оператор {token.text} в начале выражения")
            elif previous == "operator":
                error(token.position, f"Два оператора подряд: перед {token.text} нет условия")
            elif previous == "lparen":
                error(token.position, f"Оператор {token.text} сразу после открывающей скобки")
            previous = "operator"
            last_operator = token

        elif token.kind is TokenKind.LPAREN:
            if previous == "operand":
                warning(
                    token.position, "Между условиями нет оператора: добавьте AND, OR или ANDNOT"
                )
            open_parens.append(token.position)
            previous = "lparen"

        elif token.kind is TokenKind.RPAREN:
            if not open_parens:
                error(token.position, "Лишняя закрывающая скобка")
            else:
                open_parens.pop()
                if previous == "lparen":
                    error(token.position, "Пустые скобки")
                elif previous == "operator" and last_operator is not None:
                    error(
                        last_operator.position,
                        f"После оператора {last_operator.text} нет условия",
                    )
            previous = "operand"

        elif _is_lowercase_operator(token):
            warning(
                token.position,
                f"Оператор «{token.text}» написан строчными и не сработает: "
                f"замените на {token.text.upper()}",
            )
            # дальше считаем его оператором, чтобы не плодить вторичные замечания
            previous = "operator"
            last_operator = None

        else:
            kind = "фразой" if token.kind is TokenKind.PHRASE else f"словом «{token.text}»"
            error(token.position, f"Перед {kind} нужен префикс поля, например all:")
            operand(token.position)

    for position in open_parens:
        error(position, "Незакрытая скобка")
    if previous == "operator" and last_operator is not None and tokens[-1] is last_operator:
        error(last_operator.position, f"Оператор {last_operator.text} в конце выражения")

    return sorted(issues, key=lambda issue: issue.position)


def check_query(query: str) -> QueryCheck:
    """Нормализует запрос и раскладывает замечания на ошибки и предупреждения."""
    issues = validate_query(query)
    return QueryCheck(
        normalized=normalize_query(query),
        errors=[i for i in issues if i.severity is Severity.ERROR],
        warnings=[i for i in issues if i.severity is Severity.WARNING],
    )


def format_issues(issues: list[QueryIssue]) -> str:
    return "; ".join(f"позиция {issue.position + 1}: {issue.message}" for issue in issues)


def format_period(period_start: datetime, period_end: datetime) -> str:
    start = period_start.astimezone(UTC).strftime(_DATE_FORMAT)
    end = period_end.astimezone(UTC).strftime(_DATE_FORMAT)
    return f"submittedDate:[{start} TO {end}]"


def build_search_query(
    query: str,
    categories: list[str],
    period_start: datetime,
    period_end: datetime,
) -> str:
    """`(<query>) AND (cat:A OR cat:B) AND submittedDate:[YYYYMMDDHHMM TO YYYYMMDDHHMM]` (UTC).

    URL-кодирование выполняет HTTP-клиент (`arxiv_client.build_query_url`).
    """
    parts = [f"({normalize_query(query)})"]
    if categories:
        parts.append(f"({' OR '.join(f'cat:{c}' for c in categories)})")
    parts.append(format_period(period_start, period_end))
    return " AND ".join(parts)
