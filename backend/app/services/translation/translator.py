"""Перевод документа LLM: текст — по абзацам, короткие строки — пачками, формулы не меняются.

Строчные формулы в тексте стоят метками `<f12/>`: модель обязана вернуть их на месте.
Если она их теряет, абзац переводится ещё раз с формулами обычным текстом.
Абзац, который перевести не удалось, остаётся в документе на языке оригинала.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.logging import get_logger
from app.services.llm_client import LLMError, LLMResponse, LLMUnavailableError
from app.services.translation import prompts
from app.services.translation.languages import prompt_target
from app.services.translation.model import (
    MATH_TOKEN_RE,
    Document,
    Figure,
    Formula,
    Heading,
    ListItem,
    Math,
    Paragraph,
    Table,
    math_token,
)
from app.services.translation.omml import MathConversionError, clean_latex, latex_to_mathml

log = get_logger(__name__)

# короткие строки (заголовки, ячейки таблиц) переводятся пачками такого размера
BATCH_ITEMS = 20
SHORT_TEXT_CHARS = 120
FORMULA_MAX_TOKENS = 700
FORMULA_CONTEXT_CHARS = 400
# доля кириллицы среди букв, ниже которой перевод на русский считается невыполненным
MIN_CYRILLIC_SHARE = 0.3
PROGRESS_EVERY = 10

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_SENTENCE_END_RE = re.compile(r"(?<=[.!?…;])\s+")
_PREFIX_RE = re.compile(
    r"^(?:перевод|translation|übersetzung|traduction|traducción)\s*[:：]\s*", re.I
)
_BATCH_LINE_RE = re.compile(r"^\s*(\d{1,3})\s*[.):]\s*(.*\S)\s*$")
_LETTERS_RE = re.compile(r"[^\W\d_]{2,}")
# обозначение или аббревиатура без слов: «LSTM», «RMSE@10», «v2.1»
_IDENTIFIER_RE = re.compile(r"^[A-Z0-9\-_/.+@%()×·\s]+$")


class ChatModel(Protocol):
    model: str

    def complete(
        self, system: str, user: str, *, purpose: str, max_tokens: int | None = None
    ) -> LLMResponse: ...


class _Rejected(Exception):
    """Модель не дала пригодного перевода фрагмента."""


@dataclass
class TranslateStats:
    segments: int = 0
    translated: int = 0
    untranslated: int = 0
    formulas: int = 0
    formulas_restored: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))


def clean_translation(text: str) -> str:
    """Убирает рассуждения, ограничители из промпта, Markdown и подпись «Перевод:»."""
    text = _THINK_RE.sub("", text)
    if "<think>" in text.lower():
        return ""
    text = re.sub(r"^```\w*\s*$", "", text, flags=re.MULTILINE)
    text = text.replace("<<<", "").replace(">>>", "")
    text = re.sub(r"(\*\*|__)", "", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = _PREFIX_RE.sub("", text.strip())
    return re.sub(r"\s+", " ", text).strip()


def _token_ids(text: str) -> list[int]:
    return [int(match.group(1)) for match in MATH_TOKEN_RE.finditer(text)]


def _canonical_tokens(text: str) -> str:
    """Метки в ответе модели приводятся к единому виду: «< f3 >» → «<f3/>»."""
    return MATH_TOKEN_RE.sub(lambda match: math_token(int(match.group(1))), text)


def _cyrillic_share(text: str) -> float:
    letters = [ch for ch in MATH_TOKEN_RE.sub("", text) if ch.isalpha()]
    if not letters:
        return 1.0
    return sum("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in letters) / len(letters)


def needs_translation(text: str) -> bool:
    """Есть ли в строке слова: числа, формулы и обозначения переводить незачем."""
    plain = MATH_TOKEN_RE.sub(" ", text).strip()
    return bool(_LETTERS_RE.search(plain)) and not _IDENTIFIER_RE.fullmatch(plain)


def split_text(text: str, limit: int) -> list[str]:
    """Делит длинный абзац на части не длиннее `limit` по границам предложений."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for sentence in _SENTENCE_END_RE.split(text):
        while len(sentence) > limit:
            if current:
                parts.append(current)
                current = ""
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            parts.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > limit:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return parts


