"""Scorer-validation checks A, B, and C -- the canary applied to the
SCORER, not the mutation-execution path. See CHANGELOG.md's "The scorer
itself was never checked" entry (credit: Zain Dana Harper,
dev.to/zaindanaharper) for why this exists: the canary proves a mutation
reaches the interpreter, the determinism gate proves execution is
isolated, the byte-size canary proves bytecode isn't stale -- none of them
proves that the code turning a subprocess exit code into
killed/survived/timeout/error, and the code aggregating that across
mutants, is itself correct. Three of this project's ten instrument bugs so
far were exactly that kind of bug, and not one was caught by a standing
check. CHECK A found an eleventh on its first real run -- see CHANGELOG.md's
"instrument bug eleven" entry.

CHECK A -- known-outcome fixtures. Five known-by-construction outcomes,
each run through the REAL scoring pipeline (killcheck.runner._evaluate_one
and killcheck.agent.gate_check / gate_decision -- production code, not a
reimplementation):

  1. a test that provably kills its mutant       -> must score killed
  2. a test that provably cannot kill it          -> must score survived
  3. a test that fails on clean source            -> must be recorded as
                                                      clean-fail, never
                                                      credited as a kill
  4. a test that hangs                            -> must score timeout
  5. an unparseable test file                     -> must score as a
                                                      collection error,
                                                      distinctly

CHECK B -- conservation invariants (killcheck/invariants.py). Exercised
here directly against both conserving and deliberately non-conserving
inputs, on top of being wired as standing checks into
scripts/verify_targets.py, killcheck/baseline.py, and killcheck/agent.py.

CHECK C -- unit metadata (killcheck/logs.py's `unit` field on
generated_tests.jsonl rows, and the consumer assertions in
scripts/classify_tests.py and scripts/ablate.py). Exercised here against a
deliberately mislabeled row.

Every check here has its own meta-test: a deliberately broken variant,
verified to make the check FAIL, run before the check is trusted -- same
discipline as scripts/test_pyc_exclusion.py's
test_property_check_catches_the_deliberately_stale_case. A check that
cannot be shown to fail on a known-broken input proves nothing about a
real one.

Run directly (`python3 scripts/test_scorer_checks.py`) or via pytest.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from killcheck.agent import gate_check, gate_decision
from killcheck.engine import Mutant, generate_mutants
from killcheck.invariants import (
    assert_ids_subset,
    assert_no_duplicates,
    assert_outcome_conservation,
    assert_pooled_conservation,
    assert_reachability_conservation,
)
from killcheck.logs import UNIT_SINGLE_TEST_FUNCTION, UNIT_TEST_BATCH
from killcheck.runner import Target, _evaluate_one, verify_clean

# ---------------------------------------------------------------------------
# Fixture: one real module, one real engine.py-generated mutant, and five
# test files/candidate-test-strings with KNOWN scoring outcomes by
# construction -- not asserted by inspection, verified directly below in
# _sanity_check_fixture() before anything else trusts them.
# ---------------------------------------------------------------------------

MODULE_SOURCE = "def check(a, b):\n    return a == b\n"

# Candidate test bodies, one per known-outcome case. Each is a complete,
# standalone test function (mirrors what agent.py's real generated tests
# look like -- an import plus one def).
CANDIDATE_KILLING = "from mod import check\n\n\ndef test_check():\n    assert check(1, 1) is True\n"
CANDIDATE_NONKILLING = "from mod import check\n\n\ndef test_check():\n    assert check(1, 1) is not None\n"
CANDIDATE_CLEANFAIL = "from mod import check\n\n\ndef test_check():\n    assert check(1, 1) == 'nonsense'\n"
CANDIDATE_HANGING = "import time\n\n\ndef test_check():\n    time.sleep(30)\n"
CANDIDATE_UNPARSEABLE = "def test_check(:\n    pass\n"


def _real_compare_mutant(module_source: str = MODULE_SOURCE) -> Mutant:
    """The real, engine.py-generated `==` -> `!=` mutant for MODULE_SOURCE
    -- not hand-constructed, so CHECK A exercises the same mutant shape
    every other check in this project does."""
    for m in generate_mutants(module_source, "mod.py"):
        if m.operator == "compare":
            return m
    raise AssertionError("fixture module produced no compare mutant -- fixture is broken")


def _build_evaluate_one_fixture(tmp: Path) -> dict[str, Target]:
    """One project per known-outcome case, each a Target whose EXISTING
    test file already contains that case's test -- used to exercise
    _evaluate_one directly (module-mutation path), which has no concept of
    a separately-supplied candidate test."""
    targets = {}
    files = {
        "killing": CANDIDATE_KILLING,
        "nonkilling": CANDIDATE_NONKILLING,
        "hanging": CANDIDATE_HANGING,
        "unparseable": CANDIDATE_UNPARSEABLE,
    }
    for name, test_body in files.items():
        root = tmp / f"proj_{name}"
        root.mkdir()
        (root / "mod.py").write_text(MODULE_SOURCE)
        (root / "tests").mkdir()
        (root / "tests" / "test_mod.py").write_text(test_body)
        targets[name] = Target(
            name=f"scorer-check-{name}",
            project_root=root,
            module_path=Path("mod.py"),
            test_command=[sys.executable, "-m", "pytest", "tests/test_mod.py", "-q"],
        )
    return targets


def _build_gate_check_fixture(tmp: Path) -> Target:
    """One project with a minimal, valid existing test file (imports only,
    no tests) -- gate_check appends a candidate test's source to this file,
    exactly like agent.py's real draft_and_gate does."""
    root = tmp / "proj_gate"
    root.mkdir()
    (root / "mod.py").write_text(MODULE_SOURCE)
    (root / "tests").mkdir()
    (root / "tests" / "test_mod.py").write_text("from mod import check\n")
    return Target(
        name="scorer-check-gate",
        project_root=root,
        module_path=Path("mod.py"),
        test_command=[sys.executable, "-m", "pytest", "tests/test_mod.py", "-q"],
    )


