"""Exact maths with SymPy, so answers are computed rather than guessed. Input is checked against a whitelist before parsing."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from jarvis_core.errors import SkillError
from jarvis_core.skills.base import S, Tool, ToolContext, ToolRegistry, obj

_ALLOWED_NAMES = {
    "sin", "cos", "tan", "cot", "sec", "csc", "asin", "acos", "atan", "sinh", "cosh", "tanh", "log", "ln", "exp", "sqrt", "cbrt",
    "abs", "pi", "e", "E", "oo", "inf", "factorial", "binomial", "gcd", "lcm", "floor", "ceiling", "ceil", "Rational", "mod", "Mod",
}
_CHARS = re.compile(r"^[0-9A-Za-z_+\-*/^().,=<>!%\s√π×÷·]*$")
_TASKS = ("evaluate", "solve", "simplify", "factor", "expand", "derivative", "integral", "limit")


def _clean(expr: str) -> str:
    e = expr.strip().replace("×", "*").replace("÷", "/").replace("·", "*").replace("π", "pi").replace("√", "sqrt")
    e = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s*", r"(\1/100)*", e, flags=re.I)  # 15% of 240
    e = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", e)
    e = e.replace("ln(", "log(").replace("inf", "oo").replace("ceil(", "ceiling(")
    return e


def _check(e: str) -> None:
    if not _CHARS.match(e) or "__" in e or len(e) > 400:
        raise SkillError("I can only do plain maths with that.")
    for name in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", e):
        if name not in _ALLOWED_NAMES and len(name) > 1 and not re.fullmatch(r"[A-Za-z]\d*", name):
            raise SkillError(f"I don't know the term '{name}' in a maths expression.")


def _compute(expression: str, task: str, variable: str, at: str) -> str:
    import sympy as sp
    from sympy.parsing.sympy_parser import (
        convert_xor,
        implicit_multiplication_application,
        parse_expr,
        standard_transformations,
    )

    tf = standard_transformations + (implicit_multiplication_application, convert_xor)
    x = sp.Symbol(variable or "x")

    def parse(s: str):
        s = _clean(s)
        _check(s)
        return parse_expr(s, transformations=tf, local_dict={"e": sp.E, "ln": sp.log, "ceiling": sp.ceiling, variable or "x": x})

    task = task if task in _TASKS else "evaluate"
    if task == "solve":
        eqs = [part for part in re.split(r"\s*(?:;|\band\b)\s*", expression) if part.strip()]
        parsed = []
        for part in eqs:
            if "=" in part and "==" not in part and "<" not in part and ">" not in part:
                left, right = part.split("=", 1)
                parsed.append(sp.Eq(parse(left), parse(right)))
            else:
                parsed.append(parse(part))
        syms = sorted(set().union(*[p.free_symbols for p in parsed]), key=str) or [x]
        sol = sp.solve(parsed, syms if len(parsed) > 1 else syms[0] if len(syms) == 1 else (x if x in syms else syms[0]), dict=True) if len(parsed) > 1 else sp.solve(parsed[0], x if x in syms else syms[0])
        if not sol:
            return "No solution found."
        return f"Solutions: {sol}\nLaTeX: {sp.latex(sol)}"
    expr = parse(expression)
    if task == "simplify":
        res = sp.simplify(expr)
    elif task == "factor":
        res = sp.factor(expr)
    elif task == "expand":
        res = sp.expand(expr)
    elif task == "derivative":
        res = sp.diff(expr, x)
    elif task == "integral":
        res = sp.integrate(expr, x)
    elif task == "limit":
        res = sp.limit(expr, x, parse(at or "0"))
    else:
        res = sp.nsimplify(expr) if expr.is_number and expr.is_rational is False else expr
        res = sp.simplify(res)
        if res.is_number:
            approx = sp.N(res, 12)
            exact = sp.latex(res)
            return f"Result: {res}" + ("" if res.is_Integer or res.is_Float else f" (≈ {approx})") + f"\nLaTeX: {exact}"
    return f"Result: {res}\nLaTeX: {sp.latex(res)}"


async def _calculate(ctx: ToolContext, a: dict[str, Any]) -> str:
    expression = str(a.get("expression") or "").strip()
    if not expression:
        raise SkillError("What should I calculate?")
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _compute, expression, str(a.get("task") or "evaluate"), str(a.get("variable") or "x"), str(a.get("at") or "")),
            timeout=8,
        )
    except TimeoutError:
        raise SkillError("That one is taking too long to work out.") from None
    except SkillError:
        raise
    except Exception as e:  # sympy parse and domain errors
        raise SkillError(f"I couldn't work that out: {str(e)[:80]}") from None


def register(reg: ToolRegistry) -> None:
    reg.register(Tool(
        "calculate",
        "Exact maths: arithmetic, percentages, algebra (solve equations, simplify, factor, expand), derivatives, integrals, limits. "
        "Use it for anything beyond a trivial sum instead of working it out yourself. Write expressions like 3*x^2+2*x-5; "
        "for solve give equations like x^2-5x+6=0.",
        obj({
            "expression": S("The expression or equation, e.g. '15% of 240', 'x^2-5x+6=0', 'sin(x)*x'"),
            "task": S("evaluate (default), solve, simplify, factor, expand, derivative, integral or limit"),
            "variable": S("Variable for solve/derivative/integral/limit, default x"),
            "at": S("Point for a limit, e.g. 0 or oo"),
        }, ["expression"]),
        _calculate, category="math"))
