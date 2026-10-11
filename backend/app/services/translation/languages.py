"""Языки перевода: список для UI, формулировка для промпта и служебные слова документа."""

import re
from dataclasses import dataclass

MAX_LANGUAGE_LENGTH = 64


@dataclass(frozen=True, slots=True)
class Language:
    code: str
    # название для UI
    label: str
    # как язык называется в промпте: «переведи на ...»
    target: str
    # другие написания, по которым язык узнаётся при свободном вводе
    aliases: tuple[str, ...]
    abstract: str
    keywords: str
    references: str
    figure: str
    table: str
    note: str


LANGUAGES: tuple[Language, ...] = (
    Language(
        code="ru",
        label="Русский",
        target="русский язык",
        aliases=("русский", "russian", "рус"),
        abstract="Аннотация.",
        keywords="Ключевые слова:",
        references="Список литературы",
        figure="Рис.",
        table="Таблица",
        note="Машинный перевод.",
    ),
    Language(
        code="en",
        label="Английский",
        target="английский язык",
        aliases=("английский", "english", "англ"),
        abstract="Abstract.",
        keywords="Keywords:",
        references="References",
        figure="Fig.",
        table="Table",
        note="Machine translation.",
    ),
    Language(
        code="de",
        label="Немецкий",
        target="немецкий язык",
        aliases=("немецкий", "german", "deutsch"),
        abstract="Zusammenfassung.",
        keywords="Schlüsselwörter:",
        references="Literaturverzeichnis",
        figure="Abb.",
        table="Tabelle",
        note="Maschinelle Übersetzung.",
    ),
    Language(
        code="fr",
        label="Французский",
        target="французский язык",
        aliases=("французский", "french", "français", "francais"),
        abstract="Résumé.",
        keywords="Mots-clés :",
        references="Références",
        figure="Fig.",
        table="Tableau",
        note="Traduction automatique.",
    ),
    Language(
        code="es",
        label="Испанский",
        target="испанский язык",
        aliases=("испанский", "spanish", "español", "espanol"),
        abstract="Resumen.",
        keywords="Palabras clave:",
        references="Referencias",
        figure="Fig.",
        table="Tabla",
        note="Traducción automática.",
    ),
)
_BY_CODE = {language.code: language for language in LANGUAGES}
_BY_ALIAS = {
    alias: language
    for language in LANGUAGES
    for alias in (language.code, language.label.lower(), *language.aliases)
}
# для языка, введённого вручную, служебные слова остаются английскими
_FALLBACK = _BY_CODE["en"]


def normalize_language(value: str) -> str:
    """Код языка из списка либо введённое вручную название (без лишних пробелов)."""
    cleaned = " ".join(value.split())[:MAX_LANGUAGE_LENGTH].strip()
    if not cleaned:
        raise ValueError("Язык перевода не указан")
    known = _BY_ALIAS.get(cleaned.lower())
    return known.code if known else cleaned


def find_language(code: str) -> Language | None:
    return _BY_CODE.get(code)


def language_label(code: str) -> str:
    known = _BY_CODE.get(code)
    return known.label if known else code


def prompt_target(code: str) -> str:
    known = _BY_CODE.get(code)
    return known.target if known else f"язык «{code}»"


def document_labels(code: str) -> Language:
    return _BY_CODE.get(code, _FALLBACK)


def filename_slug(code: str) -> str:
    """Язык в имени файла: код либо название, пригодное для файловой системы."""
    return re.sub(r"[^\w-]+", "_", code.lower()).strip("_") or "translation"