def _sanity_check_fixture() -> None:
    """Confirm the fixture module and mutant are what this file assumes
    before anything downstream trusts them -- e.g. that the module really
    does parse, the mutant really is the == -> != flip, and clean source
    really does say check(1, 1) is True. A wrong assumption here would
    make every "known outcome" below not actually known."""
    mutant = _real_compare_mutant()
    assert mutant.operator == "compare"
    assert "==" in mutant.original_line and "!=" in mutant.mutated_line, (
        f"fixture mutant is not the expected == -> != flip: {mutant.summary()}"
    )
    ns: dict = {}
    exec(MODULE_SOURCE, ns)
    assert ns["check"](1, 1) is True, "fixture module's clean behavior is not what this file assumes"
    ns2: dict = {}
    exec(mutant.source, ns2)
    assert ns2["check"](1, 1) is False, "fixture mutant's behavior is not what this file assumes"


# ---------------------------------------------------------------------------
# CHECK A, via _evaluate_one (module-mutation path)
# ---------------------------------------------------------------------------


def test_evaluate_one_known_outcomes() -> None:
    """Cases 1 (killing), 2 (non-killing), 4 (hangs), 5 (unparseable test
    file) through the real, unmodified _evaluate_one -- production code,
    not a reimplementation. Case 3 (clean-fail) has no meaning for
    _evaluate_one in isolation -- it has no "clean" concept of its own, it
    just scores one exact module content against one test suite -- so it's
    covered separately below, via gate_check/gate_decision, which is where
    the actual clean-vs-mutant distinction lives."""
    with tempfile.TemporaryDirectory(prefix="scorer-check-a-") as tmp_str:
        tmp = Path(tmp_str)
        targets = _build_evaluate_one_fixture(tmp)
        mutant = _real_compare_mutant()

        # verify_clean only makes sense for fixtures whose test file is
        # supposed to pass on unmutated code -- the "unparseable" fixture's
        # test file is deliberately broken regardless of the module, so
        # verify_clean would (correctly) reject it before _evaluate_one is
        # ever reached. score_target's own real precondition chain works
        # the same way (verify_clean gates score_target entirely, before
        # any mutant work begins) -- this case tests _evaluate_one's own
        # direct classification of an already-broken test file, bypassing
        # that higher-level gate deliberately, not a real production path.
        for name, t in targets.items():
            if name != "unparseable":
                verify_clean(t)

        killing_result = _evaluate_one(targets["killing"], mutant, timeout=10)
        assert killing_result.outcome == "killed", (
            f"a test that asserts on the exact value the mutation changes must score 'killed', "
            f"got {killing_result.outcome!r}"
        )

        nonkilling_result = _evaluate_one(targets["nonkilling"], mutant, timeout=10)
        assert nonkilling_result.outcome == "survived", (
            f"a test that asserts something the mutation does not affect must score 'survived', "
            f"got {nonkilling_result.outcome!r}"
        )

        hanging_result = _evaluate_one(targets["hanging"], mutant, timeout=2)
        assert hanging_result.outcome == "timeout", (
            f"a hanging test must score 'timeout', not some other path to 'killed', "
            f"got {hanging_result.outcome!r}"
        )

        unparseable_result = _evaluate_one(targets["unparseable"], mutant, timeout=10)
        assert unparseable_result.outcome == "error", (
            f"an unparseable test FILE must score as a collection error, distinctly, "
            f"got {unparseable_result.outcome!r}"
        )

    print("[CHECK A] _evaluate_one: killing->killed, non-killing->survived, "
          "hanging->timeout, unparseable->error -- all confirmed")


