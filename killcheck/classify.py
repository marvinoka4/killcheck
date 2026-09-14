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

METHODOLOGY.md's stated hypothesis, written before any test is classified: a
meaningful share of gate-passing tests will be `none` or `existence`,
meaning the gate selects differential probes rather than specifications.
The gate (pass on clean, fail on mutant) only proves a test is sensitive to
one syntactic neighbour of the source; it says nothing about whether the
test specifies correct behavior.

Extracted from scripts/classify_tests.py (pure code motion, no logic
changed -- every function body below is byte-identical to what previously
lived there; verified by AST-diffing old vs new before trusting this, same
as killcheck/verify_core.py's extraction) so `classify_test` is part of the
installable `killcheck` package. killcheck/agent.py needs it for arm C's
per-draft assertion taxonomy -- a library module (killcheck/agent.py)
depending on scripts/ (eval-set batch tooling that itself depends on
killcheck) was the wrong direction and would have broken `killcheck harden`
for anyone who installed this package without scripts/ also being on
sys.path. scripts/classify_tests.py now imports from here instead of
defining these itself; its own batch-analysis behavior (reading
results/generated_tests.jsonl, writing results/assertion_taxonomy.json) is
unchanged. See CHANGELOG.md.
"""
from __future__ import annotations

import ast

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

# unittest.TestCase's assertion methods -- checked against a real model
# response before assuming coverage: a test written as
# `self.assertEqual(result.count(x), 0)` has no `ast.Assert` node at all, so
# without this it silently classifies as "none" (zero assertions found)
# despite asserting a concrete expected value, same as `assert a == b` would.
# assertRaises/assertRaisesRegex are handled separately by
# _is_unittest_assert_raises, already in place before this fix.
UNITTEST_VALUE_METHODS = {
    "assertEqual", "assertNotEqual",
    "assertIn", "assertNotIn",
    "assertGreater", "assertGreaterEqual", "assertLess", "assertLessEqual",
    "assertAlmostEqual", "assertNotAlmostEqual",
    "assertListEqual", "assertDictEqual", "assertSetEqual", "assertTupleEqual",
    "assertSequenceEqual", "assertMultiLineEqual", "assertCountEqual",
}
UNITTEST_EXISTENCE_METHODS = {
    "assertTrue", "assertFalse",
    "assertIs", "assertIsNot",
    "assertIsNone", "assertIsNotNone",
    "assertIsInstance", "assertNotIsInstance",
}


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
            call = node.value
            if _is_mock_assert_call(call):
                has_mock = True
            elif isinstance(call.func, ast.Attribute) and call.func.attr in UNITTEST_VALUE_METHODS:
                has_value = True
            elif isinstance(call.func, ast.Attribute) and call.func.attr in UNITTEST_EXISTENCE_METHODS:
                has_existence = True
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
