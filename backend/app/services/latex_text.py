"""LaTeX → обычный текст: формулы в резюме и названиях статей печатаются без кода разметки."""

import re

# fmt: off
_SYMBOLS = {
    # греческие буквы
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "θ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ", "varrho": "ρ",
    "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ",
    "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ",
    "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    # операторы и отношения
    "cdot": "·", "times": "×", "div": "÷", "pm": "±", "mp": "∓", "ast": "*", "star": "*",
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠", "approx": "≈",
    "sim": "~", "simeq": "≃", "equiv": "≡", "propto": "∝", "ll": "≪", "gg": "≫",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "leftrightarrow": "↔", "mapsto": "↦",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔",
    "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆", "cup": "∪", "cap": "∩",
    "setminus": "∖", "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃",
    "neg": "¬", "land": "∧", "lor": "∨", "wedge": "∧", "vee": "∨", "oplus": "⊕",
    "otimes": "⊗", "circ": "∘", "bullet": "•",
    "sum": "∑", "prod": "∏", "int": "∫", "partial": "∂", "nabla": "∇", "infty": "∞",
    "ldots": "…", "dots": "…", "cdots": "…", "vdots": "…", "ddots": "…",
    "mid": "|", "vert": "|", "Vert": "‖", "parallel": "‖", "perp": "⊥",
    "langle": "⟨", "rangle": "⟩", "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉",
    "lbrace": "{", "rbrace": "}", "lbrack": "[", "rbrack": "]",
    "ell": "l", "hbar": "ħ", "prime": "′", "dagger": "†", "top": "T", "degree": "°",
    "angle": "∠", "triangle": "△", "checkmark": "✓",
    # пробелы и служебные команды
    "quad": " ", "qquad": " ", "left": "", "right": "", "big": "", "Big": "", "bigg": "",
    "Bigg": "", "bigl": "", "bigr": "", "Bigl": "", "Bigr": "", "limits": "", "nolimits": "",
    "displaystyle": "", "textstyle": "", "scriptstyle": "", "nonumber": "", "notag": "",
    "rm": "", "bf": "", "it": "", "cal": "", "tt": "", "sf": "", "boldmath": "",
}
# fmt: on

# команды с одним аргументом, от которых остаётся только аргумент
_WRAPPERS = (
    "text|textbf|textit|textrm|textsf|texttt|textsc|textnormal|emph|underline|mbox|hbox|"
    "mathrm|mathbf|mathit|mathsf|mathtt|mathcal|mathbb|mathfrak|mathscr|mathbfit|mathnormal|"
    "boldsymbol|bm|pmb|operatorname|hat|widehat|tilde|widetilde|bar|overline|underline|vec|"
    "dot|ddot|check|breve|acute|grave|mathring|overrightarrow|overleftarrow|ensuremath"
)

