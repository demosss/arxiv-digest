"""Суммаризация статьи: краткая аннотация и резюме полного текста (чанкинг и map-reduce)."""

import re
from dataclasses import dataclass
from typing import Protocol

from app.core.logging import get_logger
from app.services import prompts
from app.services.latex_text import latex_to_text
from app.services.llm_client import LLMError, LLMResponse

log = get_logger(__name__)

CHUNK_OVERLAP_RATIO = 0.05
MIN_SUMMARY_CHARS = 200
MIN_NOTE_CHARS = 20
# доля кириллицы среди букв, ниже которой резюме считается написанным не по-русски
MIN_CYRILLIC_SHARE = 0.5
# сколько символов начала статьи просматривается в поисках авторской аннотации, если на arXiv её нет
ABSTRACT_SOURCE_CHARS = 6000

# модель иногда начинает аннотацию со слова «Аннотация:»
_ABSTRACT_LABEL_RE = re.compile(r"^аннотация\s*[:.—–-]\s*", re.IGNORECASE)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_SENTENCE_END_RE = re.compile(r"(?<=[.!?…])\s+")
_GARBAGE_RE = re.compile(
    r"^\W*(?:as an ai|as a language model|i'?m sorry|i am sorry|i cannot|i can'?t|sure[,!]|"
    r"certainly[,!]|here is|here's|как (?:ии|искусственный интеллект|языковая модель)|"
    r"я (?:не могу|являюсь)|извините|к сожалению, я|конечно[,!]|вот (?:резюме|краткое))",
    re.IGNORECASE,
)


class SummaryValidationError(LLMError):
    """Модель дважды вернула ответ, не прошедший проверку."""


class ChatModel(Protocol):
    model: str

    def complete(self, system: str, user: str, *, purpose: str) -> LLMResponse: ...


@dataclass(frozen=True, slots=True)
class AbstractResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int
    llm_calls: int


@dataclass(frozen=True, slots=True)
class SummaryResult:
    text: str
    # краткая аннотация одним абзацем
    abstract: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int
    chunks_count: int
    llm_calls: int


def _split_long_unit(unit: str, limit: int) -> list[str]:
    """Режет слишком длинный абзац по предложениям, а если и это не помогает — по пробелам."""
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END_RE.split(unit):
        while len(sentence) > limit:
            if current:
                pieces.append(current)
                current = ""
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > limit:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def _overlap_tail(chunk: str, overlap: int) -> str:
    """Хвост предыдущего чанка, с которого начинается следующий; обрезается по границе слова."""
    if overlap <= 0 or len(chunk) <= overlap:
        return ""
    tail = chunk[-overlap:]
    space = tail.find(" ")
    return tail[space + 1 :].strip() if space != -1 else tail


