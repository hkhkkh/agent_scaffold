"""Safe-ish expression evaluation for Condition nodes.

We never call :func:`eval` on raw user input. Instead the expression is parsed
to an AST and walked against a strict whitelist of node types and callables.
Anything outside the whitelist raises :class:`ExpressionError`.

**This is a correctness guard, not a security boundary.** It exists so a typo in
a condition produces a clear error instead of surprising behaviour, and so a
LLM-authored condition cannot reach ``__import__``. The Code node is the
deliberate escape hatch for anything richer.
"""

from __future__ import annotations

import ast
import operator
from typing import Any, Callable

__all__ = ["ExpressionError", "evaluate", "SAFE_FUNCTIONS"]


class ExpressionError(ValueError):
    """Raised for malformed or disallowed expressions."""


_BIN_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_CMP_OPS: dict[type[ast.cmpop], Callable[[Any, Any], Any]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}

_UNARY_OPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.Not: operator.not_,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_len(value: Any) -> int:
    try:
        return len(value)
    except TypeError:
        return 0


def _describe(value: Any) -> str:
    """How to name a value in an error a workflow author will read.

    ``None`` is called out by name because in this language it almost always
    means "that field is not there" rather than "the field holds null" — and the
    fix is entirely different between those two.
    """
    if value is None:
        return "None (a missing field?)"
    return type(value).__name__


#: Callables a condition expression may use. Deliberately tiny.
SAFE_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "len": _safe_len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "sorted": sorted,
    "sum": sum,
    "any": any,
    "all": all,
    "lower": lambda value: str(value).lower(),
    "upper": lambda value: str(value).upper(),
    "strip": lambda value: str(value).strip(),
    "contains": lambda haystack, needle: needle in haystack,
    "startswith": lambda value, prefix: str(value).startswith(prefix),
    "endswith": lambda value, suffix: str(value).endswith(suffix),
}

_SAFE_CONSTANTS: dict[str, Any] = {
    "True": True,
    "False": False,
    "None": None,
    "true": True,
    "false": False,
    "null": None,
}


def evaluate(expression: str, variables: dict[str, Any]) -> Any:
    """Evaluate ``expression`` with ``variables`` in scope.

    Supports dotted lookups (``vars.node_1.out``) and subscripting over both
    dicts and lists, so an author can reach into a node's output naturally.
    """
    if expression is None or str(expression).strip() == "":
        raise ExpressionError("Expression is empty.")

    try:
        tree = ast.parse(str(expression).strip(), mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"Invalid expression syntax: {exc.msg}") from exc

    scope = {**_SAFE_CONSTANTS, **SAFE_FUNCTIONS, **variables}
    return _eval_node(tree.body, scope)


def _eval_node(node: ast.AST, scope: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        if node.id in scope:
            return scope[node.id]
        raise ExpressionError(
            f"Unknown name '{node.id}'. Available: {', '.join(sorted(scope))}"
        )

    if isinstance(node, ast.BoolOp):
        # Short-circuiting, as Python does. Evaluating every operand up front
        # meant `a and b` still raised from `b` when `a` was already false — so
        # the ordinary guard `x and x < 0.8` guarded nothing, which is the shape
        # a workflow author reaches for when a field may be absent.
        if isinstance(node.op, ast.And):
            for value in node.values:
                if not _eval_node(value, scope):
                    return False
            return True
        for value in node.values:
            if _eval_node(value, scope):
                return True
        return False

    if isinstance(node, ast.BinOp):
        handler = _BIN_OPS.get(type(node.op))
        if handler is None:
            raise ExpressionError(f"Operator '{type(node.op).__name__}' is not allowed.")
        return handler(_eval_node(node.left, scope), _eval_node(node.right, scope))

    if isinstance(node, ast.UnaryOp):
        handler = _UNARY_OPS.get(type(node.op))
        if handler is None:
            raise ExpressionError(f"Unary '{type(node.op).__name__}' is not allowed.")
        return handler(_eval_node(node.operand, scope))

    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, scope)
        for op, comparator in zip(node.ops, node.comparators):
            handler = _CMP_OPS.get(type(op))
            if handler is None:
                raise ExpressionError(f"Comparison '{type(op).__name__}' is not allowed.")
            right = _eval_node(comparator, scope)
            try:
                matched = handler(left, right)
            except TypeError as exc:
                # A missing field reads as `None`, and `None < 0.8` raises. That
                # is the commonest way a condition breaks: the node it points at
                # stopped returning the key, or never did. Reporting it as a
                # TypeError leaves the caller (`control.py` catches only
                # ExpressionError) with an unhandled crash and no hint that a
                # field is absent — so it is re-raised as what it actually is.
                raise ExpressionError(
                    f"Cannot compare {_describe(left)} with {_describe(right)} "
                    f"using '{type(op).__name__.lower()}': {exc}"
                ) from exc
            if not matched:
                return False
            left = right
        return True

    if isinstance(node, ast.IfExp):
        branch = node.body if _eval_node(node.test, scope) else node.orelse
        return _eval_node(branch, scope)

    if isinstance(node, ast.Subscript):
        container = _eval_node(node.value, scope)
        key = _eval_node(node.slice, scope)
        try:
            return container[key]
        except (KeyError, IndexError, TypeError):
            return None

    if isinstance(node, ast.Attribute):
        target = _eval_node(node.value, scope)
        if isinstance(target, dict):
            return target.get(node.attr)
        return getattr(target, node.attr, None)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ExpressionError("Only direct calls to whitelisted functions are allowed.")
        func = scope.get(node.func.id)
        if not callable(func) or node.func.id not in SAFE_FUNCTIONS:
            raise ExpressionError(f"Function '{node.func.id}' is not available.")
        if node.keywords:
            raise ExpressionError("Keyword arguments are not supported.")
        return func(*[_eval_node(arg, scope) for arg in node.args])

    if isinstance(node, ast.List):
        return [_eval_node(element, scope) for element in node.elts]

    if isinstance(node, ast.Tuple):
        return tuple(_eval_node(element, scope) for element in node.elts)

    if isinstance(node, ast.Dict):
        return {
            _eval_node(k, scope): _eval_node(v, scope)
            for k, v in zip(node.keys, node.values)
        }

    raise ExpressionError(f"Expression element '{type(node).__name__}' is not allowed.")