class Translator:
    def __init__(self, llm: ChatModel, *, language: str, chunk_chars: int, max_tokens: int) -> None:
        self._llm = llm
        self._language = language
        self._target = prompt_target(language)
        self._chunk_chars = chunk_chars
        self._max_tokens = max_tokens
        self._title = ""
        self._math: dict[int, Math] = {}
        self._cache: dict[str, str] = {}
        self.stats = TranslateStats()

    def translate(self, document: Document) -> TranslateStats:
        """Переводит документ на месте и возвращает статистику."""
        self._title = MATH_TOKEN_RE.sub("", document.title)[:300]
        self._math = document.math
        self._restore_formulas(document)

        Setter = Callable[[str], None]
        long: list[tuple[str, Setter]] = []
        short: list[tuple[str, Setter]] = []

        def add(text: str, setter: Setter, *, brief: bool = False) -> None:
            if not needs_translation(text):
                return
            target = short if brief or (len(text) <= SHORT_TEXT_CHARS and "." not in text) else long
            target.append((text, setter))

        def attr(obj: Any, name: str) -> Setter:
            return lambda value: setattr(obj, name, value)

        def item(items: list[str], index: int) -> Setter:
            return lambda value: items.__setitem__(index, value)

        add(document.title, attr(document, "title"))
        for index, paragraph in enumerate(document.abstract):
            add(paragraph, item(document.abstract, index))
        add(document.keywords, attr(document, "keywords"))
        for block in document.blocks:
            if isinstance(block, Heading):
                add(block.text, attr(block, "text"), brief=True)
            elif isinstance(block, Paragraph) and not block.preformatted:
                add(block.lead, attr(block, "lead"), brief=True)
                add(block.text, attr(block, "text"))
            elif isinstance(block, ListItem):
                add(block.text, attr(block, "text"))
            elif isinstance(block, Figure):
                add(block.caption, attr(block, "caption"))
            elif isinstance(block, Table):
                add(block.caption, attr(block, "caption"))
                for row in block.rows:
                    for cell in row:
                        add(cell.text, attr(cell, "text"), brief=True)

        self.stats.segments = len(long) + len(short)
        log.info(
            "translation_started",
            language=self._language,
            segments=self.stats.segments,
            short_segments=len(short),
            formulas=self.stats.formulas,
        )
        self._translate_short(short)
        for index, (text, setter) in enumerate(long, start=1):
            setter(self._translate_long(text))
            if index % PROGRESS_EVERY == 0:
                log.info("translation_progress", done=index, total=len(long))
        log.info("translation_done", **self.stats.as_dict())
        return self.stats

    # --- запросы к модели ---

    def _complete(self, user: str, *, purpose: str, max_tokens: int | None = None) -> LLMResponse:
        response = self._llm.complete(
            prompts.SYSTEM_PROMPT, user, purpose=purpose, max_tokens=max_tokens or self._max_tokens
        )
        self.stats.llm_calls += 1
        self.stats.prompt_tokens += response.prompt_tokens
        self.stats.completion_tokens += response.completion_tokens
        self.stats.llm_duration_ms += response.duration_ms
        return response

    def _problem(self, source: str, text: str, *, truncated: bool) -> str | None:
        if not text:
            return "ответ пустой"
        if truncated:
            return "перевод оборван: не хватило лимита токенов"
        expected, actual = set(_token_ids(source)), set(_token_ids(text))
        if expected != actual:
            missing = ", ".join(math_token(i) for i in sorted(expected - actual))
            extra = ", ".join(math_token(i) for i in sorted(actual - expected))
            details = "; ".join(
                filter(
                    None,
                    [f"пропущены {missing}" if missing else "", f"лишние {extra}" if extra else ""],
                )
            )
            return f"метки формул должны остаться без изменений ({details})"
        if len(source) >= 100 and len(text) < 0.35 * len(source):
            return "перевод слишком короткий: часть текста пропущена"
        if len(text) > 4 * len(source) + 200:
            return "перевод намного длиннее оригинала: в ответе есть лишний текст"
        if (
            self._language == "ru"
            and len(_LETTERS_RE.findall(source)) >= 8
            and _cyrillic_share(text) < MIN_CYRILLIC_SHARE
        ):
            return "текст не переведён на русский язык"
        return None

    def _ask(self, source: str, *, purpose: str = "translate") -> str:
        """Перевод одного фрагмента: запрос, проверка, один повтор с указанием ошибки."""
        prompt = prompts.TRANSLATE_PROMPT.format(
            target=self._target, title=self._title, text=source
        )
        reason = ""
        truncated = False
        for attempt in (1, 2):
            user = prompt if attempt == 1 else prompt + prompts.RETRY_SUFFIX.format(reason=reason)
            response = self._complete(user, purpose=purpose)
            text = _canonical_tokens(clean_translation(response.text))
            truncated = response.finish_reason == "length"
            problem = self._problem(source, text, truncated=truncated)
            if problem is None:
                return text
            reason = problem
            log.warning("translation_invalid_response", attempt=attempt, reason=reason)
            if truncated:
                break
        if truncated and len(source) > 400:
            # ответ не уместился в лимит токенов — фрагмент делится пополам
            halves = split_text(source, max(200, len(source) // 2 + 1))
            if len(halves) > 1:
                return " ".join(self._ask(half, purpose=purpose) for half in halves)
        if _token_ids(source) and "метки" in reason:
            # модель не удержала метки: формулы подставляются обычным текстом
            plain = MATH_TOKEN_RE.sub(lambda m: self._plain_math(int(m.group(1))), source)
            if not _token_ids(plain):
                log.info("translation_plain_formulas", formulas=len(_token_ids(source)))
                return self._ask(plain, purpose="translate plain")
        raise _Rejected(reason)

    def _plain_math(self, index: int) -> str:
        math = self._math.get(index)
        return (math.text if math else "") or "…"

    def _translate_long(self, text: str) -> str:
        cached = self._cache.get(text)
        if cached is not None:
            # повторяющийся фрагмент (подпись, строка таблицы) второй раз модели не отправляется
            self.stats.translated += 1
            return cached
        try:
            parts = [self._ask(part) for part in split_text(text, self._chunk_chars)]
        except _Rejected as exc:
            self.stats.untranslated += 1
            log.error("translation_segment_failed", reason=str(exc), chars=len(text))
            return text
        except LLMUnavailableError:
            raise
        except LLMError as exc:
            self.stats.untranslated += 1
            log.error("translation_segment_failed", reason=str(exc), chars=len(text))
            return text
        self.stats.translated += 1
        self._cache[text] = result = " ".join(parts)
        return result

    def _translate_short(self, items: list[tuple[str, Callable[[str], None]]]) -> None:
        """Короткие строки — пачками по номерам; что не разобралось, переводится по одной."""
        unique = list(dict.fromkeys(text for text, _ in items))
        for start in range(0, len(unique), BATCH_ITEMS):
            batch = unique[start : start + BATCH_ITEMS]
            for text, translated in zip(batch, self._batch(batch), strict=True):
                if translated is not None:
                    self._cache[text] = translated
        for text, setter in items:
            setter(self._translate_long(text))

    def _batch(self, batch: list[str]) -> list[str | None]:
        numbered = "\n".join(f"{index}. {text}" for index, text in enumerate(batch, start=1))
        prompt = prompts.BATCH_PROMPT.format(target=self._target, title=self._title, lines=numbered)
        try:
            response = self._complete(prompt, purpose="translate batch")
        except LLMUnavailableError:
            raise
        except LLMError as exc:
            log.warning("translation_batch_failed", reason=str(exc), items=len(batch))
            return [None] * len(batch)
        answers: dict[int, str] = {}
        text = _THINK_RE.sub("", response.text).replace("<<<", "").replace(">>>", "")
        for line in text.splitlines():
            match = _BATCH_LINE_RE.match(line)
            if match:
                answers.setdefault(int(match.group(1)), match.group(2))
        result: list[str | None] = []
        for index, source in enumerate(batch, start=1):
            answer = _canonical_tokens(clean_translation(answers.get(index, "")))
            ok = bool(answer) and self._problem(source, answer, truncated=False) is None
            result.append(answer if ok else None)
        missed = sum(answer is None for answer in result)
        if missed:
            log.warning("translation_batch_partial", items=len(batch), missed=missed)
        return result

    # --- формулы из PDF ---

    def _restore_formulas(self, document: Document) -> None:
        """Формулам, извлечённым из PDF текстом, LLM возвращает структуру в виде LaTeX."""
        context = ""
        for block in document.blocks:
            if isinstance(block, Paragraph):
                context = block.text
            if not isinstance(block, Formula):
                continue
            self.stats.formulas += 1
            if block.math is not None or not block.source_text.strip():
                continue
            block.math = self._restore_formula(block.source_text, context)
            if block.math is not None:
                self.stats.formulas_restored += 1

    def _restore_formula(self, source: str, context: str) -> Math | None:
        prompt = prompts.FORMULA_PROMPT.format(
            text=source, context=MATH_TOKEN_RE.sub("", context)[-FORMULA_CONTEXT_CHARS:]
        )
        reason = ""
        for attempt in (1, 2):
            user = prompt if attempt == 1 else prompt + prompts.RETRY_SUFFIX.format(reason=reason)
            try:
                response = self._complete(user, purpose="formula", max_tokens=FORMULA_MAX_TOKENS)
            except LLMUnavailableError:
                raise
            except LLMError as exc:
                log.warning("translation_formula_failed", reason=str(exc))
                return None
            latex = clean_latex(_THINK_RE.sub("", response.text).strip())
            try:
                return Math(mathml=latex_to_mathml(latex), latex=latex, text=source)
            except MathConversionError as exc:
                reason = f"формула не разобрана как LaTeX ({exc})"
                log.warning("translation_formula_invalid", attempt=attempt, reason=reason)
        return None