# ---------------------------------------------------------------------------
# CHECK A, via gate_check + gate_decision (candidate-test path -- this is
# what agent.py's arm C actually uses to decide kept/discarded)
# ---------------------------------------------------------------------------


def test_gate_check_known_outcomes() -> None:
    """All five cases through the real gate_check() and gate_decision() --
    the exact functions draft_and_gate calls, not a parallel
    reimplementation (gate_decision is literally the same function
    draft_and_gate uses; see killcheck/agent.py). No model call: the
    candidate test sources are the known-outcome fixtures above, standing
    in for what a model would have produced."""
    with tempfile.TemporaryDirectory(prefix="scorer-check-a-gate-") as tmp_str:
        tmp = Path(tmp_str)
        target = _build_gate_check_fixture(tmp)
        # No verify_clean() here -- the base fixture's test file is
        # deliberately import-only, zero test functions (gate_check appends
        # a candidate to it, exactly like draft_and_gate does), so it
        # reports pytest's own exit 5 (NO_TESTS_COLLECTED) standalone.
        # That's expected and not what this test is checking; real usage
        # (draft_and_gate) doesn't call verify_clean on the bare pre-
        # candidate file either -- gate_check's own clean-gate call, run
        # per case below with a candidate actually appended, is the real
        # precondition check.
        mutant = _real_compare_mutant()
        test_file = target.project_root / "tests" / "test_mod.py"

        # Case 1: killing candidate.
        clean = gate_check(target, test_file, CANDIDATE_KILLING, None)
        mutant_gate = gate_check(target, test_file, CANDIDATE_KILLING, mutant) if clean["outcome"] == "survived" else None
        verdict = gate_decision(clean, mutant_gate)
        assert verdict == {"passed_on_clean": True, "killed_target": True, "kept": True}, (
            f"killing candidate: expected passed_on_clean=killed_target=kept=True, got {verdict}"
        )

        # Case 2: non-killing candidate.
        clean = gate_check(target, test_file, CANDIDATE_NONKILLING, None)
        mutant_gate = gate_check(target, test_file, CANDIDATE_NONKILLING, mutant) if clean["outcome"] == "survived" else None
        verdict = gate_decision(clean, mutant_gate)
        assert verdict == {"passed_on_clean": True, "killed_target": False, "kept": False}, (
            f"non-killing candidate: expected passed_on_clean=True, killed_target=kept=False, got {verdict}"
        )

        # Case 3: clean-fail candidate -- must never be credited as a kill,
        # and the mutant gate must never even run once the clean gate fails
        # (mirrors draft_and_gate's own control flow exactly).
        clean = gate_check(target, test_file, CANDIDATE_CLEANFAIL, None)
        assert clean["outcome"] != "survived", (
            "fixture assumption broken: the clean-fail candidate was expected to fail on clean source"
        )
        mutant_gate = gate_check(target, test_file, CANDIDATE_CLEANFAIL, mutant) if clean["outcome"] == "survived" else None
        assert mutant_gate is None, "the mutant gate must never run once the clean gate already failed"
        verdict = gate_decision(clean, mutant_gate)
        assert verdict == {"passed_on_clean": False, "killed_target": False, "kept": False}, (
            f"clean-fail candidate: must never be recorded as a kill, got {verdict}"
        )

        # Case 4: hanging candidate.
        clean = gate_check(target, test_file, CANDIDATE_HANGING, None, timeout=2)
        assert clean["outcome"] == "timeout", f"hanging candidate's clean gate must be 'timeout', got {clean['outcome']!r}"
        verdict = gate_decision(clean, None)
        assert verdict == {"passed_on_clean": False, "killed_target": False, "kept": False}, (
            f"a clean gate that timed out must not be treated as passed_on_clean, got {verdict}"
        )

        # Case 5: unparseable candidate appended to an otherwise-valid file.
        clean = gate_check(target, test_file, CANDIDATE_UNPARSEABLE, None)
        assert clean["outcome"] == "error", (
            f"an unparseable candidate must score the augmented file as a collection error, "
            f"distinctly from 'killed', got {clean['outcome']!r}"
        )

    print("[CHECK A] gate_check/gate_decision: all five known-outcome cases confirmed, "
          "including clean-fail never reaching the mutant gate")


