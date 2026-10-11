"""Шаг build_digest: PDF-дайджест на русском (Jinja2 → WeasyPrint)."""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload
from weasyprint import HTML

from app.core.logging import get_logger
from app.models import (
    Digest,
    DigestContent,
    DigestPaper,
    DigestStatus,
    Paper,
    PaperStatus,
    PaperTopic,
    Relevance,
    Summary,
    Topic,
)
from app.services.files import profile_dir, remove_files
from app.services.latex_text import latex_to_text
from app.services.prompts import SUMMARY_SECTIONS

log = get_logger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = BACKEND_DIR / "app" / "templates"
FONTS_DIR = BACKEND_DIR / "assets" / "fonts"
DIGEST_TITLE = "Дайджест препринтов arXiv"
NO_TOPIC_SECTION = "Без темы"
MAX_AUTHORS = 5
DATE_FORMAT = "%d.%m.%Y"

_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


@dataclass
class DigestItem:
    """Статья в разделе темы: полная карточка либо ссылка на карточку в другом разделе."""

    paper_id: int
    arxiv_id: str
    title: str
    anchor: str
    is_reference: bool
    primary_section: str = ""
    abs_url: str = ""
    authors: str = ""
    categories: list[str] = field(default_factory=list)
    date: str = ""
    # краткая аннотация; пустая, если в карточке её нет
    abstract: str = ""
    summary: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class DigestSection:
    name: str
    anchor: str
    topic_id: int | None
    description: str | None
    items: list[DigestItem] = field(default_factory=list)

    @property
    def papers_count(self) -> int:
        return len(self.items)


@dataclass
class BuiltDigest:
    digest: Digest
    pdf_path: Path
    pdf_size: int
    duration_ms: int
    sections: list[DigestSection]

    def stats(self) -> dict[str, Any]:
        return {
            "digest_id": self.digest.id,
            "status": self.digest.status.value,
            "content": self.digest.content.value,
            "papers_count": self.digest.papers_count,
            "topics": len(self.sections),
            "pdf_path": str(self.pdf_path),
            "pdf_size_bytes": self.pdf_size,
            "build_duration_ms": self.duration_ms,
        }


def format_authors(authors: list[str]) -> str:
    """Первые пять авторов и «и др.», если их больше."""
    if len(authors) <= MAX_AUTHORS:
        return ", ".join(authors)
    return ", ".join(authors[:MAX_AUTHORS]) + " и др."


def format_period(start: datetime, end: datetime, tz: ZoneInfo) -> str:
    """Период для заголовка: целые сутки UTC — датами, иначе интервалом в таймзоне сервиса."""
    start_utc, end_utc = start.astimezone(UTC), end.astimezone(UTC)
    whole_days = start_utc.time() == dt_time(0, 0) and (
        end_utc + timedelta(seconds=1)
    ).time() == dt_time(0, 0)
    if whole_days:
        first, last = start_utc.strftime(DATE_FORMAT), end_utc.strftime(DATE_FORMAT)
        return f"{first} (UTC)" if first == last else f"{first} — {last} (UTC)"
    fmt = f"{DATE_FORMAT} %H:%M"
    zone = start.astimezone(tz).tzname() or str(tz)
    return f"{start.astimezone(tz).strftime(fmt)} — {end.astimezone(tz).strftime(fmt)} ({zone})"


def digest_filename(start: datetime, end: datetime) -> str:
    first, last = start.astimezone(UTC).date(), end.astimezone(UTC).date()
    if first == last:
        return f"digest_{first.isoformat()}.pdf"
    return f"digest_{first.isoformat()}_{last.isoformat()}.pdf"


def capitalize_first(text: str) -> str:
    """Начинает текст с заглавной буквы.

    Модель пишет «Задача: авторы исследуют…», и без названия раздела абзац начинался бы
    со строчной. Слово с заглавной буквой или цифрой внутри («mAP», «x86») не трогается:
    это обозначение, а не начало предложения.
    """
    words = text.split(maxsplit=1)
    if not words or not words[0][0].islower():
        return text
    if any(char.isupper() or char.isdigit() for char in words[0]):
        return text
    return text[0].upper() + text[1:]


