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
every test in it, which would silently overcount. See METHODOLOGY.md and
README.md for the same limitation stated in the report's own words.

`classify_test` and its supporting decision procedure now live in
killcheck/classify.py, not here -- moved so killcheck/agent.py (part of the
installable library) doesn't depend on scripts/ (eval-set batch tooling
that itself depends on killcheck) for arm C's per-draft taxonomy. Pure code
motion, re-verified by AST-diffing against the pre-move original; see
CHANGELOG.md. `_split_tests` and `main()` below are batch-analysis-specific
(they read results/generated_tests.jsonl) and stay here, unchanged.
"""
from __future__ import annotations

import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from killcheck.classify import CATEGORIES, classify_test
from killcheck.logs import read_jsonl, UNIT_SINGLE_TEST_FUNCTION, UNIT_TEST_BATCH


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

    # CHECK C (unit metadata) -- see CHANGELOG.md's "The scorer itself was
    # never checked" entry. Bug 4 was exactly this file being handed a
    # whole batch when it expected one test function's source, silently.
    # generated_tests.jsonl rows written after that fix carry an explicit
    # `unit` field (killcheck.logs.UNIT_SINGLE_TEST_FUNCTION /
    # UNIT_TEST_BATCH); assert it's a recognized value rather than assuming
    # test_source means what this file expects. Rows logged before the
    # field existed have none -- counted and reported, not silently treated
    # as passing a check they were never subject to.
    legacy_rows_without_unit = 0
    for r in records:
        arm = r["arm"]
        unit = r.get("unit")
        if unit is None:
            legacy_rows_without_unit += 1
        else:
            assert unit in (UNIT_SINGLE_TEST_FUNCTION, UNIT_TEST_BATCH), (
                f"generated_tests.jsonl row for arm {arm}/{r['target']} has an unrecognized "
                f"unit {unit!r} -- this file doesn't know how to interpret its test_source"
            )
        tests = _split_tests(r["test_source"])
        by_arm_target_counts[arm][r["target"]] += len(tests)
        total_rows[arm] += 1

        # gate_would_keep is only attributable to an individual test when the
        # row's batch IS exactly that one test -- true by construction for
        # arm C, true incidentally for an arm A/B target whose whole batch
        # happened to be a single test. A multi-test batch's
        # passed_on_clean/killed_target describes the batch, not any one
        # test in it (see this file's docstring and METHODOLOGY.md).
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
            # CHECK C (unit metadata): "unit" on each block says what
            # `total`/`counts`/`fractions` there are counting. all_generated
            # and gate_would_keep's own counts/fractions count test
            # FUNCTIONS (post-_split_tests, one per test regardless of which
            # arm wrote it) -- but attributable_rows/total_rows inside
            # gate_would_keep count generated_tests.jsonl ROWS (one per
            # arm-target for A/B, one per attempt for C), a different unit
            # sitting in the same block. Making both explicit is exactly
            # the distinction bug 4 lacked.
            "all_generated": {
                "unit": "test_function",
                "total": total_all,
                "counts": {c: by_arm_all[arm][c] for c in CATEGORIES},
                "fractions": {
                    c: round(by_arm_all[arm][c] / total_all, 4) if total_all else 0.0
                    for c in CATEGORIES
                },
            },
            "gate_would_keep": {
                "unit": "test_function",
                "total": total_kept,
                "attributable_rows": attributable_rows[arm],
                "total_rows": total_rows[arm],
                "attributable_rows_unit": "generated_tests_jsonl_row",
                "counts": {c: by_arm_kept[arm][c] for c in CATEGORIES},
                "fractions": {
                    c: round(by_arm_kept[arm][c] / total_kept, 4) if total_kept else 0.0
                    for c in CATEGORIES
                },
            },
            "tests_by_target": dict(by_arm_target_counts[arm]),
            "tests_by_target_unit": "test_function",
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
    if legacy_rows_without_unit:
        print(
            f"note: {legacy_rows_without_unit} of {len(records)} generated_tests.jsonl row(s) "
            f"predate the `unit` field (CHECK C) and were not checked against it."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