# ---------------------------------------------------------------------------
# CHECK A meta-test: prove the checks above would actually fail against a
# deliberately broken scorer, before trusting them.
# ---------------------------------------------------------------------------


def test_check_a_has_teeth_against_a_broken_scorer() -> None:
    """Deliberately break the scorer (force every subprocess exit code to
    0, i.e. "always survived" -- the single most flattering possible
    corruption) and confirm the known-outcome assertion for the KILLING
    case then fails. If it doesn't, CHECK A cannot be trusted to catch a
    real scorer bug of this shape."""
    with tempfile.TemporaryDirectory(prefix="scorer-check-a-meta-") as tmp_str:
        tmp = Path(tmp_str)
        targets = _build_evaluate_one_fixture(tmp)
        mutant = _real_compare_mutant()
        killing_target = targets["killing"]
        verify_clean(killing_target)

        class _AlwaysZero:
            returncode = 0
            stdout = ""
            stderr = ""

        caught = False
        with mock.patch("killcheck.runner.subprocess.run", return_value=_AlwaysZero()):
            result = _evaluate_one(killing_target, mutant, timeout=10)
            try:
                assert result.outcome == "killed", (
                    f"expected 'killed', got {result.outcome!r}"
                )
            except AssertionError:
                caught = True

        assert caught, (
            "meta-test failed: forcing every subprocess exit code to 0 should have made the "
            "killing-candidate assertion fail (broken scorer reports 'survived' for a real "
            "kill) -- it didn't, which means CHECK A would not catch this class of bug"
        )
    print("[CHECK A meta-test] confirmed: a scorer forced to always report success DOES fail "
          "the known-killing-outcome assertion -- CHECK A has teeth")


# ---------------------------------------------------------------------------
# CHECK B -- conservation invariants (killcheck/invariants.py)
# ---------------------------------------------------------------------------


def test_check_b_passes_on_conserving_data() -> None:
    """The invariant functions must not raise on real, self-consistent
    data -- exercised against the actual committed target_verification.json
    rather than synthetic input, so this also doubles as a live check on
    the committed eval-set data (see the standing checks wired into
    scripts/verify_targets.py for the same assertions run during a real
    scoring pass)."""
    import json

    verification = json.loads((ROOT / "results" / "target_verification.json").read_text())
    pooled = 0
    per_target = []
    for t in verification:
        if t.get("quarantined") or "outcome_counts" not in t:
            continue
        assert_outcome_conservation(t["outcome_counts"], t["total_mutants"], t["name"])
        assert_reachability_conservation(t["reachability_counts"], t["total_mutants"], t["name"])
        pooled += t["reachability_counts"]["reachable_survivor"]
        per_target.append(t["reachability_counts"]["reachable_survivor"])
    assert_pooled_conservation(pooled, per_target, "committed target_verification.json")
    print(f"[CHECK B] committed target_verification.json conserves on every check "
          f"({len(per_target)} targets, pooled reachable survivors={pooled})")


def test_check_b_has_teeth_against_broken_counts() -> None:
    """Meta-test: construct counts that deliberately do not conserve and
    confirm each invariant function raises. If any of these silently
    passed a broken input, CHECK B would prove nothing about real data."""
    broken_outcome = {"killed": 5, "survived": 3, "timeout": 0, "error": 0}  # sums to 8, not 10
    try:
        assert_outcome_conservation(broken_outcome, 10, "meta-test")
    except AssertionError:
        pass
    else:
        raise AssertionError("meta-test failed: assert_outcome_conservation did not catch a non-conserving count")

    broken_reach = {"reachable_survivor": 2, "unreachable": 1, "unknown_reachability_survivor": 0, "killed": 4}  # sums to 7, not 10
    try:
        assert_reachability_conservation(broken_reach, 10, "meta-test")
    except AssertionError:
        pass
    else:
        raise AssertionError("meta-test failed: assert_reachability_conservation did not catch a non-conserving count")

    try:
        assert_pooled_conservation(100, [30, 30, 30], "meta-test")  # 100 != 90
    except AssertionError:
        pass
    else:
        raise AssertionError("meta-test failed: assert_pooled_conservation did not catch a wrong pooled figure")

    try:
        assert_ids_subset({"M-real", "M-stale"}, {"M-real"}, "meta-test")
    except AssertionError:
        pass
    else:
        raise AssertionError("meta-test failed: assert_ids_subset did not catch a stale id")

    try:
        assert_no_duplicates(["M-a", "M-b", "M-a"], "meta-test")
    except AssertionError:
        pass
    else:
        raise AssertionError("meta-test failed: assert_no_duplicates did not catch a duplicate")

    print("[CHECK B meta-test] confirmed: all five invariant functions correctly reject "
          "deliberately broken input -- CHECK B has teeth")