def split_summary(text: str) -> list[tuple[str, str]]:
    """Делит резюме на абзацы и отделяет названия разделов («Задача», «Метод», ...).

    Текст каждого абзаца начинается с заглавной буквы — как и аннотация над ними.
    """
    paragraphs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        for section in SUMMARY_SECTIONS:
            prefix = f"{section}:"
            if line.lower().startswith(prefix.lower()):
                paragraphs.append((section, capitalize_first(line[len(prefix) :].strip())))
                break
        else:
            paragraphs.append(("", capitalize_first(line)))
    return paragraphs


def card_content(summary: Summary, content: DigestContent) -> tuple[str, list[tuple[str, str]]]:
    """Аннотация и разделы резюме для карточки статьи в зависимости от состава дайджеста.

    У резюме, созданного до появления аннотаций, её нет: в режиме «только аннотация»
    такая статья печатается разделами, чтобы карточка не осталась пустой.
    """
    abstract = ""
    if content != DigestContent.SUMMARY:
        abstract = capitalize_first(latex_to_text(summary.abstract_ru or ""))
    if content == DigestContent.ABSTRACT and abstract:
        return abstract, []
    return abstract, split_summary(latex_to_text(summary.text_ru))


def select_papers(session: Session, profile_id: int, start: datetime, end: datetime) -> list[Paper]:
    """Статьи профиля за период с готовыми резюме, от новых к старым."""
    stmt = (
        select(Paper)
        .where(
            Paper.profile_id == profile_id,
            Paper.status == PaperStatus.SUMMARIZED,
            Paper.published_at >= start,
            Paper.published_at <= end,
        )
        .options(
            selectinload(Paper.summary),
            selectinload(Paper.topic_links).selectinload(PaperTopic.topic),
        )
        .order_by(Paper.published_at.desc(), Paper.id)
    )
    return [paper for paper in session.scalars(stmt).all() if paper.summary is not None]


def _shown_links(paper: Paper) -> list[PaperTopic]:
    """Темы, в разделах которых печатается статья: без тех, для которых она нерелевантна."""
    return [link for link in paper.topic_links if link.relevance != Relevance.IRRELEVANT]


def build_sections(
    papers: list[Paper], tz: ZoneInfo, content: DigestContent = DigestContent.FULL
) -> list[DigestSection]:
    """Группирует статьи по темам. Статья выводится полностью в первой своей теме

    (темы упорядочены по названию), в остальных — ссылкой на неё.
    """
    topics: dict[int, Topic] = {}
    for paper in papers:
        for link in _shown_links(paper):
            topics[link.topic_id] = link.topic
    ordered = sorted(topics.values(), key=lambda t: (t.name.lower(), t.id))
    sections = {
        topic.id: DigestSection(
            name=topic.name,
            anchor=f"topic-{topic.id}",
            topic_id=topic.id,
            description=topic.description,
        )
        for topic in ordered
    }
    no_topic = DigestSection(
        name=NO_TOPIC_SECTION, anchor="topic-none", topic_id=None, description=None
    )
    order = {topic.id: index for index, topic in enumerate(ordered)}

    for paper in papers:
        anchor = f"paper-{paper.id}"
        title = latex_to_text(paper.title)
        topic_ids = sorted((link.topic_id for link in _shown_links(paper)), key=order.__getitem__)
        primary = sections[topic_ids[0]] if topic_ids else no_topic
        assert paper.summary is not None
        abstract, summary = card_content(paper.summary, content)
        primary.items.append(
            DigestItem(
                paper_id=paper.id,
                arxiv_id=paper.arxiv_id,
                title=title,
                anchor=anchor,
                is_reference=False,
                primary_section=primary.name,
                abs_url=paper.abs_url,
                authors=format_authors(paper.authors),
                categories=paper.categories,
                date=paper.published_at.astimezone(tz).strftime(DATE_FORMAT),
                abstract=abstract,
                summary=summary,
            )
        )
        for topic_id in topic_ids[1:]:
            sections[topic_id].items.append(
                DigestItem(
                    paper_id=paper.id,
                    arxiv_id=paper.arxiv_id,
                    title=title,
                    anchor=anchor,
                    is_reference=True,
                    primary_section=primary.name,
                )
            )

    result = [section for section in sections.values() if section.items]
    if no_topic.items:
        result.append(no_topic)
    return result


