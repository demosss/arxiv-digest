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
from app.models import Digest, DigestPaper, DigestStatus, Paper, PaperStatus, PaperTopic, Topic
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


def split_summary(text: str) -> list[tuple[str, str]]:
    """Делит резюме на абзацы и отделяет названия разделов («Задача», «Метод», ...)."""
    paragraphs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        for section in SUMMARY_SECTIONS:
            prefix = f"{section}:"
            if line.lower().startswith(prefix.lower()):
                paragraphs.append((section, line[len(prefix) :].strip()))
                break
        else:
            paragraphs.append(("", line))
    return paragraphs


def select_papers(session: Session, start: datetime, end: datetime) -> list[Paper]:
    """Статьи периода с готовыми резюме, от новых к старым."""
    stmt = (
        select(Paper)
        .where(
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


def build_sections(papers: list[Paper], tz: ZoneInfo) -> list[DigestSection]:
    """Группирует статьи по темам. Статья выводится полностью в первой своей теме

    (темы упорядочены по названию), в остальных — ссылкой на неё.
    """
    topics: dict[int, Topic] = {}
    for paper in papers:
        for link in paper.topic_links:
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
        topic_ids = sorted((link.topic_id for link in paper.topic_links), key=order.__getitem__)
        primary = sections[topic_ids[0]] if topic_ids else no_topic
        assert paper.summary is not None
        primary.items.append(
            DigestItem(
                paper_id=paper.id,
                arxiv_id=paper.arxiv_id,
                title=paper.title,
                anchor=anchor,
                is_reference=False,
                primary_section=primary.name,
                abs_url=paper.abs_url,
                authors=format_authors(paper.authors),
                categories=paper.categories,
                date=paper.published_at.astimezone(tz).strftime(DATE_FORMAT),
                summary=split_summary(paper.summary.text_ru),
            )
        )
        for topic_id in topic_ids[1:]:
            sections[topic_id].items.append(
                DigestItem(
                    paper_id=paper.id,
                    arxiv_id=paper.arxiv_id,
                    title=paper.title,
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
    digests_dir: Path,
    timezone: str,
) -> BuiltDigest:
    """Собирает дайджест за период. Повторный вызов обновляет ту же запись и перезаписывает PDF."""
    started = time.perf_counter()
    tz = ZoneInfo(timezone)

    digest = session.scalar(
        select(Digest).where(Digest.period_start == period_start, Digest.period_end == period_end)
    )
    if digest is None:
        digest = Digest(period_start=period_start, period_end=period_end)
        session.add(digest)
    digest.status = DigestStatus.BUILDING
    digest.error = None
    session.commit()

    try:
        papers = select_papers(session, period_start, period_end)
        sections = build_sections(papers, tz)
        html = render_html(
            sections,
            period_start=period_start,
            period_end=period_end,
            generated_at=datetime.now(UTC),
            papers_count=len(papers),
            tz=tz,
        )
        pdf_path = digests_dir / digest_filename(period_start, period_end)
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
    digest.status = DigestStatus.READY if papers else DigestStatus.EMPTY
    session.commit()

    duration_ms = round((time.perf_counter() - started) * 1000)
    log.info(
        "digest_built",
        digest_id=digest.id,
        status=digest.status.value,
        papers_count=digest.papers_count,
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
