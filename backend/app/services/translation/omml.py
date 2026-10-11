"""Формулы для Word: MathML → OMML (редактируется в редакторе формул), LaTeX → MathML."""

import re

from lxml import etree

M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NSMAP = {"m": M_NS, "w": W_NS}
MATH_FONT = "Cambria Math"

# большие операторы: набираются как m:nary и забирают следующее за ними выражение
_NARY = frozenset("∑∏∐∫∬∭∮∯∰⋃⋂⋁⋀⨁⨂⨀⨄⨆")
_INTEGRALS = frozenset("∫∬∭∮∯∰")
# на этих знаках выражение под большим оператором заканчивается
_NARY_STOP = frozenset("=<>≤≥≠≈≡∼≃≅∝∈∉⊂⊆→←↔⇒⇐⇔,;+-−±∓)]}⟩|")
# невидимые операторы MathML (применение функции, умножение, разделитель, сложение)
_INVISIBLE = frozenset("\u2061\u2062\u2063\u2064")
_FENCES = {
    "(": ")",
    "[": "]",
    "{": "}",
    "⟨": "⟩",
    "⌊": "⌋",
    "⌈": "⌉",
    "|": "|",
    "‖": "‖",
    "∥": "∥",
}
# знак над выражением → комбинируемый символ для m:acc
_ACCENTS = {
    "^": "\u0302",
    "ˆ": "\u0302",
    "\u0302": "\u0302",
    "~": "\u0303",
    "˜": "\u0303",
    "\u0303": "\u0303",
    "→": "\u20d7",
    "⟶": "\u20d7",
    "⇀": "\u20d7",
    "\u20d7": "\u20d7",
    "←": "\u20d6",
    "\u20d6": "\u20d6",
    "˙": "\u0307",
    ".": "\u0307",
    "\u0307": "\u0307",
    "¨": "\u0308",
    "\u0308": "\u0308",
    "ˇ": "\u030c",
    "\u030c": "\u030c",
    "´": "\u0301",
    "\u0301": "\u0301",
    "`": "\u0300",
    "\u0300": "\u0300",
    "˘": "\u0306",
    "\u0306": "\u0306",
    "˚": "\u030a",
    "\u030a": "\u030a",
}
_BARS = frozenset("¯‾ˉ―_\u0304\u0305\u0332")
_SCRIPT_STYLES = {
    "double-struck": "double-struck",
    "script": "script",
    "bold-script": "script",
    "fraktur": "fraktur",
    "bold-fraktur": "fraktur",
    "sans-serif": "sans-serif",
    "bold-sans-serif": "sans-serif",
    "sans-serif-italic": "sans-serif",
    "monospace": "monospace",
}
_SKIPPED = frozenset({"annotation", "annotation-xml", "mphantom", "none", "mprescripts"})
_TOKENS = frozenset({"mi", "mn", "mo", "mtext", "ms", "mglyph"})
_SCRIPTS = frozenset({"msub", "msup", "msubsup", "munder", "mover", "munderover"})
_GROUPS = frozenset({"mrow", "mstyle", "mpadded"})

# обёртки, в которые модель и авторы заключают формулу
_LATEX_OPEN_RE = re.compile(
    r"^(?:\$\$|\$|\\\[|\\\(|\\begin\{(?:equation|displaymath|math)\*?\})\s*"
)
_LATEX_CLOSE_RE = re.compile(r"\s*(?:\$\$|\$|\\\]|\\\)|\\end\{(?:equation|displaymath|math)\*?\})$")
_LATEX_META_RE = re.compile(r"\\(?:label|tag)\*?\s*\{[^{}]*\}|\\(?:nonumber|notag)\b")
_CODE_FENCE_RE = re.compile(r"^```\w*\s*|\s*```$")


class MathConversionError(Exception):
    pass


def _m(tag: str, *children: etree._Element, **attrs: str) -> etree._Element:
    element = etree.Element(f"{{{M_NS}}}{tag}", nsmap=_NSMAP)
    for name, value in attrs.items():
        element.set(f"{{{M_NS}}}{name}", value)
    element.extend(children)
    return element


def _local(element: etree._Element) -> str:
    tag = element.tag
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _children(element: etree._Element) -> list[etree._Element]:
    return [child for child in element if _local(child) and _local(child) not in _SKIPPED]


def _text(element: etree._Element) -> str:
    return "".join(element.itertext()).strip()