def split_into_chunks(
    text: str, chunk_chars: int, overlap_ratio: float = CHUNK_OVERLAP_RATIO
) -> list[str]:
    """Делит текст на чанки не длиннее `chunk_chars` по границам абзацев с перекрытием ~5%."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [text]

    overlap = int(chunk_chars * overlap_ratio)
    # запас под перекрытие и разделитель, чтобы чанк гарантированно уложился в лимит
    unit_limit = chunk_chars - overlap - 2
    units: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        units.extend(
            [paragraph] if len(paragraph) <= unit_limit else _split_long_unit(paragraph, unit_limit)
        )

    chunks: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + 2 + len(unit) > chunk_chars:
            chunks.append(current)
            tail = _overlap_tail(current, overlap)
            current = f"{tail}\n\n{unit}" if tail else unit
        else:
            current = f"{current}\n\n{unit}" if current else unit
    if current:
        chunks.append(current)
    return chunks


def clean_response(text: str) -> str:
    """Убирает рассуждения <think>, Markdown, код формул LaTeX и лишние пустые строки."""
    text = _THINK_RE.sub("", text)
    # незакрытый <think>: модель упёрлась в лимит токенов, не дойдя до ответа
    if "<think>" in text.lower():
        return ""
    text = re.sub(r"^```\w*\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"(\*\*|__)", "", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = latex_to_text(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _cyrillic_share(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    return sum("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in letters) / len(letters)


def validate_response(text: str, *, final: bool) -> str | None:
    """Причина, по которой ответ не годится, или None. `final` — итоговое резюме, а не конспект."""
    if not text:
        return "ответ пустой"
    if _GARBAGE_RE.match(text):
        return "ответ содержит служебную фразу вместо текста по существу"
    if len(text) < (MIN_SUMMARY_CHARS if final else MIN_NOTE_CHARS):
        return "ответ слишком короткий"
    if final and _cyrillic_share(text) < MIN_CYRILLIC_SHARE:
        return "резюме должно быть написано на русском языке"
    return None


class Summarizer:
    def __init__(self, llm: ChatModel, *, context_chars: int) -> None:
        self._llm = llm
        self._context_chars = context_chars

    def summarize(self, title: str, abstract: str, full_text: str) -> SummaryResult:
        usage = _Usage()
        chunks = split_into_chunks(full_text, self._context_chars)
        if not chunks:
            raise LLMError("У статьи нет текста для суммаризации")
        log.info("summarize_started", chunks=len(chunks), text_chars=len(full_text))

        # аннотация — один короткий запрос: если модель не справится, map-reduce не тратится зря
        abstract_ru = self._abstract(title, abstract, full_text, usage)
        if len(chunks) == 1:
            prompt = prompts.SINGLE_PROMPT.format(title=title, abstract=abstract, text=chunks[0])
            text = self._ask(prompt, purpose="summary", final=True, usage=usage)
        else:
            notes = [
                self._ask(
                    prompts.MAP_PROMPT.format(
                        title=title, index=index, total=len(chunks), text=chunk
                    ),
                    purpose=f"map {index}/{len(chunks)}",
                    final=False,
                    usage=usage,
                )
                for index, chunk in enumerate(chunks, start=1)
            ]
            notes = self._collapse(title, notes, usage)
            prompt = prompts.REDUCE_PROMPT.format(
                title=title, abstract=abstract, notes=_join_notes(notes)
            )
            text = self._ask(prompt, purpose="reduce", final=True, usage=usage)

        log.info(
            "summarize_done",
            chunks=len(chunks),
            llm_calls=usage.calls,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            duration_ms=usage.duration_ms,
            summary_chars=len(text),
        )
        return SummaryResult(
            text=text,
            abstract=abstract_ru,
            model=self._llm.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            duration_ms=usage.duration_ms,
            chunks_count=len(chunks),
            llm_calls=usage.calls,
        )

    def summarize_abstract(self, title: str, abstract: str, full_text: str) -> AbstractResult:
        """Только краткая аннотация — для статей, у которых резюме уже есть."""
        usage = _Usage()
        text = self._abstract(title, abstract, full_text, usage)
        return AbstractResult(
            text=text,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            duration_ms=usage.duration_ms,
            llm_calls=usage.calls,
        )

    def _abstract(self, title: str, abstract: str, full_text: str, usage: "_Usage") -> str:
        """Аннотация одним абзацем: по аннотации с arXiv, а если её нет — по началу статьи."""
        if abstract.strip():
            prompt = prompts.ABSTRACT_PROMPT.format(title=title, abstract=abstract.strip())
        else:
            head = full_text.strip()[: min(ABSTRACT_SOURCE_CHARS, self._context_chars)]
            if not head:
                raise LLMError("У статьи нет ни аннотации, ни текста для суммаризации")
            prompt = prompts.ABSTRACT_FROM_TEXT_PROMPT.format(title=title, text=head)
        text = self._ask(prompt, purpose="abstract", final=True, usage=usage)
        return _ABSTRACT_LABEL_RE.sub("", " ".join(text.split()))

    def _collapse(self, title: str, notes: list[str], usage: "_Usage") -> list[str]:
        """Пока конспекты не помещаются в один запрос, сжимает их группами."""
        while len(notes) > 1 and len(_join_notes(notes)) > self._context_chars:
            groups: list[list[str]] = [[]]
            for note in notes:
                if groups[-1] and len(_join_notes([*groups[-1], note])) > self._context_chars:
                    groups.append([])
                groups[-1].append(note)
            if len(groups) == len(notes):
                # каждый конспект сам занимает весь контекст — дальше сжимать нечем
                break
            log.info("summarize_collapse", notes=len(notes), groups=len(groups))
            notes = [
                group[0]
                if len(group) == 1
                else self._ask(
                    prompts.COLLAPSE_PROMPT.format(title=title, notes=_join_notes(group)),
                    purpose=f"collapse {index}/{len(groups)}",
                    final=False,
                    usage=usage,
                )
                for index, group in enumerate(groups, start=1)
            ]
        return notes

    def _ask(self, prompt: str, *, purpose: str, final: bool, usage: "_Usage") -> str:
        """Запрос с проверкой ответа; при невалидном ответе — один повтор."""
        reason = ""
        for attempt in (1, 2):
            user = prompt if attempt == 1 else prompt + prompts.RETRY_SUFFIX.format(reason=reason)
            response = self._llm.complete(prompts.SYSTEM_PROMPT, user, purpose=purpose)
            usage.add(response)
            text = clean_response(response.text)
            problem = validate_response(text, final=final)
            if problem is None:
                return text
            reason = problem
            log.warning("llm_invalid_response", purpose=purpose, attempt=attempt, reason=reason)
        raise SummaryValidationError(f"Модель дважды вернула некорректный ответ: {reason}")


def _join_notes(notes: list[str]) -> str:
    return "\n\n".join(f"[Фрагмент {index}]\n{note}" for index, note in enumerate(notes, start=1))


class _Usage:
    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.duration_ms = 0
        self.calls = 0

    def add(self, response: LLMResponse) -> None:
        self.prompt_tokens += response.prompt_tokens
        self.completion_tokens += response.completion_tokens
        self.duration_ms += response.duration_ms
        self.calls += 1
