"""Turn the LaTeX an LLM likes to write into readable Unicode math, and wrap display equations for the chat card."""

from __future__ import annotations

import re

_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ", "eta": "η",
    "theta": "θ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ",
    "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Phi": "Φ",
    "Psi": "Ψ", "Omega": "Ω",
}
_SYMBOLS = {
    "cdot": "·", "times": "×", "div": "÷", "pm": "±", "mp": "∓", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥",
    "neq": "≠", "ne": "≠", "approx": "≈", "equiv": "≡", "sim": "∼", "infty": "∞", "int": "∫", "iint": "∬",
    "oint": "∮", "sum": "∑", "prod": "∏", "partial": "∂", "nabla": "∇", "to": "→", "rightarrow": "→",
    "leftarrow": "←", "Rightarrow": "⇒", "Leftarrow": "⇐", "leftrightarrow": "↔", "Leftrightarrow": "⇔",
    "implies": "⇒", "iff": "⇔", "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆", "cup": "∪", "cap": "∩",
    "forall": "∀", "exists": "∃", "emptyset": "∅", "ldots": "…", "cdots": "⋯", "dots": "…", "circ": "∘",
    "degree": "°", "angle": "∠", "perp": "⊥", "parallel": "∥", "propto": "∝", "ll": "≪", "gg": "≫",
    "land": "∧", "lor": "∨", "neg": "¬", "star": "⋆", "bullet": "•", "prime": "′",
}
_FUNCS = {"sin", "cos", "tan", "cot", "sec", "csc", "log", "ln", "exp", "lim", "max", "min", "det", "gcd", "arcsin",
          "arccos", "arctan", "sinh", "cosh", "tanh", "mod", "sup", "inf"}
_BLACKBOARD = {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ", "P": "ℙ"}
_SUP = str.maketrans("0123456789+-=()nixyabcdkmt", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱˣʸᵃᵇᶜᵈᵏᵐᵗ")
_SUB = str.maketrans("0123456789+-=()aeoxhklmnpst", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₕₖₗₘₙₚₛₜ")


def _group(s: str, i: int) -> tuple[str, int]:
    """The {...} group starting at s[i] (or a single token). Returns (content, index after it)."""
    if i >= len(s):
        return "", i
    if s[i] == "{":
        depth = 0
        for j in range(i, len(s)):
            depth += s[j] == "{"
            depth -= s[j] == "}"
            if depth == 0:
                return s[i + 1 : j], j + 1
        return s[i + 1 :], len(s)
    if s[i] == "\\":
        m = re.match(r"\\[A-Za-z]+", s[i:])
        if m:
            return m.group(), i + len(m.group())
    return s[i], i + 1


def _wrap(x: str) -> str:
    x = x.strip()
    return x if re.fullmatch(r"[\w.·]+|\([^()]*\)", x) else f"({x})"


def _script(x: str, table: dict[int, str], marker: str, sign: str) -> str:
    x = x.strip()
    if x and all(ch in marker for ch in x.replace(" ", "")):
        return x.translate(table)
    return f"{sign}({x})" if len(x) > 1 else f"{sign}{x}"


def latex_to_unicode(src: str) -> str:
    s = src
    s = re.sub(r"\\(?:left|right|big|Big|bigg|Bigg)\b\s*", "", s)
    s = s.replace("\\,", " ").replace("\\;", " ").replace("\\!", "").replace("\\ ", " ").replace("\\quad", "  ")
    s = s.replace("\\\\", "; ")
    out: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            m = re.match(r"\\([A-Za-z]+)", s[i:])
            if not m:
                out.append(s[i + 1 : i + 2])
                i += 2
                continue
            name, i = m.group(1), i + m.end()
            if name in ("frac", "dfrac", "tfrac"):
                a, i = _group(s, _skip(s, i))
                b, i = _group(s, _skip(s, i))
                out.append(f"{_wrap(latex_to_unicode(a))}/{_wrap(latex_to_unicode(b))}")
            elif name == "sqrt":
                n = ""
                if i < len(s) and s[i] == "[":
                    j = s.find("]", i)
                    n, i = s[i + 1 : j], j + 1
                a, i = _group(s, _skip(s, i))
                root = latex_to_unicode(a)
                sign = {"": "√", "2": "√", "3": "∛", "4": "∜"}.get(n, f"{_script(n, _SUP, '0123456789', '^')}√")
                out.append(f"{sign}{_wrap(root)}" if len(root) > 1 and not root.isalnum() else f"{sign}{root}")
            elif name in ("text", "mathrm", "mathbf", "mathit", "operatorname", "textbf", "mathcal"):
                a, i = _group(s, _skip(s, i))
                out.append(latex_to_unicode(a))
            elif name == "mathbb":
                a, i = _group(s, _skip(s, i))
                out.append(_BLACKBOARD.get(a, a))
            elif name in ("overline", "bar", "hat", "vec"):
                a, i = _group(s, _skip(s, i))
                out.append(latex_to_unicode(a) + {"overline": "̅", "bar": "̅", "hat": "̂", "vec": "⃗"}[name])
            elif name in _GREEK:
                out.append(_GREEK[name])
            elif name in _SYMBOLS:
                out.append(_SYMBOLS[name])
            elif name in _FUNCS:
                out.append(name)
            elif name in ("begin", "end"):
                _, i = _group(s, _skip(s, i))
            else:
                out.append(name)
            continue
        if c in "^_":
            a, i2 = _group(s, i + 1)
            inner = latex_to_unicode(a)
            out.append(_script(inner, _SUP, "0123456789+-=()nixyabcdkmt", "^") if c == "^" else _script(inner, _SUB, "0123456789+-=()aeoxhklmnpst", "_"))
            i = i2
            continue
        if c in "{}":
            i += 1
            continue
        if c == "&":
            out.append("  ")
            i += 1
            continue
        out.append(c)
        i += 1
    return re.sub(r"[ \t]{3,}", "  ", "".join(out)).strip()


def _skip(s: str, i: int) -> int:
    while i < len(s) and s[i] == " ":
        i += 1
    return i


_FENCE = re.compile(r"(```.*?```)", re.S)
_DISPLAY = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]", re.S)
_INLINE = re.compile(r"(?<![\\$])\$(?!\s)([^$\n]+?)(?<!\s)\$(?!\d)|\\\((.+?)\\\)")


def render_math(markdown: str) -> str:
    """Convert $...$ inline math to Unicode and $$...$$ display math to a ```math block; leave code fences alone."""
    parts = _FENCE.split(markdown)
    for k in range(0, len(parts), 2):  # even parts are outside code fences
        seg = parts[k]
        seg = _DISPLAY.sub(lambda m: "\n```math\n" + latex_to_unicode(m.group(1) or m.group(2)) + "\n```\n", seg)
        seg = _INLINE.sub(lambda m: latex_to_unicode(m.group(1) or m.group(2)), seg)
        parts[k] = seg
    return "".join(parts)
