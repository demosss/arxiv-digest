"""Разбор HTML-версии статьи с arXiv (разметка LaTeXML) в блоки документа.

В HTML структура уже размечена: разделы, целые абзацы, формулы в MathML, таблицы,
рисунки и список литературы, поэтому эвристики здесь не нужны.
"""

import re
from collections.abc import Callable
from urllib.parse import urljoin

import lxml.html
from lxml import etree

from app.core.logging import get_logger
from app.services.latex_text import latex_to_text
from app.services.translation.images import render_svg
from app.services.translation.model import (
    SOURCE_HTML,
    Block,
    Document,
    Figure,
    Formula,
    Heading,
    Image,
    ListItem,
    Math,
    Paragraph,
    References,
    Table,
    TableCell,
)

log = get_logger(__name__)

ARXIV_HTML_URL = "https://arxiv.org/html/{arxiv_id}v{version}"
# меньше этого — LaTeXML не справился с исходником, и в HTML осталась одна шапка
MIN_TEXT_CHARS = 1500
# пиксель LaTeXML — 1/96 дюйма
CM_PER_PX = 2.54 / 96

ImageLoader = Callable[[str], Image | None]

_HEADING_LEVELS = {
    "ltx_title_section": 1,
    "ltx_title_appendix": 1,
    "ltx_title_acknowledgements": 1,
    "ltx_title_subsection": 2,
    "ltx_title_subsubsection": 3,
    "ltx_title_paragraph": 3,
    "ltx_title_subparagraph": 3,
}
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
# служебные части страницы и то, что разбирается отдельно (заголовок, авторы, аннотация)
_SKIPPED_CLASSES = frozenset(
    {
        "ltx_title_document",
        "ltx_authors",
        "ltx_abstract",
        "ltx_classification",
        "ltx_keywords",
        "ltx_dates",
        "ltx_TOC",
        "ltx_pagination",
        "ltx_page_footer",
        "ltx_note",
        "ltx_ERROR",
    }
)
_DROPPED_INLINE_CLASSES = frozenset({"ltx_ERROR", "ltx_rule", "ltx_bib_cited"})
_DROPPED_INLINE_TAGS = frozenset({"svg", "img", "script", "style", "button", "annotation"})
_BLOCK_TAGS = frozenset({"p", "figure", "table", "ul", "ol", "dl", "pre", "section"})
_NOTE_SERVICE_CLASSES = frozenset({"ltx_note_mark", "ltx_note_type", "ltx_tag_note"})
_KEYWORDS_RE = re.compile(r"^\s*(key\s?words|index terms)\b", re.IGNORECASE)
_SECTION_NUMBER_RE = re.compile(r"[\dA-Z]{1,4}(?:\.\d+)*")
_REFERENCE_NUMBER_RE = re.compile(r"\[?(\d+)\]?\.?")
_SUPERSCRIPTS = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
# lxml приводит имена атрибутов к нижнему регистру, а SVG к нему чувствителен
_SVG_ATTRIBUTES = {"viewbox": "viewBox", "preserveaspectratio": "preserveAspectRatio"}
# в формуле только число или простая величина («95%», «−0.5»): её проще оставить текстом
_PLAIN_MATH_TAGS = frozenset({"math", "semantics", "mrow", "mstyle", "mn", "mo", "mtext"})


class HtmlParseError(Exception):
    """HTML не похож на статью или в нём почти нет текста."""


def html_url(arxiv_id: str, version: int) -> str:
    return ARXIV_HTML_URL.format(arxiv_id=arxiv_id, version=version)


def _classes(element: etree._Element) -> frozenset[str]:
    return frozenset((element.get("class") or "").split())


def _is_element(node: etree._Element) -> bool:
    return isinstance(node.tag, str)


def _normalize(text: str) -> str:
    text = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
    # пробелы перед знаками препинания остаются от вырезанных сносок и ссылок
    return re.sub(r" +([,.;:!?)\]])", r"\1", text)


