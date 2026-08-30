"""
Deterministic AST mutation engine.

Generates one mutant per mutable site. Mutants are ordered deterministically by
(lineno, col_offset, operator) so the same source always yields the same mutant
IDs across runs. This matters: mutant IDs appear in evaluation output and must be
stable for the baseline and agent arms to be comparable.
"""

from __future__ import annotations

import ast
import copy
import hashlib
from dataclasses import dataclass, asdict
from typing import Iterator


# ---------------------------------------------------------------------------
# Operator tables
# ---------------------------------------------------------------------------

COMPARE_SWAPS = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}

BINOP_SWAPS = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.FloorDiv,
    ast.Div: ast.Mult,
    ast.FloorDiv: ast.Mult,
    ast.Mod: ast.Mult,
    ast.Pow: ast.Mult,
}

BOOLOP_SWAPS = {
    ast.And: ast.Or,
    ast.Or: ast.And,
}

OPERATOR_NAMES = {
    "compare": "comparison boundary / equality flip",
    "binop": "arithmetic operator swap",
    "boolop": "and/or swap",
    "constant": "literal value change",
    "unary_not": "removed logical negation",
    "raise_removed": "exception raise replaced with pass",
    "return_none": "return value replaced with None",
}


@dataclass(frozen=True)
class Mutant:
    id: str
    module: str
    lineno: int
    col_offset: int
    operator: str
    description: str
    original_line: str
    mutated_line: str
    source: str  # full mutated module source

    def to_dict(self, include_source: bool = False) -> dict:
        d = asdict(self)
        if not include_source:
            d.pop("source")
        return d

    def summary(self) -> str:
        return (
            f"{self.id} | line {self.lineno} | {self.operator}\n"
            f"  before: {self.original_line.strip()}\n"
            f"  after:  {self.mutated_line.strip()}"
        )


# ---------------------------------------------------------------------------
# Site discovery
# ---------------------------------------------------------------------------