def render_html(
    sections: list[DigestSection],
    *,
    period_start: datetime,
    period_end: datetime,
    generated_at: datetime,
    papers_count: int,
    tz: ZoneInfo,
) -> str:
    return _env.get_template("digest.html.j2").render(
        title=DIGEST_TITLE,
        period_label=format_period(period_start, period_end, tz),
        generated_label=generated_at.astimezone(tz).strftime(f"{DATE_FORMAT} %H:%M (%Z)"),
        papers_count=papers_count,
        sections=sections,
        fonts_url=FONTS_DIR.as_uri(),
    )


def render_pdf(html: str, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_suffix(".pdf.part")
    try:
        HTML(string=html, base_url=str(TEMPLATES_DIR)).write_pdf(temp_path)
        temp_path.replace(destination)
    finally:
        temp_path.unlink(missing_ok=True)
    return destination.stat().st_size


def build_digest(
    session: Session,
    period_start: datetime,
    period_end: datetime,
    *,
    profile_id: int,
    digests_dir: Path,
    timezone: str,
    content: DigestContent = DigestContent.FULL,
) -> BuiltDigest:
    """Собирает дайджест профиля за период. Повторный вызов обновляет ту же запись и PDF.

    `content` — что печатать в карточке статьи: аннотацию, разделы резюме или всё вместе.
    """
    started = time.perf_counter()
    tz = ZoneInfo(timezone)

    digest = session.scalar(
        select(Digest).where(
            Digest.profile_id == profile_id,
            Digest.period_start == period_start,
            Digest.period_end == period_end,
        )
    )
    if digest is None:
        digest = Digest(profile_id=profile_id, period_start=period_start, period_end=period_end)
        session.add(digest)
    previous_pdf = digest.pdf_path
    digest.status = DigestStatus.BUILDING
    digest.error = None
    session.commit()

    try:
        papers = select_papers(session, profile_id, period_start, period_end)
        sections = build_sections(papers, tz, content)
        html = render_html(
            sections,
            period_start=period_start,
            period_end=period_end,
            generated_at=datetime.now(UTC),
            papers_count=len(papers),
            tz=tz,
        )
        pdf_path = profile_dir(digests_dir, profile_id) / digest_filename(period_start, period_end)
        pdf_size = render_pdf(html, pdf_path)
    except Exception as exc:
        session.rollback()
        digest.status = DigestStatus.FAILED
        digest.error = f"{type(exc).__name__}: {exc}"
        session.commit()
        log.error("digest_build_failed", digest_id=digest.id, error=digest.error)
        raise

    session.execute(delete(DigestPaper).where(DigestPaper.digest_id == digest.id))
    position = 0
    for section in sections:
        for item in section.items:
            if item.is_reference:
                continue
            position += 1
            session.add(
                DigestPaper(
                    digest_id=digest.id,
                    paper_id=item.paper_id,
                    position=position,
                    topic_id=section.topic_id,
                )
            )
    digest.pdf_path = str(pdf_path)
    digest.papers_count = len(papers)
    digest.content = content
    digest.status = DigestStatus.READY if papers else DigestStatus.EMPTY
    session.commit()
    if previous_pdf and Path(previous_pdf) != pdf_path:
        # дайджест собран до появления каталогов профилей: прежний файл больше не нужен
        remove_files([previous_pdf])

    duration_ms = round((time.perf_counter() - started) * 1000)
    log.info(
        "digest_built",
        digest_id=digest.id,
        status=digest.status.value,
        papers_count=digest.papers_count,
        content=content.value,
        topics=len(sections),
        pdf_path=str(pdf_path),
        pdf_size_bytes=pdf_size,
        duration_ms=duration_ms,
    )
    return BuiltDigest(
        digest=digest,
        pdf_path=pdf_path,
        pdf_size=pdf_size,
        duration_ms=duration_ms,
        sections=sections,
    )