def _superscript(number: int) -> str:
    return str(number).translate(_SUPERSCRIPTS)


def _math_plain_text(element: etree._Element) -> str:
    return "".join(
        (node.text or "")
        for node in element.iter()
        if _is_element(node) and node.tag not in ("annotation", "annotation-xml")
    )


def _split_label(text: str) -> tuple[str, str]:
    """«Figure 1: Overall structure» → («Figure 1», «Overall structure»)."""
    match = re.match(r"^\s*([A-Za-z]+\.?\s*[\w.\-]+)\s*[:.]?\s*$", text)
    return (_normalize(match.group(1)), "") if match else ("", _normalize(text))


class _Parser:
    def __init__(self, base_url: str, load_image: ImageLoader) -> None:
        self._base_url = base_url
        self._load_image = load_image
        self._doc = Document(title="", authors=[], abstract=[], blocks=[], source=SOURCE_HTML)
        self._blocks: list[Block] = self._doc.blocks
        self._footnotes: list[str] = []
        self._footnote_count = 0
        # рисунки по id элемента: по нему панели («F1.sf1») находят общую подпись («F1»)
        self._figure_ids: dict[int, str] = {}
        # сноски в заголовке и подписях не собираются: их некуда поставить
        self._keep_notes = True

    def parse(self, article: etree._Element) -> Document:
        titles = article.xpath(".//h1[contains(@class, 'ltx_title_document')]")
        if titles:
            self._keep_notes = False
            self._doc.title = self._inline(titles[0])
            self._keep_notes = True
        for abstract in article.xpath(".//div[contains(@class, 'ltx_abstract')]")[:1]:
            paragraphs = [self._inline(p, plain_math=False) for p in abstract.xpath(".//p")]
            if not any(paragraphs):
                paragraphs = [self._inline(abstract, skip=frozenset({"ltx_title"}))]
            self._doc.abstract = [text for text in paragraphs if text]
        self._footnotes.clear()
        self._doc.keywords = self._keywords(article)
        self._walk(article)
        return self._doc

    def _keywords(self, article: etree._Element) -> str:
        blocks = article.xpath(
            ".//div[contains(@class, 'ltx_keywords') or contains(@class, 'ltx_classification')]"
        )
        for block in blocks:
            titles = block.xpath("./*[contains(@class, 'ltx_title')]")
            title = _normalize("".join(titles[0].itertext())) if titles else ""
            if "ltx_keywords" in _classes(block) or _KEYWORDS_RE.match(title):
                text = self._inline(block, skip=frozenset({"ltx_title"}), plain_math=True)
                text = _KEYWORDS_RE.sub("", text).lstrip(" :—-")
                return re.sub(r"\s*,\s*", ", ", text).strip(" ,")
        return ""

    # --- строчное содержимое ---

    def _inline(
        self,
        element: etree._Element,
        *,
        skip: frozenset[str] = frozenset(),
        plain_math: bool = False,
    ) -> str:
        parts: list[str] = []
        self._collect(element, parts, skip, plain_math)
        return _normalize("".join(parts))

    def _collect(
        self, element: etree._Element, parts: list[str], skip: frozenset[str], plain_math: bool
    ) -> None:
        if element.text:
            parts.append(element.text)
        for child in element:
            if _is_element(child):
                self._collect_child(child, parts, skip, plain_math)
            if child.tail:
                parts.append(child.tail)

    def _collect_child(
        self, child: etree._Element, parts: list[str], skip: frozenset[str], plain_math: bool
    ) -> None:
        classes = _classes(child)
        if child.tag == "math":
            parts.append(self._math(child, plain_math))
        elif "ltx_note" in classes:
            parts.append(self._note(child, classes))
        elif child.tag == "br":
            parts.append(" ")
        elif (
            child.tag in _DROPPED_INLINE_TAGS or classes & _DROPPED_INLINE_CLASSES or classes & skip
        ):
            return
        else:
            self._collect(child, parts, skip, plain_math)

    def _math(self, element: etree._Element, plain: bool) -> str:
        latex = (element.get("alttext") or "").strip()
        if not latex:
            annotations = element.xpath(".//annotation[@encoding='application/x-tex']")
            latex = "".join(annotations[0].itertext()).strip() if annotations else ""
        text = latex_to_text(f"${latex}$") if latex else ""
        text = text if text and "\\" not in text else _normalize(_math_plain_text(element))
        tags = {node.tag for node in element.iter() if _is_element(node)}
        tags -= {"annotation", "annotation-xml"}
        if plain or tags <= _PLAIN_MATH_TAGS:
            return _normalize(_math_plain_text(element)) if tags <= _PLAIN_MATH_TAGS else text
        mathml = etree.tostring(element, encoding="unicode", with_tail=False)
        return self._doc.add_math(Math(mathml=mathml, latex=latex or None, text=text))

    def _note(self, element: etree._Element, classes: frozenset[str]) -> str:
        if not self._keep_notes or "ltx_role_footnote" not in classes:
            return ""
        content = element.xpath(".//*[contains(@class, 'ltx_note_content')]")
        if not content:
            return ""
        text = self._inline(content[0], skip=_NOTE_SERVICE_CLASSES)
        if not text:
            return ""
        self._footnote_count += 1
        mark = _superscript(self._footnote_count)
        self._footnotes.append(f"{mark} {text}")
        return mark

    # --- блоки ---

    def _emit_paragraph(self, text: str, lead: str = "") -> None:
        if text or lead:
            self._blocks.append(Paragraph(text=text, lead=lead))
        for note in self._footnotes:
            self._blocks.append(Paragraph(text=note, footnote=True))
        self._footnotes.clear()

    def _has_blocks(self, element: etree._Element) -> bool:
        return any(
            _is_element(node)
            and node is not element
            and (node.tag in _BLOCK_TAGS or node.tag in _HEADING_TAGS)
            for node in element.iter()
        )

    def _walk(self, parent: etree._Element) -> None:
        for child in parent:
            if _is_element(child):
                self._block(child)

    def _block(self, element: etree._Element) -> None:
        classes = _classes(element)
        tag = element.tag
        if classes & _SKIPPED_CLASSES or tag in ("nav", "header", "footer", "script", "style"):
            return
        if "ltx_bibliography" in classes:
            self._bibliography(element)
        elif tag in _HEADING_TAGS:
            self._heading(element, classes)
        elif tag == "p":
            self._emit_paragraph(self._inline(element))
        elif tag == "table" and (classes & {"ltx_equation", "ltx_equationgroup", "ltx_eqn_table"}):
            self._equations(element)
        elif tag == "table":
            self._table(element, "", "")
        elif tag == "figure":
            self._float(element)
        elif tag in ("ul", "ol"):
            self._list(element)
        elif tag == "dl":
            self._description_list(element)
        elif classes & {"ltx_theorem", "ltx_proof"}:
            self._theorem(element)
        elif "ltx_listing" in classes or tag == "pre":
            self._listing(element)
        elif "ltx_acknowledgements" in classes:
            for title in element.xpath("./*[contains(@class, 'ltx_title')]"):
                self._heading(title, _classes(title))
            self._emit_paragraph(self._inline(element, skip=frozenset({"ltx_title"})))
        elif self._has_blocks(element):
            self._walk(element)
        elif tag == "svg":
            image = self._svg(element)
            if image is not None:
                self._blocks.append(Figure(images=[image]))
        else:
            self._emit_paragraph(self._inline(element))

    def _heading(self, element: etree._Element, classes: frozenset[str]) -> None:
        if classes & {"ltx_title_abstract", "ltx_title_document", "ltx_title_bibliography"}:
            return
        level = next((lvl for name, lvl in _HEADING_LEVELS.items() if name in classes), None)
        if level is None:
            level = min(3, max(1, int(element.tag[1]) - 1))
        tags = element.xpath("./*[contains(@class, 'ltx_tag')]")
        number = _normalize("".join(tags[0].itertext())).rstrip(".") if tags else ""
        self._keep_notes = False
        text = self._inline(element, skip=frozenset({"ltx_tag"}))
        self._keep_notes = True
        if number and not _SECTION_NUMBER_RE.fullmatch(number):
            # «Appendix A» переводится вместе с названием
            text, number = f"{number}. {text}" if text else number, ""
        if text or number:
            self._blocks.append(Heading(level=level, text=text, number=number))

    def _equations(self, table: etree._Element) -> None:
        for row in table.iter("tr"):
            maths = list(row.iter("math"))
            tags = row.xpath(".//*[contains(@class, 'ltx_tag')]")
            number = _normalize("".join(tags[0].itertext())) if tags else ""
            if not maths:
                # строка текста между формулами (\intertext)
                text = self._inline(row, skip=frozenset({"ltx_tag"}))
                if text:
                    self._emit_paragraph(text)
                continue
            latex = " ".join(filter(None, ((m.get("alttext") or "").strip() for m in maths)))
            if len(maths) == 1:
                mathml = etree.tostring(maths[0], encoding="unicode", with_tail=False)
            else:
                # выравненные части одной строки (левая часть, знак, правая) склеиваются
                merged = etree.Element("math")
                for math in maths:
                    wrapper = etree.SubElement(merged, "mrow")
                    wrapper.extend(etree.fromstring(etree.tostring(math, with_tail=False)))
                mathml = etree.tostring(merged, encoding="unicode")
            text = latex_to_text(f"${latex}$") if latex else ""
            self._blocks.append(
                Formula(math=Math(mathml=mathml, latex=latex or None, text=text), number=number)
            )

    def _caption(self, figure: etree._Element) -> tuple[str, str]:
        """Метка («Figure 1») и текст подписи плавающего объекта."""
        captions = figure.xpath("./figcaption") or figure.xpath(".//figcaption")
        if not captions:
            return "", ""
        caption = captions[0]
        self._keep_notes = False
        try:
            tags = caption.xpath("./*[contains(@class, 'ltx_tag')]")
            text = self._inline(caption, skip=frozenset({"ltx_tag"}))
            if not tags:
                return "", text
            label, rest = _split_label("".join(tags[0].itertext()))
            # метка не разобрана — подпись остаётся целиком, как в оригинале
            return (label, text) if label else ("", _normalize(f"{rest} {text}"))
        finally:
            self._keep_notes = True

    def _float(self, figure: etree._Element) -> None:
        label, caption = self._caption(figure)
        tabulars = figure.xpath(
            ".//table[contains(@class, 'ltx_tabular')]"
            "[not(ancestor::table[contains(@class, 'ltx_tabular')])]"
        )
        if tabulars:
            for index, tabular in enumerate(tabulars):
                self._table(tabular, label if index == 0 else "", caption if index == 0 else "")
            return
        images = self._images(figure)
        if images:
            block = Figure(images=images, caption=caption, label=label)
            self._figure_ids[id(block)] = figure.get("id") or ""
            self._blocks.append(block)
            return
        if self._merge_panels(figure.get("id") or "", label, caption):
            return
        if label or caption:
            self._emit_paragraph(caption, lead=f"{label}." if label else "")
        listings = figure.xpath(".//*[contains(@class, 'ltx_listing')]")
        if listings:
            for listing in listings:
                self._listing(listing)
            return
        for child in figure:
            if _is_element(child) and child.tag != "figcaption":
                self._block(child)

    def _merge_panels(self, figure_id: str, label: str, caption: str) -> bool:
        """Панели рисунка, вынесенные в отдельные figure перед пустым figure с общей подписью.

        Так LaTeXML размечает часть составных рисунков: панели «F1.sf1», «F1.sf2» идут
        соседями, а сам «F1» содержит только подпись. Панели собираются в один рисунок.
        """
        if not figure_id:
            return False
        panels: list[Figure] = []
        while self._blocks:
            last = self._blocks[-1]
            if not isinstance(last, Figure):
                break
            if not self._figure_ids.get(id(last), "").startswith(f"{figure_id}."):
                break
            panels.insert(0, last)
            self._blocks.pop()
        if not panels:
            return False
        images = [image for panel in panels for image in panel.images]
        self._blocks.append(Figure(images=images, caption=caption, label=label))
        return True

    def _images(self, figure: etree._Element) -> list[Image]:
        images: list[Image] = []
        for node in figure.iter("img", "svg"):
            if node.tag == "svg":
                image = self._svg(node)
            else:
                source = node.get("src")
                if not source or source.startswith("data:"):
                    continue
                image = self._load_image(urljoin(self._base_url, source))
                width = node.get("width") or ""
                if image is not None and width.isdigit():
                    image.width_cm = int(width) * CM_PER_PX
            if image is not None:
                images.append(image)
        return images

    def _svg(self, element: etree._Element) -> Image | None:
        markup = etree.tostring(element, encoding="unicode", with_tail=False)
        for lowered, proper in _SVG_ATTRIBUTES.items():
            markup = markup.replace(f" {lowered}=", f" {proper}=")
        if "xmlns=" not in markup[:300]:
            markup = markup.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
        width = element.get("width") or ""
        try:
            width_cm = float(width) * CM_PER_PX
        except ValueError:
            width_cm = None
        return render_svg(markup.encode("utf-8"), width_cm)

    def _table(self, tabular: etree._Element, label: str, caption: str) -> None:
        rows: list[list[TableCell]] = []
        self._keep_notes = False
        for row in tabular.xpath("./tr|./thead/tr|./tbody/tr|./tfoot/tr"):
            in_head = row.getparent().tag == "thead"
            cells = [
                TableCell(
                    text=self._inline(cell),
                    colspan=_span(cell.get("colspan")),
                    rowspan=_span(cell.get("rowspan")),
                    header=in_head or (cell.tag == "th" and "ltx_th_column" in _classes(cell)),
                )
                for cell in row.xpath("./td|./th")
            ]
            if any(cell.text for cell in cells):
                rows.append(cells)
        self._keep_notes = True
        if rows:
            self._blocks.append(Table(rows=rows, caption=caption, label=label))
        elif label or caption:
            self._emit_paragraph(caption, lead=f"{label}." if label else "")

    def _nested(self, element: etree._Element) -> list[Block]:
        """Блоки внутри элемента списка или теоремы — отдельным списком, чтобы поправить первый."""
        outer, self._blocks = self._blocks, []
        try:
            if self._has_blocks(element):
                self._walk(element)
            else:
                self._emit_paragraph(
                    self._inline(element, skip=frozenset({"ltx_tag", "ltx_title"}))
                )
            return self._blocks
        finally:
            self._blocks = outer

    def _list(self, element: etree._Element) -> None:
        ordered = element.tag == "ol"
        for item in element.xpath("./li"):
            tags = item.xpath("./*[contains(@class, 'ltx_tag')]")
            marker = _normalize("".join(tags[0].itertext())) if tags else ""
            if not ordered and not re.search(r"\w", marker):
                marker = ""
            for tag in tags:
                _drop(tag)
            blocks = self._nested(item)
            if blocks and isinstance(blocks[0], Paragraph) and not blocks[0].footnote:
                blocks[0] = ListItem(text=blocks[0].text, marker=marker)
            elif marker:
                blocks.insert(0, ListItem(text="", marker=marker))
            self._blocks.extend(blocks)

    def _description_list(self, element: etree._Element) -> None:
        lead = ""
        for child in element:
            if not _is_element(child):
                continue
            if child.tag == "dt":
                lead = self._inline(child)
            elif child.tag == "dd":
                blocks = self._nested(child)
                if blocks and isinstance(blocks[0], Paragraph):
                    blocks[0].lead = lead
                elif lead:
                    blocks.insert(0, Paragraph(text="", lead=lead))
                self._blocks.extend(blocks)
                lead = ""

    def _theorem(self, element: etree._Element) -> None:
        titles = element.xpath("./*[contains(@class, 'ltx_title')]")
        lead = self._inline(titles[0]) if titles else ""
        for title in titles:
            _drop(title)
        blocks = self._nested(element)
        if blocks and isinstance(blocks[0], Paragraph) and not blocks[0].footnote:
            blocks[0].lead = lead
        elif lead:
            blocks.insert(0, Paragraph(text="", lead=lead))
        self._blocks.extend(blocks)

    def _listing(self, element: etree._Element) -> None:
        lines = element.xpath(".//*[contains(@class, 'ltx_listingline')]")
        if lines:
            text = "\n".join(
                filter(None, (self._inline(line, skip=frozenset({"ltx_tag"})) for line in lines))
            )
        else:
            text = "\n".join(
                line.rstrip() for line in "".join(element.itertext()).splitlines() if line.strip()
            )
        if text:
            self._blocks.append(Paragraph(text=text, preformatted=True))

    def _bibliography(self, section: etree._Element) -> None:
        titles = section.xpath("./*[contains(@class, 'ltx_title')]")
        title = _normalize("".join(titles[0].itertext())) if titles else "References"
        items: list[str] = []
        self._keep_notes = False
        for item in section.xpath(".//li[contains(@class, 'ltx_bibitem')]"):
            tags = item.xpath("./*[contains(@class, 'ltx_tag')]")
            tag = _normalize("".join(tags[0].itertext())) if tags else ""
            text = self._inline(item, skip=frozenset({"ltx_tag"}), plain_math=True)
            match = _REFERENCE_NUMBER_RE.fullmatch(tag)
            # у стилей «автор — год» метка повторяет начало записи, поэтому не печатается
            if match:
                text = f"[{match.group(1)}] {text}"
            if text:
                items.append(text)
        self._keep_notes = True
        if items:
            self._blocks.append(References(title=title, items=items))


