"""Оценка релевантности: LLM по названию и аннотации решает, относится ли статья к теме."""

import json
import re
from dataclasses import dataclass

from app.core.logging import get_logger
from app.models import Relevance
from app.services import prompts
from app.services.llm_summarizer import ChatModel

log = get_logger(__name__)

MAX_REASON_CHARS = 500

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_LEVELS = {
    "yes": Relevance.RELEVANT,
    "да": Relevance.RELEVANT,
    "relevant": Relevance.RELEVANT,
    "partial": Relevance.PARTIAL,
    "partially": Relevance.PARTIAL,
    "частично": Relevance.PARTIAL,
    "no": Relevance.IRRELEVANT,
    "нет": Relevance.IRRELEVANT,
    "irrelevant": Relevance.IRRELEVANT,
}


@dataclass(frozen=True, slots=True)
class RelevanceVerdict:
    relevance: Relevance
    reason: str


def parse_verdict(text: str) -> RelevanceVerdict | None:
    """Разбирает ответ модели; None — ответ не в формате `{"relevance": ..., "reason": ...}`."""
    match = _JSON_RE.search(_THINK_RE.sub("", text))
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    relevance = _LEVELS.get(str(data.get("relevance", "")).strip().lower())
    if relevance is None:
        return None
    reason = " ".join(str(data.get("reason") or "").split())
    return RelevanceVerdict(relevance=relevance, reason=reason[:MAX_REASON_CHARS])


class RelevanceJudge:
    def __init__(self, llm: ChatModel) -> None:
        self._llm = llm

    def judge(
        self, *, topic: str, description: str | None, title: str, abstract: str
    ) -> RelevanceVerdict | None:
        """Оценка статьи для темы. None — модель дважды ответила не по формату:

        такую статью нельзя отбрасывать, решение остаётся за запросами темы.
        """
        prompt = prompts.RELEVANCE_PROMPT.format(
            topic=topic,
            description=description or "нет",
            title=title,
            abstract=abstract,
        )
        reason = "ответ должен быть одной строкой JSON с полями relevance и reason"
        for attempt in (1, 2):
            user = prompt if attempt == 1 else prompt + prompts.RETRY_SUFFIX.format(reason=reason)
            response = self._llm.complete(
                prompts.RELEVANCE_SYSTEM_PROMPT, user, purpose="relevance"
            )
            verdict = parse_verdict(response.text)
            if verdict is not None:
                return verdict
            log.warning("llm_invalid_response", purpose="relevance", attempt=attempt, reason=reason)
        return None
