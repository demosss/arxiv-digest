import pytest

from app.services.latex_text import latex_to_text


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            r"$\hat{\beta} = -0.021$, $\phi = 2.03$, $SEQ = 0.016$",
            "β = -0.021, φ = 2.03, SEQ = 0.016",
        ),
        (r"$e^{\hat{\beta}} = 0.979$", "e^β = 0.979"),
        (r"$\binom{2m}{m}/2$ и $C_{m+1}$", "C(2m, m)/2 и C_(m+1)"),
        (r"степень $2 \cdot 3^{m-1}$ при $m \ge 3$", "степень 2 · 3^(m-1) при m ≥ 3"),
        (r"$\lambda_{dir}, \lambda_{int} = 0$", "λ_dir, λ_int = 0"),
        (r"$\frac{a+b}{2}$ и $\frac{1}{n}$", "(a+b)/2 и 1/n"),
        (r"$10^{-3}$, $x^2$, $x_1$", "10⁻³, x², x₁"),
        (r"\(O(n \log n)\) и \[\mathcal{L}_{\mathrm{total}}\]", "O(n log n) и L_total"),
        (r"$\sqrt{d_k}$, $x \in \{0, 1\}$", "√d_k, x ∈ {0, 1}"),
        (r"$$\sum_{i=1}^{N} x_i$$", "∑_(i=1)^N x_i"),
        (r"$F_n^{(\varepsilon)}(c)$", "F_n^(ε)(c)"),
        (r"точность 95\%, параметр \alpha и \textbf{важно}", "точность 95%, параметр α и важно"),
    ],
)
def test_formulas_become_plain_text(source: str, expected: str) -> None:
    assert latex_to_text(source) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Обычный текст без формул: BLEU 28.4, snake_case и x^2 остаются как есть.",
        "Стоимость выросла с $5 до $10 за запрос.",
        "Бюджет $100 и 50% скидка.",
    ],
)
def test_plain_text_is_not_changed(text: str) -> None:
    assert latex_to_text(text) == text


def test_conversion_is_idempotent() -> None:
    once = latex_to_text(r"Модель даёт $\hat{\beta} = -0.021$ и $3^{m-1}$ состояний.")

    assert latex_to_text(once) == once
    assert "$" not in once and "\\" not in once