def _span(value: str | None) -> int:
    return int(value) if value and value.isdigit() and int(value) > 0 else 1


def _drop(element: etree._Element) -> None:
    """Удаляет элемент из дерева, сохраняя текст, стоящий после него."""
    parent = element.getparent()
    if parent is None:
        return
    tail = element.tail or ""
    previous = element.getprevious()
    if previous is not None:
        previous.tail = (previous.tail or "") + tail
    else:
        parent.text = (parent.text or "") + tail
    parent.remove(element)


def parse_html(content: bytes, *, base_url: str, load_image: ImageLoader) -> Document:
    """HTML статьи → документ. `load_image` скачивает рисунок по абсолютному адресу."""
    try:
        # кодировка задаётся явно: без <meta charset> lxml читает страницу как Latin-1
        root = lxml.html.fromstring(content, parser=lxml.html.HTMLParser(encoding="utf-8"))
    except (etree.ParserError, ValueError) as exc:
        raise HtmlParseError(f"HTML не разобран: {exc}") from exc
    articles = root.xpath("//article")
    if not articles:
        raise HtmlParseError("в HTML нет статьи (элемента article)")
    document = _Parser(base_url, load_image).parse(articles[0])
    chars = document.text_chars()
    if chars < MIN_TEXT_CHARS:
        raise HtmlParseError(f"в HTML-версии почти нет текста ({chars} символов)")
    log.info(
        "translation_html_parsed",
        blocks=len(document.blocks),
        text_chars=chars,
        inline_formulas=len(document.math),
    )
    return document