def _run(
    text: str, *, style: str | None = None, script: str | None = None, normal: bool = False
) -> etree._Element:
    run = _m("r")
    props = _m("rPr")
    if normal:
        props.append(_m("nor"))
    else:
        if script:
            props.append(_m("scr", val=script))
        if style:
            props.append(_m("sty", val=style))
    if len(props):
        run.append(props)
    word_props = etree.SubElement(run, f"{{{W_NS}}}rPr")
    fonts = etree.SubElement(word_props, f"{{{W_NS}}}rFonts")
    fonts.set(f"{{{W_NS}}}ascii", MATH_FONT)
    fonts.set(f"{{{W_NS}}}hAnsi", MATH_FONT)
    text_element = _m("t")
    text_element.text = text
    if text != text.strip():
        text_element.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    run.append(text_element)
    return run


def _token(element: etree._Element) -> list[etree._Element]:
    kind = _local(element)
    text = "".join(ch for ch in "".join(element.itertext()) if ch not in _INVISIBLE)
    text = text.replace("\xa0", " ") if kind == "mtext" else " ".join(text.split())
    if not text:
        return []
    if kind in ("mtext", "ms"):
        return [_run(text, normal=True)]
    variant = (element.get("mathvariant") or "").lower()
    style: str | None = None
    if variant == "normal":
        style = "p"
    elif variant == "bold-italic":
        style = "bi"
    elif variant.startswith("bold"):
        style = "b"
    elif variant == "italic":
        style = "i"
    elif kind == "mi" and len(text) > 1:
        # в MathML идентификатор из нескольких букв (sin, max) по умолчанию прямой
        style = "p"
    return [_run(text, style=style, script=_SCRIPT_STYLES.get(variant))]


def _space(element: etree._Element) -> list[etree._Element]:
    match = re.match(r"\s*(-?[\d.]+)\s*(em|ex|pt|px)?", element.get("width") or "")
    if not match:
        return []
    try:
        width = float(match.group(1))
    except ValueError:
        return []
    width *= {"em": 1.0, "ex": 0.5, "pt": 0.1, "px": 0.08}.get(match.group(2) or "em", 1.0)
    if width < 0.2:
        return []
    return [_run("\u2003" if width >= 0.9 else "\u2009", normal=True)]


def _arg(tag: str, element: etree._Element | None) -> etree._Element:
    """Аргумент конструкции (m:e, m:num, m:sub ...): у mrow берётся содержимое, а не он сам."""
    if element is None:
        return _m(tag)
    if _local(element) in _GROUPS and _fence(element) is None:
        return _m(tag, *_row(_children(element)))
    return _m(tag, *_convert(element))


def _operator(element: etree._Element | None) -> str:
    """Символ оператора, если узел — одиночный mo (возможно, в обёртках mrow / mstyle)."""
    while element is not None and _local(element) in _GROUPS:
        inner = _children(element)
        if len(inner) != 1:
            return ""
        element = inner[0]
    if element is None or _local(element) != "mo":
        return ""
    return _text(element)


def _is_nary(element: etree._Element) -> bool:
    if _local(element) not in _SCRIPTS:
        return False
    parts = _children(element)
    return bool(parts) and _operator(parts[0]) in _NARY


def _nary(element: etree._Element, operand: list[etree._Element]) -> etree._Element:
    kind = _local(element)
    parts = _children(element)
    char = _operator(parts[0])
    sub = sup = None
    if kind in ("msub", "munder", "msubsup", "munderover"):
        sub = parts[1] if len(parts) > 1 else None
    if kind in ("msup", "mover"):
        sup = parts[1] if len(parts) > 1 else None
    elif kind in ("msubsup", "munderover"):
        sup = parts[2] if len(parts) > 2 else None
    under_over = kind in ("munder", "mover", "munderover") and char not in _INTEGRALS
    props = _m(
        "naryPr", _m("chr", val=char), _m("limLoc", val="undOvr" if under_over else "subSup")
    )
    if sub is None:
        props.append(_m("subHide", val="1"))
    if sup is None:
        props.append(_m("supHide", val="1"))
    return _m("nary", props, _arg("sub", sub), _arg("sup", sup), _m("e", *operand))