def _sites(tree: ast.AST) -> list[tuple[int, int, str, int]]:
    """Return (lineno, col_offset, operator, index_within_node) for each mutable site."""
    found: list[tuple[int, int, str, int]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                if type(op) in COMPARE_SWAPS:
                    found.append((node.lineno, node.col_offset, "compare", i))

        elif isinstance(node, ast.BinOp):
            if type(node.op) in BINOP_SWAPS:
                found.append((node.lineno, node.col_offset, "binop", 0))

        elif isinstance(node, ast.BoolOp):
            if type(node.op) in BOOLOP_SWAPS:
                found.append((node.lineno, node.col_offset, "boolop", 0))

        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            found.append((node.lineno, node.col_offset, "unary_not", 0))

        elif isinstance(node, ast.Constant):
            if _mutable_constant(node.value):
                found.append((node.lineno, node.col_offset, "constant", 0))

        elif isinstance(node, ast.Raise) and node.exc is not None:
            found.append((node.lineno, node.col_offset, "raise_removed", 0))

        elif isinstance(node, ast.Return) and node.value is not None:
            if not (isinstance(node.value, ast.Constant) and node.value.value is None):
                found.append((node.lineno, node.col_offset, "return_none", 0))

    # Deterministic ordering.
    return sorted(set(found))


def _mutable_constant(value) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return True
    if isinstance(value, str):
        # Skip docstrings-sized blobs; they are almost always equivalent mutants.
        return 0 < len(value) <= 40
    return False


def _mutate_constant(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 1.0
    if isinstance(value, str):
        return "" if value else "x"
    return value


# ---------------------------------------------------------------------------
# Transformer
# ---------------------------------------------------------------------------


class _SingleSiteMutator(ast.NodeTransformer):
    """Applies exactly one mutation, at the site matching `target`."""

    def __init__(self, target: tuple[int, int, str, int]):
        self.target = target
        self.applied = False
        self.description = ""

    def _matches(self, node, operator: str, index: int = 0) -> bool:
        return (
            not self.applied
            and (getattr(node, "lineno", -1), getattr(node, "col_offset", -1), operator, index)
            == self.target
        )

    def visit_Compare(self, node: ast.Compare):
        self.generic_visit(node)
        for i, op in enumerate(node.ops):
            if self._matches(node, "compare", i) and type(op) in COMPARE_SWAPS:
                new_op = COMPARE_SWAPS[type(op)]()
                self.description = f"{type(op).__name__} -> {type(new_op).__name__}"
                node.ops[i] = new_op
                self.applied = True
                break
        return node

    def visit_BinOp(self, node: ast.BinOp):
        self.generic_visit(node)
        if self._matches(node, "binop") and type(node.op) in BINOP_SWAPS:
            new_op = BINOP_SWAPS[type(node.op)]()
            self.description = f"{type(node.op).__name__} -> {type(new_op).__name__}"
            node.op = new_op
            self.applied = True
        return node

    def visit_BoolOp(self, node: ast.BoolOp):
        self.generic_visit(node)
        if self._matches(node, "boolop") and type(node.op) in BOOLOP_SWAPS:
            new_op = BOOLOP_SWAPS[type(node.op)]()
            self.description = f"{type(node.op).__name__} -> {type(new_op).__name__}"
            node.op = new_op
            self.applied = True
        return node

    def visit_UnaryOp(self, node: ast.UnaryOp):
        self.generic_visit(node)
        if self._matches(node, "unary_not") and isinstance(node.op, ast.Not):
            self.description = "dropped `not`"
            self.applied = True
            return node.operand
        return node

    def visit_Constant(self, node: ast.Constant):
        if self._matches(node, "constant") and _mutable_constant(node.value):
            new_value = _mutate_constant(node.value)
            self.description = f"{node.value!r} -> {new_value!r}"
            self.applied = True
            return ast.copy_location(ast.Constant(value=new_value), node)
        return node

    def visit_Raise(self, node: ast.Raise):
        self.generic_visit(node)
        if self._matches(node, "raise_removed") and node.exc is not None:
            self.description = "raise -> pass"
            self.applied = True
            return ast.copy_location(ast.Pass(), node)
        return node

    def visit_Return(self, node: ast.Return):
        self.generic_visit(node)
        if self._matches(node, "return_none") and node.value is not None:
            self.description = "return <expr> -> return None"
            self.applied = True
            return ast.copy_location(
                ast.Return(value=ast.Constant(value=None)), node
            )
        return node


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_mutants(source: str, module_name: str) -> list[Mutant]:
    """Generate one mutant per mutable site in `source`.

    Mutants whose unparsed output is identical to the unparsed original are
    dropped: they are provably equivalent and cannot be killed by any test.
    """
    tree = ast.parse(source)
    baseline_unparsed = ast.unparse(ast.parse(source))
    original_lines = source.splitlines()

    mutants: list[Mutant] = []
    for site in _sites(tree):
        lineno, col, operator, index = site
        mutator = _SingleSiteMutator(site)
        mutated_tree = mutator.visit(copy.deepcopy(tree))
        if not mutator.applied:
            continue

        ast.fix_missing_locations(mutated_tree)
        try:
            mutated_source = ast.unparse(mutated_tree)
        except Exception:
            continue

        if mutated_source == baseline_unparsed:
            continue  # equivalent mutant

        original_line = (
            original_lines[lineno - 1] if 0 < lineno <= len(original_lines) else ""
        )
        # ast.unparse renormalises formatting, so line numbers in `mutated_source`
        # do not correspond to `source`. Recover the changed line by diffing the
        # mutated output against the unparsed original.
        mutated_line = _first_difference(baseline_unparsed, mutated_source)

        digest = hashlib.sha1(
            f"{module_name}:{lineno}:{col}:{operator}:{index}".encode()
        ).hexdigest()[:8]

        mutants.append(
            Mutant(
                id=f"M-{digest}",
                module=module_name,
                lineno=lineno,
                col_offset=col,
                operator=operator,
                description=mutator.description,
                original_line=original_line,
                mutated_line=mutated_line,
                source=mutated_source,
            )
        )

    return mutants


def _first_difference(before: str, after: str) -> str:
    """Return the first line of `after` that differs from `before`."""
    b = before.splitlines()
    a = after.splitlines()
    for i, line in enumerate(a):
        if i >= len(b) or b[i] != line:
            return line
    return ""


def iter_operators() -> Iterator[tuple[str, str]]:
    yield from OPERATOR_NAMES.items()
