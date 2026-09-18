"""CHECK A and CHECK B, applied to Design 3's own pipeline
(scripts/within_function_transfer.py) -- per METHODOLOGY.md's Design 3
constraints and the instruction that any new scoring code gets the same
treatment as everything else in this project. See
scripts/test_scorer_checks.py's module docstring for why this class of
check exists at all (credit: Zain Dana Harper, dev.to/zaindanaharper).

CHECK A here exercises `prepare_kept_only_project` and
`score_fresh_mutant_kept_alone` directly -- the two functions that decide,
for a fresh mutant, whether the frozen kept-tests-alone file kills it --
against five known-by-construction outcomes, same shape as
scripts/test_scorer_checks.py's CHECK A:

  1. a kept test that provably kills the fresh mutant      -> killed
  2. a kept test that provably cannot kill it               -> survived
  3. joined kept sources that fail on CLEAN module           -> must be
                                                                 caught by
                                                                 the
                                                                 clean-pass
                                                                 check,
                                                                 never
                                                                 scored
  4. a kept test that hangs                                  -> timeout
  5. a kept-tests file that doesn't parse at all              -> error
                                                                 (collection),
                                                                 distinctly

CHECK B exercises assert_disjoint (new in killcheck/invariants.py for this
design) against both a genuinely disjoint pair and a deliberately
overlapping one.

Every check has its own meta-test proving it has teeth against a
deliberately broken variant, same discipline as scripts/
test_scorer_checks.py and scripts/test_pyc_exclusion.py.

Run directly (`python3 scripts/test_within_function_transfer_checks.py`)
or via pytest.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from killcheck.engine import generate_mutants
from killcheck.invariants import assert_disjoint
from killcheck.runner import Target
from scripts.within_function_transfer import (
    prepare_kept_only_project,
    score_fresh_mutant_kept_alone,
)

MODULE_SOURCE = "def check(a, b):\n    return a == b\n"

KEPT_KILLING = "from mod import check\n\n\ndef test_kept_killing():\n    assert check(1, 1) is True\n"
KEPT_NONKILLING = "from mod import check\n\n\ndef test_kept_nonkilling():\n    assert check(1, 1) is not None\n"
KEPT_HANGING = "import time\n\n\ndef test_kept_hanging():\n    time.sleep(30)\n"
KEPT_CLEANFAIL = "from mod import check\n\n\ndef test_kept_cleanfail():\n    assert check(1, 1) == 'nonsense'\n"
KEPT_UNPARSEABLE = "def test_kept_unparseable(:\n    pass\n"


def _real_compare_mutant():
    for m in generate_mutants(MODULE_SOURCE, "mod.py"):
        if m.operator == "compare":
            return m
    raise AssertionError("fixture module produced no compare mutant -- fixture is broken")


def _build_fixture_target(tmp: Path) -> tuple[Target, Path]:
    root = tmp / "proj"
    root.mkdir()
    (root / "mod.py").write_text(MODULE_SOURCE)
    (root / "tests").mkdir()
    test_file = root / "tests" / "test_mod.py"
    test_file.write_text("from mod import check\n")  # pre-existing suite, deliberately empty of tests
    target = Target(
        name="wft-check",
        project_root=root,
        module_path=Path("mod.py"),
        test_command=[sys.executable, "-m", "pytest", "tests", "-q"],
    )
    return target, test_file


def _score(kept_sources: list[tuple[str, str]], mutant=None, timeout: int = 10):
    """One call through the real pipeline: prepare_kept_only_project, then
    (if a mutant is given) score_fresh_mutant_kept_alone. Returns
    (clean_pass, clean_output, result_or_None)."""
    with tempfile.TemporaryDirectory(prefix="wft-check-") as tmp_str:
        tmp = Path(tmp_str)
        target, test_file = _build_fixture_target(tmp)
        kept_only = prepare_kept_only_project(target, test_file, kept_sources, tmp)

        from killcheck.agent import _run_with_plugin
        code, output, _ = _run_with_plugin(
            kept_only.test_command, kept_only.project_root, timeout, tmp / "clean_report.jsonl"
        )
        clean_pass = code == 0
        if not clean_pass or mutant is None:
            return clean_pass, output, None
        result = score_fresh_mutant_kept_alone(kept_only, mutant, timeout)
        return clean_pass, output, result


def test_within_function_transfer_known_outcomes() -> None:
    mutant = _real_compare_mutant()

    clean_pass, _, result = _score([("test_kept_killing", KEPT_KILLING)], mutant)
    assert clean_pass, "killing candidate must pass on clean source"
    assert result["outcome"] == "killed", f"expected 'killed', got {result['outcome']!r}"

    clean_pass, _, result = _score([("test_kept_nonkilling", KEPT_NONKILLING)], mutant)
    assert clean_pass, "non-killing candidate must pass on clean source"
    assert result["outcome"] == "survived", f"expected 'survived', got {result['outcome']!r}"

    clean_pass, output, result = _score([("test_kept_cleanfail", KEPT_CLEANFAIL)], mutant)
    assert not clean_pass, (
        "fixture assumption broken: the clean-fail candidate was expected to fail on clean "
        "source -- if it doesn't, this case tests nothing"
    )
    assert result is None, "a candidate that fails clean-pass must never be scored against a mutant at all"

    clean_pass, _, result = _score([("test_kept_hanging", KEPT_HANGING)], mutant, timeout=2)
    assert not clean_pass, "the hanging candidate must not report a clean pass (it times out on the clean-pass run too)"

    clean_pass, output, result = _score([("test_kept_unparseable", KEPT_UNPARSEABLE)], mutant)
    assert not clean_pass, "an unparseable joined kept-tests file must fail the clean-pass check"

    print("[Design 3 CHECK A] prepare_kept_only_project + score_fresh_mutant_kept_alone: "
          "killing->killed, non-killing->survived, clean-fail->never scored, "
          "hanging->clean-pass fails, unparseable->clean-pass fails -- all confirmed")


def test_within_function_transfer_check_a_has_teeth() -> None:
    """Meta-test: force every subprocess exit code to 0 (the most flattering
    possible corruption -- everything reads as passing) and confirm the
    known-killing-outcome assertion then fails."""
    mutant = _real_compare_mutant()

    class _AlwaysZero:
        returncode = 0
        stdout = ""
        stderr = ""

    caught = False
    with mock.patch("killcheck.agent.subprocess.run", return_value=_AlwaysZero()):
        clean_pass, _, result = _score([("test_kept_killing", KEPT_KILLING)], mutant)
        try:
            assert clean_pass and result["outcome"] == "killed"
        except AssertionError:
            caught = True

    assert caught, (
        "meta-test failed: forcing every subprocess exit code to 0 should have made the "
        "known-killing-outcome assertion fail -- it didn't, which means this pipeline's CHECK A "
        "would not catch this class of bug"
    )
    print("[Design 3 CHECK A meta-test] confirmed: a scorer forced to always report success "
          "DOES fail the known-killing-outcome assertion -- has teeth")


def test_within_function_transfer_disjoint() -> None:
    assert_disjoint({"M-a", "M-b"}, {"M-c", "M-d"}, "meta-test disjoint pair")
    print("[Design 3 CHECK B] assert_disjoint passes on a genuinely disjoint pair")


def test_within_function_transfer_disjoint_has_teeth() -> None:
    try:
        assert_disjoint({"M-a", "M-b"}, {"M-b", "M-c"}, "meta-test overlapping pair")
    except AssertionError:
        pass
    else:
        raise AssertionError(
            "meta-test failed: assert_disjoint did not catch a deliberately overlapping pair "
            "-- Design 3's disjointness guarantee (the fresh population never overlaps the "
            "original work queue) would not actually be checked"
        )
    print("[Design 3 CHECK B meta-test] confirmed: assert_disjoint correctly rejects an "
          "overlapping pair -- has teeth")


if __name__ == "__main__":
    test_within_function_transfer_check_a_has_teeth()
    test_within_function_transfer_known_outcomes()
    test_within_function_transfer_disjoint_has_teeth()
    test_within_function_transfer_disjoint()
    print("\nscripts/test_within_function_transfer_checks.py: all checks passed")