def _script(element: etree._Element) -> list[etree._Element]:
    kind = _local(element)
    parts = _children(element)
    base = parts[0] if parts else None
    first = parts[1] if len(parts) > 1 else None
    second = parts[2] if len(parts) > 2 else None
    if kind == "msub":
        return [_m("sSub", _arg("e", base), _arg("sub", first))]
    if kind == "msup":
        return [_m("sSup", _arg("e", base), _arg("sup", first))]
    if kind == "msubsup":
        return [_m("sSubSup", _arg("e", base), _arg("sub", first), _arg("sup", second))]
    if kind == "munderover":
        lower = _m("limLow", _arg("e", base), _arg("lim", first))
        return [_m("limUpp", _m("e", lower), _arg("lim", second))]

    mark = _operator(first)
    if kind == "mover":
        if mark in _BARS:
            return [_m("bar", _m("barPr", _m("pos", val="top")), _arg("e", base))]
        if mark in _ACCENTS:
            return [_m("acc", _m("accPr", _m("chr", val=_ACCENTS[mark])), _arg("e", base))]
        if mark == "⏞":
            props = _m(
                "groupChrPr", _m("chr", val="⏞"), _m("pos", val="top"), _m("vertJc", val="bot")
            )
            return [_m("groupChr", props, _arg("e", base))]
        return [_m("limUpp", _arg("e", base), _arg("lim", first))]
    if mark in _BARS:
        return [_m("bar", _m("barPr", _m("pos", val="bot")), _arg("e", base))]
    if mark == "⏟":
        return [_m("groupChr", _m("groupChrPr", _m("chr", val="⏟")), _arg("e", base))]
    return [_m("limLow", _arg("e", base), _arg("lim", first))]


def _multiscripts(element: etree._Element) -> list[etree._Element]:
    """mmultiscripts: основание, пары (нижний, верхний) индексов, после mprescripts — левые."""
    parts = [child for child in element if _local(child) and _local(child) != "annotation"]
    if not parts:
        return []
    base, rest = parts[0], parts[1:]
    split = next((i for i, part in enumerate(rest) if _local(part) == "mprescripts"), len(rest))
    post, pre = rest[:split], rest[split + 1 :]

    def pick(items: list[etree._Element], index: int) -> etree._Element | None:
        if index < len(items) and _local(items[index]) != "none":
            return items[index]
        return None

    result = _m("e", *_convert(base))
    if pick(post, 0) is not None or pick(post, 1) is not None:
        scripted = _m("sSubSup", result, _arg("sub", pick(post, 0)), _arg("sup", pick(post, 1)))
        result = _m("e", scripted)
    if pick(pre, 0) is not None or pick(pre, 1) is not None:
        return [_m("sPre", _arg("sub", pick(pre, 0)), _arg("sup", pick(pre, 1)), result)]
    return list(result)


def _table(element: etree._Element) -> list[etree._Element]:
    rows = [
        [cell for cell in row if _local(cell) == "mtd"]
        for row in element
        if _local(row) in ("mtr", "mlabeledtr")
    ]
    rows = [row for row in rows if row]
    if not rows:
        return []
    width = max(len(row) for row in rows)
    column = _m("mc", _m("mcPr", _m("count", val=str(width)), _m("mcJc", val="center")))
    matrix = _m("m", _m("mPr", _m("mcs", column)))
    for row in rows:
        cells = [_m("e", *_row(_children(cell))) for cell in row]
        cells += [_m("e") for _ in range(width - len(row))]
        matrix.append(_m("mr", *cells))
    return [matrix]


def _fence(element: etree._Element) -> tuple[str, str, list[etree._Element]] | None:
    """Растягиваемые скобки вокруг содержимого mrow: (открывающая, закрывающая, содержимое)."""
    parts = _children(element)
    if len(parts) < 2 or _local(parts[0]) != "mo" or parts[0].get("stretchy") == "false":
        return None
    opening = _text(parts[0])
    if opening not in _FENCES:
        return None
    last = parts[-1]
    if _local(last) == "mo" and last.get("stretchy") != "false":
        closing = _text(last)
        if closing not in _FENCES.values():
            return None
        inner = parts[1:-1]
        depth = 0
        for part in inner:
            mark = _text(part) if _local(part) == "mo" else ""
            if not mark:
                continue
            if opening == closing and mark == opening:
                return None
            if mark == opening:
                depth += 1
            elif mark == closing:
                depth -= 1
                if depth < 0:
                    return None
        return (opening, closing, inner) if depth == 0 else None
    # «{» без пары перед таблицей — запись по случаям (cases)
    if opening == "{" and _local(last) == "mtable" and len(parts) == 2:
        return opening, "", parts[1:]
    return None


def _delimited(opening: str, closing: str, inner: list[etree._Element]) -> etree._Element:
    props = _m("dPr", _m("begChr", val=opening), _m("endChr", val=closing))
    return _m("d", props, _m("e", *_row(inner)))


