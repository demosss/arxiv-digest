from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import get_sync_engine


def _topics(columns: str) -> dict[str, Any]:
    with get_sync_engine().connect() as conn:
        rows = conn.execute(text(f"SELECT name, {columns} FROM topics ORDER BY name")).all()
    return {row[0]: row[1] for row in rows}


def test_keywords_to_queries_migration_roundtrip(db: Session) -> None:
    config = Config("alembic.ini")
    engine = get_sync_engine()
    try:
        command.downgrade(config, "0001")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO topics (name, keywords) VALUES "
                    "('LLM', ARRAY['large language model', 'LLM', 'the \"quoted\" phrase']), "
                    "('Пустая', ARRAY[]::text[])"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO papers (arxiv_id, title, published_at, pdf_url, abs_url) "
                    "VALUES ('2410.00001', 'Paper', now(), 'https://p', 'https://a')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO paper_topics (paper_id, topic_id, matched_keywords) "
                    "SELECT p.id, t.id, ARRAY['LLM'] FROM papers p, topics t WHERE t.name = 'LLM'"
                )
            )

        command.upgrade(config, "head")

        assert _topics("queries") == {
            "LLM": ['all:"large language model"', 'all:"LLM"', 'all:"the quoted phrase"'],
            "Пустая": [],
        }
        assert _topics("description") == {"LLM": None, "Пустая": None}
        with engine.connect() as conn:
            link = conn.execute(
                text("SELECT matched_keywords, matched_queries FROM paper_topics")
            ).one()
        assert link.matched_keywords == ["LLM"]
        assert link.matched_queries == []

        # сложное выражение, появившееся уже после миграции, при откате пропускается
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE topics SET queries = queries || "
                    "ARRAY['(ti:dataset OR ti:benchmark) AND all:railway', 'all:COLREG'] "
                    "WHERE name = 'LLM'"
                )
            )

        command.downgrade(config, "0001")

        assert _topics("keywords") == {
            "LLM": ["large language model", "LLM", "the quoted phrase", "COLREG"],
            "Пустая": [],
        }

        command.upgrade(config, "head")

        assert _topics("queries")["LLM"] == [
            'all:"large language model"',
            'all:"LLM"',
            'all:"the quoted phrase"',
            'all:"COLREG"',
        ]
    finally:
        # схема должна вернуться на head, даже если проверка упала посередине
        command.upgrade(config, "head")