_ESCAPED_RE = re.compile(r"\\([%$&#_{}])")
_SPACING_RE = re.compile(r"\\[,;:! ]")
_SYMBOL_RE = re.compile(r"\\([a-zA-Z]+)(?![a-zA-Z])")
_FRAC_RE = re.compile(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_BINOM_RE = re.compile(r"\\[dt]?binom\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_ROOT_RE = re.compile(r"\\sqrt\s*\[([^\[\]{}]*)\]\s*\{([^{}]*)\}")
_SQRT_RE = re.compile(r"\\sqrt\s*\{([^{}]*)\}")
_WRAPPER_RE = re.compile(rf"\\(?:{_WRAPPERS})\*?\s*\{{([^{{}}]*)\}}")
_SCRIPT_GROUP_RE = re.compile(r"([_^])\s*\{([^{}]*)\}")
_SCRIPT_DIGITS_RE = re.compile(r"(?<=[\w)\]])([_^])(\d+)(?![.,]?\d)")
_COMMAND_RE = re.compile(r"\\([a-zA-Z]+)\*?")
_SIMPLE_RE = re.compile(r"[\w.*′]+", re.UNICODE)
_PARENTHESIZED_RE = re.compile(r"\([^()]*\)")

# формула в тексте: $...$, $$...$$, \(...\), \[...\]; «$5 и $10» формулой не считается
_MATH_RE = re.compile(
    r"\$\$(.+?)\$\$"
    r"|(?<![\\$\w])\$(?=[^\s$])([^$\n]*?[^\s$\\])\$(?![\d$])"
    r"|\\\((.+?)\\\)"
    r"|\\\[(.+?)\\\]",
    re.DOTALL,
)

_SUPERSCRIPT = str.maketrans("0123456789+-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻")
_SUBSCRIPT = str.maketrans("0123456789+-", "₀₁₂₃₄₅₆₇₈₉₊₋")
# экранированные символы прячутся на время разбора, чтобы скобки и знаки не считались разметкой
_PLACEHOLDERS = {ch: chr(0xE000 + index) for index, ch in enumerate("%$&#_{}")}
_RESTORE = {placeholder: ch for ch, placeholder in _PLACEHOLDERS.items()}


def _symbol(match: re.Match[str]) -> str:
    return _SYMBOLS.get(match.group(1), match.group(0))


def _group(text: str) -> str:
    """Аргумент дроби, корня или индекса: составное выражение берётся в скобки."""
    text = text.strip()
    if not text or _SIMPLE_RE.fullmatch(text) or _PARENTHESIZED_RE.fullmatch(text):
        return text
    return f"({text})"


def _script(match: re.Match[str]) -> str:
    mark, body = match.group(1), match.group(2).strip()
    if re.fullmatch(r"[+-]?\d+", body):
        return body.translate(_SUPERSCRIPT if mark == "^" else _SUBSCRIPT)
    return f"{mark}{_group(body)}" if body else ""


def _commands(text: str) -> str:
    """Команды LaTeX: символы — в Юникод, оформление — снимается, неизвестные — по имени."""
    text = _SPACING_RE.sub(" ", text) if "\\" in text else text
    text = _SYMBOL_RE.sub(_symbol, text)
    previous = None
    while previous != text:
        previous = text
        text = _FRAC_RE.sub(lambda m: f"{_group(m.group(1))}/{_group(m.group(2))}", text)
        text = _BINOM_RE.sub(lambda m: f"C({m.group(1).strip()}, {m.group(2).strip()})", text)
        text = _ROOT_RE.sub(lambda m: f"{_group(m.group(2))}^(1/{m.group(1).strip()})", text)
        text = _SQRT_RE.sub(lambda m: f"√{_group(m.group(1))}", text)
        text = _WRAPPER_RE.sub(lambda m: m.group(1), text)
    return text


def _formula(text: str) -> str:
    text = _commands(text)
    previous = None
    while previous != text:
        previous = text
        text = _SCRIPT_GROUP_RE.sub(_script, text)
    text = _SCRIPT_DIGITS_RE.sub(_script_digits, text)
    text = _COMMAND_RE.sub(lambda m: m.group(1), text)
    text = text.replace("\\\\", " ").replace("\\", "")
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", text).strip()


def _script_digits(match: re.Match[str]) -> str:
    table = _SUPERSCRIPT if match.group(1) == "^" else _SUBSCRIPT
    return match.group(2).translate(table)


def latex_to_text(text: str) -> str:
    """Заменяет формулы и команды LaTeX читаемым текстом: `$\\hat{\\beta} \\le 0.5$` → `β ≤ 0.5`.

    Индексы и скобки разбираются только внутри формул; вне их снимаются лишь команды
    (`\\alpha`, `\\textbf{...}`, `\\%`), чтобы не испортить обычный текст.
    """
    if "$" not in text and "\\" not in text:
        return text
    text = _ESCAPED_RE.sub(lambda m: _PLACEHOLDERS[m.group(1)], text)
    text = _MATH_RE.sub(lambda m: _formula(next(g for g in m.groups() if g is not None)), text)
    if "\\" in text:
        text = _commands(text)
        text = _COMMAND_RE.sub(lambda m: m.group(1), text)
    for placeholder, ch in _RESTORE.items():
        text = text.replace(placeholder, ch)
    return text
