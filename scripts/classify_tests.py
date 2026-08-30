"""Deterministic AST classifier for generated tests. No LLM involved anywhere
in this file -- the classification is a fixed decision procedure over the
test's syntax tree, so it is exactly reproducible and cannot drift between
runs or be swayed by a model's own opinion of its work.

Categories, exactly one per test:
  value     - asserts on a concrete expected value (an Eq/NotEq/Lt/LtE/Gt/GtE
              /In/NotIn comparison, or an equivalent unittest-style call)
  mock      - only assertions on mock call counts/args
  exception - pytest.raises (or unittest assertRaises) only
  existence - only `is not None`/`is None` or bare truthiness assertions
  none      - zero assert statements and no pytest.raises

Priority when a test mixes categories: value > mock > exception > existence
> none. Rationale: the taxonomy is trying to measure how much a test
actually specifies. If a test contains even one concrete value comparison
anywhere, it has cleared the bar "value" is meant to detect, regardless of
what else is in there; existence/bare-truthy checks are the weakest positive
signal, so they only win when nothing stronger is present.

CLAUDE.md's stated hypothesis, written before any test is classified: a
meaningful share of gate-passing tests will be `none` or `existence`,
meaning the gate selects differential probes rather than specifications.
The gate (pass on clean, fail on mutant) only proves a test is sensitive to
one syntactic neighbour of the source; it says nothing about whether the
test specifies correct behavior. This script measures the gap.

Reads results/generated_tests.jsonl, classifies every logged test regardless
of arm, and reports the distribution two ways per arm: over ALL generated
tests, and over the subset that would pass the kill gate (passed_on_clean
and killed_target both true) -- computed uniformly from the logged fields,
not from each arm's own keep/discard policy, so arms A/B/C are comparable on
the same basis. Writes results/assertion_taxonomy.json.
"""
from __future__ import annotations

import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from killcheck.logs import read_jsonl

CATEGORIES = ["value", "mock", "exception", "existence", "none"]

MOCK_ASSERT_METHODS = {
    "assert_called",
    "assert_called_once",
    "assert_called_with",
    "assert_called_once_with",
    "assert_any_call",
    "assert_has_calls",
    "assert_not_called",
}
MOCK_ATTRS = {"call_count", "called", "call_args", "call_args_list"}
VALUE_COMPARE_OPS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn)


def classify_test(source: str) -> str:
    """Return exactly one of CATEGORIES for a single test function's source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # A test that doesn't even parse cannot have asserted anything we can
        # verify; treat it the same as "no assertions" rather than crashing
        # a batch classification run over one bad row.
        return "none"

    has_value = has_mock = has_exception = has_existence = False

    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            kind = _classify_expr(node.test)
            if kind == "value":
                has_value = True
            elif kind == "mock":
                has_mock = True
            elif kind == "existence":
                has_existence = True
            # kind == None: an assert we can't confidently place is not
            # invented a new category for; it's simply not counted as
            # evidence for any of the four positive buckets.
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            if _is_mock_assert_call(node.value):
                has_mock = True
        elif isinstance(node, ast.With):
            for item in node.items:
                if _is_pytest_raises(item.context_expr):
                    has_exception = True
        elif isinstance(node, ast.Call) and (
            _is_pytest_raises(node) or _is_unittest_assert_raises(node)
        ):
            has_exception = True

    if has_value:
        return "value"
    if has_mock:
        return "mock"
    if has_exception:
        return "exception"
    if has_existence:
        return "existence"
    return "none"


def _classify_expr(expr: ast.expr) -> str | None:
    """Classify a single assert's test expression. Recurses into boolean
    combinations (assert a and b) and takes the strongest signal found."""
    if isinstance(expr, ast.BoolOp):
        kinds = [k for k in (_classify_expr(v) for v in expr.values) if k]
        for preferred in ("value", "mock", "existence"):
            if preferred in kinds:
                return preferred
        return None

    if isinstance(expr, ast.Compare):
        if any(isinstance(op, (ast.Is, ast.IsNot)) for op in expr.ops):
            return "existence"  # is None / is not None
        if any(isinstance(op, VALUE_COMPARE_OPS) for op in expr.ops):
            if _touches_mock_attr(expr):
                return "mock"
            return "value"
        return None

    if isinstance(expr, ast.Call) and _is_mock_assert_call(expr):
        return "mock"

    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
        inner = _classify_expr(expr.operand)
        return inner if inner else "existence"

    # Bare truthiness: assert some_name / assert some_call() / assert obj.attr
    if isinstance(expr, (ast.Name, ast.Attribute, ast.Call, ast.Subscript)):
        return "existence"

    return None


def _touches_mock_attr(compare: ast.Compare) -> bool:
    nodes = [compare.left, *compare.comparators]
    return any(isinstance(n, ast.Attribute) and n.attr in MOCK_ATTRS for n in nodes)


def _is_mock_assert_call(call: ast.Call) -> bool:
    return isinstance(call.func, ast.Attribute) and call.func.attr in MOCK_ASSERT_METHODS


def _is_pytest_raises(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "raises"
    if isinstance(func, ast.Name):
        return func.id == "raises"
    return False


def _is_unittest_assert_raises(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Attribute) and node.func.attr in (
        "assertRaises",
        "assertRaisesRegex",
    )


def main() -> int:
    records = read_jsonl(ROOT / "results" / "generated_tests.jsonl")
    if not records:
        print("results/generated_tests.jsonl is empty -- nothing to classify yet.")
        print("This is expected before any arm has run.")
        return 0

    by_arm_all: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_arm_kept: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for r in records:
        category = classify_test(r["test_source"])
        arm = r["arm"]
        by_arm_all[arm][category] += 1
        gate_would_keep = r["passed_on_clean"] and r["killed_target"]
        if gate_would_keep:
            by_arm_kept[arm][category] += 1

    report = {}
    for arm in sorted(by_arm_all):
        total_all = sum(by_arm_all[arm].values())
        total_kept = sum(by_arm_kept[arm].values())
        report[arm] = {
            "all_generated": {
                "total": total_all,
                "counts": {c: by_arm_all[arm][c] for c in CATEGORIES},
                "fractions": {
                    c: round(by_arm_all[arm][c] / total_all, 4) if total_all else 0.0
                    for c in CATEGORIES
                },
            },
            "gate_would_keep": {
                "total": total_kept,
                "counts": {c: by_arm_kept[arm][c] for c in CATEGORIES},
                "fractions": {
                    c: round(by_arm_kept[arm][c] / total_kept, 4) if total_kept else 0.0
                    for c in CATEGORIES
                },
            },
        }
        print(f"=== arm {arm} ===")
        print(f"  all generated (n={total_all}):")
        for c in CATEGORIES:
            print(f"    {c:10s} {by_arm_all[arm][c]:4d} ({report[arm]['all_generated']['fractions'][c]:.1%})")
        print(f"  gate would keep (n={total_kept}):")
        for c in CATEGORIES:
            print(f"    {c:10s} {by_arm_kept[arm][c]:4d} ({report[arm]['gate_would_keep']['fractions'][c]:.1%})")
        weak = report[arm]["gate_would_keep"]["fractions"]["none"] + report[arm]["gate_would_keep"]["fractions"]["existence"]
        if total_kept:
            print(f"  none+existence share of gate-would-keep: {weak:.1%}")

    out_path = ROOT / "results" / "assertion_taxonomy.json"
    out_path.write_text(json.dumps(report, indent=2))
    print(f"\nWrote {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