def _fenced(element: etree._Element) -> list[etree._Element]:
    separators = (element.get("separators", ",") or "").replace(" ", "")
    content: list[etree._Element] = []
    for index, child in enumerate(_children(element)):
        if index and separators:
            content.append(_run(separators[min(index - 1, len(separators) - 1)]))
        content.extend(_convert(child))
    props = _m(
        "dPr",
        _m("begChr", val=element.get("open", "(")),
        _m("endChr", val=element.get("close", ")")),
    )
    return [_m("d", props, _m("e", *content))]


def _convert(element: etree._Element) -> list[etree._Element]:
    kind = _local(element)
    if kind in _TOKENS:
        return _token(element)
    if kind in _SKIPPED or not kind:
        return []
    if kind == "mspace":
        return _space(element)
    parts = _children(element)
    if kind == "mfrac":
        fraction = _m("f")
        thickness = (element.get("linethickness") or "").strip()
        if re.fullmatch(r"0(?:\.0*)?(?:pt|em|ex|px)?", thickness):
            fraction.append(_m("fPr", _m("type", val="noBar")))
        elif element.get("bevelled") == "true":
            fraction.append(_m("fPr", _m("type", val="skw")))
        fraction.append(_arg("num", parts[0] if parts else None))
        fraction.append(_arg("den", parts[1] if len(parts) > 1 else None))
        return [fraction]
    if kind == "msqrt":
        return [_m("rad", _m("radPr", _m("degHide", val="1")), _m("deg"), _m("e", *_row(parts)))]
    if kind == "mroot":
        degree = _arg("deg", parts[1] if len(parts) > 1 else None)
        return [_m("rad", degree, _arg("e", parts[0] if parts else None))]
    if kind in _SCRIPTS:
        return _script(element)
    if kind == "mmultiscripts":
        return _multiscripts(element)
    if kind == "mtable":
        return _table(element)
    if kind == "mfenced":
        return _fenced(element)
    if kind in ("semantics", "maction"):
        return _convert(parts[0]) if parts else []
    if kind == "mrow":
        fence = _fence(element)
        if fence is not None:
            return [_delimited(*fence)]
    # mrow, mstyle, mpadded, menclose, merror, math и незнакомые узлы — просто содержимое
    return _row(parts)


def _row(parts: list[etree._Element]) -> list[etree._Element]:
    """Последовательность узлов; большой оператор забирает выражение, стоящее за ним."""
    result: list[etree._Element] = []
    index = 0
    while index < len(parts):
        part = parts[index]
        index += 1
        if not _is_nary(part):
            result.extend(_convert(part))
            continue
        start = index
        while index < len(parts):
            mark = _text(parts[index]) if _local(parts[index]) == "mo" else ""
            if mark and mark in _NARY_STOP and index > start:
                break
            index += 1
        operand = _row(parts[start:index])
        # без выражения Word показал бы пустую рамку — тогда оператор набирается как обычный знак
        result.extend([_nary(part, operand)] if operand else _script(part))
    return result


def mathml_to_omml(mathml: str) -> str:
    """Формула MathML → XML элемента `m:oMath`."""
    try:
        root = etree.fromstring(mathml.encode("utf-8"), etree.XMLParser(recover=True))
    except etree.XMLSyntaxError as exc:
        raise MathConversionError(f"некорректный MathML: {exc}") from exc
    if root is None:
        raise MathConversionError("пустой MathML")
    content = _row(_children(root)) if _local(root) == "math" else _convert(root)
    if not content:
        raise MathConversionError("в формуле нет содержимого")
    return etree.tostring(_m("oMath", *content), encoding="unicode")


def clean_latex(latex: str) -> str:
    """Убирает обёртки ($...$, \\[...\\], equation), метки и номера: остаётся сама формула."""
    text = _CODE_FENCE_RE.sub("", latex.strip()).strip()
    previous = None
    while previous != text:
        previous = text
        text = _LATEX_CLOSE_RE.sub("", _LATEX_OPEN_RE.sub("", text)).strip()
    return _LATEX_META_RE.sub("", text).strip()


def latex_to_mathml(latex: str) -> str:
    from latex2mathml import converter

    source = clean_latex(latex)
    if not source:
        raise MathConversionError("пустая формула")
    try:
        return str(converter.convert(source, display="block"))
    except Exception as exc:
        raise MathConversionError(f"LaTeX не разобран: {type(exc).__name__}: {exc}") from exc


def latex_to_omml(latex: str) -> str:
    return mathml_to_omml(latex_to_mathml(latex))