# ---------------------------------------------------------------------------
# CHECK C -- unit metadata (killcheck/logs.py, scripts/classify_tests.py)
# ---------------------------------------------------------------------------


def test_check_c_unit_field_round_trips() -> None:
    """log_generated_test requires `unit` (no default) and rejects an
    unrecognized value -- exercised directly against the real function,
    not a description of what it should do."""
    from killcheck.logs import log_generated_test, read_jsonl

    with tempfile.TemporaryDirectory(prefix="scorer-check-c-") as tmp_str:
        results_dir = Path(tmp_str)
        log_generated_test(
            results_dir=results_dir, arm="C", target="t", mutant_id="M-x", attempt=1,
            passed_on_clean=True, killed_target=True, test_source="def test_x(): pass",
            prompt_tokens=1, completion_tokens=1, unit=UNIT_SINGLE_TEST_FUNCTION,
        )
        log_generated_test(
            results_dir=results_dir, arm="A", target="t", mutant_id="", attempt=1,
            passed_on_clean=True, killed_target=True, test_source="def test_x(): pass",
            prompt_tokens=1, completion_tokens=1, unit=UNIT_TEST_BATCH,
        )
        rows = read_jsonl(results_dir / "generated_tests.jsonl")
        assert rows[0]["unit"] == UNIT_SINGLE_TEST_FUNCTION
        assert rows[1]["unit"] == UNIT_TEST_BATCH

    try:
        log_generated_test(
            results_dir=Path(tempfile.mkdtemp(prefix="scorer-check-c-bad-")), arm="C", target="t",
            mutant_id="M-x", attempt=1, passed_on_clean=True, killed_target=True,
            test_source="x", prompt_tokens=0, completion_tokens=0, unit="not_a_real_unit",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("meta-test failed: log_generated_test accepted an unrecognized unit")

    print("[CHECK C] log_generated_test: unit field round-trips correctly and rejects an "
          "unrecognized value")


def test_check_c_has_teeth_against_a_mislabeled_row() -> None:
    """Meta-test: a row claiming the wrong unit is exactly the failure mode
    bug 4 was -- a consumer treating test_source as one thing when it's
    actually another, invisibly. Confirm a consumer assertion modeled on
    scripts/classify_tests.py's own would catch it."""
    row = {"arm": "C", "target": "t", "test_source": "def test_a(): pass\n\ndef test_b(): pass", "unit": "not_a_real_unit"}

    def consumer_check(r: dict) -> None:
        unit = r.get("unit")
        if unit is not None:
            assert unit in (UNIT_SINGLE_TEST_FUNCTION, UNIT_TEST_BATCH), (
                f"unrecognized unit {unit!r} for arm {r['arm']}/{r['target']}"
            )

    try:
        consumer_check(row)
    except AssertionError:
        pass
    else:
        raise AssertionError("meta-test failed: consumer_check did not catch a mislabeled unit")

    print("[CHECK C meta-test] confirmed: a consumer assertion correctly rejects a row "
          "claiming an unrecognized unit -- CHECK C has teeth")


if __name__ == "__main__":
    _sanity_check_fixture()
    test_check_a_has_teeth_against_a_broken_scorer()
    test_evaluate_one_known_outcomes()
    test_gate_check_known_outcomes()
    test_check_b_has_teeth_against_broken_counts()
    test_check_b_passes_on_conserving_data()
    test_check_c_has_teeth_against_a_mislabeled_row()
    test_check_c_unit_field_round_trips()
    print("\nscripts/test_scorer_checks.py: CHECK A, B, C all passed")
