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

Classification unit vs scoring unit -- these are NOT the same row, and this
file has to reconcile that. `classify_test()` classifies one test function.
But killcheck/baseline.py logs one row PER TARGET for arms A and B, with
test_source holding that arm's entire generated batch (up to 99 functions
for arm A on one target) -- because clean-pass/kill scoring for A and B
happens once per batch, not once per test. `_split_tests()` below
splits a row's batch back into individual test functions before
classification, so the taxonomy is computed over real tests (e.g. 556 for
arm A across this eval set), not over 10 batch-level rows where a single
strong test would have won the whole row's classification and hidden every
weak test alongside it.

That split fixes the CLASSIFICATION unit. It does not fix the SCORING unit,
and cannot: arms A and B are scored per batch, so there is no record of
which individual test in a 69-test batch caused which mutant to die. A
per-test "gate_would_keep" (passed_on_clean and killed_target) is therefore
only attributable when a row's batch splits into exactly one test -- true by
construction for arm C (one test per mutant, one row per test) and true
incidentally for any arm A/B target whose whole batch happened to be a
single test. For every multi-test batch, gate_would_keep is reported as not
computable rather than guessed at by attributing the batch's outcome to
every test in it, which would silently overcount. See CLAUDE.md and
README.md for the same limitation stated in the report's own words.
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


def _split_tests(source: str) -> list[str]:
    """Split a (possibly multi-function) logged test_source into the source
    of each individual test function or method it contains.

    Needed because killcheck/baseline.py logs one row per target for arms A
    and B -- test_source is that arm's whole generated batch for the target,
    not one test. Without this, classify_test()'s per-row classification
    would be won by whichever single test in a batch of up to 99 happens to
    contain a value-comparison assert, silently hiding every weaker test
    alongside it. Arm C logs one row per mutant already, so this is a no-op
    there (a batch of one splits into a list of one).

    Walks the full tree, not just top-level statements, and matches both
    `def` and `async def` -- checked against this run's actual output, not
    assumed: arm A organised `tenacity-stop`'s tests as methods on ~12
    `TestXxx` classes (a bare top-level-only check would have missed all 62
    of them), and wrote every `aiofiles-temptypes` test as `async def` (a
    bare `FunctionDef`-only check would have missed all 31). Both are common,
    legitimate pytest conventions, not malformed output -- the classifier
    needs to see them regardless of which shape a given target's tests took.

    Returns [] if the source is empty, doesn't parse, or contains no
    matching test function -- an empty or unparseable generated batch
    contributed zero tests, not one phantom "none"-category test.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]


def main() -> int:
    records = read_jsonl(ROOT / "results" / "generated_tests.jsonl")
    if not records:
        print("results/generated_tests.jsonl is empty -- nothing to classify yet.")
        print("This is expected before any arm has run.")
        return 0

    by_arm_all: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_arm_kept: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    # attributable = how many of an arm's rows split into exactly one test,
    # i.e. how many rows gate_would_keep could even be computed for. Reported
    # alongside gate_would_keep so a reader can see "0 of 10" rather than a
    # bare 0 that looks like a measured result instead of an unmeasurable one.
    attributable_rows: dict[str, int] = defaultdict(int)
    total_rows: dict[str, int] = defaultdict(int)
    # Raw generation volume, per arm per target -- its own result (see
    # CHANGELOG): volume without kills is itself informative about unguided
    # generation, and is reported in Table 1 alongside the kill counts.
    by_arm_target_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for r in records:
        arm = r["arm"]
        tests = _split_tests(r["test_source"])
        by_arm_target_counts[arm][r["target"]] += len(tests)
        total_rows[arm] += 1

        # gate_would_keep is only attributable to an individual test when the
        # row's batch IS exactly that one test -- true by construction for
        # arm C, true incidentally for an arm A/B target whose whole batch
        # happened to be a single test. A multi-test batch's
        # passed_on_clean/killed_target describes the batch, not any one
        # test in it (see this file's docstring and CLAUDE.md).
        attributable = len(tests) == 1
        gate_would_keep = attributable and r["passed_on_clean"] and r["killed_target"]
        if attributable:
            attributable_rows[arm] += 1

        for t in tests:
            category = classify_test(t)
            by_arm_all[arm][category] += 1
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
                "attributable_rows": attributable_rows[arm],
                "total_rows": total_rows[arm],
                "counts": {c: by_arm_kept[arm][c] for c in CATEGORIES},
                "fractions": {
                    c: round(by_arm_kept[arm][c] / total_kept, 4) if total_kept else 0.0
                    for c in CATEGORIES
                },
            },
            "tests_by_target": dict(by_arm_target_counts[arm]),
            "tests_generated_pooled": sum(by_arm_target_counts[arm].values()),
        }
        print(f"=== arm {arm} ===")
        print(f"  tests generated (n={report[arm]['tests_generated_pooled']}, "
              f"pooled across {total_rows[arm]} target-batches):")
        for target, n in sorted(by_arm_target_counts[arm].items()):
            print(f"    {target:22s} {n:3d}")
        print(f"  all generated (n={total_all}):")
        for c in CATEGORIES:
            print(f"    {c:10s} {by_arm_all[arm][c]:4d} ({report[arm]['all_generated']['fractions'][c]:.1%})")
        if attributable_rows[arm] == 0:
            print(f"  gate would keep: NOT COMPUTABLE -- 0 of {total_rows[arm]} "
                  f"target-batches for arm {arm} were a single test (batch-level "
                  f"scoring can't be attributed to one test in a multi-test batch)")
        else:
            print(f"  gate would keep (n={total_kept}, from "
                  f"{attributable_rows[arm]} of {total_rows[arm]} single-test batches):")
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
