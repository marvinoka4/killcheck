"""Regression test for a real, confirmed CPython vulnerability: a reader's
"tenth instrument bug" report, verified rather than reasoned about (see
CHANGELOG.md for the full investigation).

CPython's default timestamp-based .pyc invalidation keys on source mtime
(whole seconds) + source size. A synthetic reproducer confirmed this really
does fool the interpreter: overwrite a module with byte-size-identical,
behaviorally-different source, force the mtime back to match a stale .pyc's
header, and the stale bytecode executes -- not the source on disk. Every
target checkout in this eval set has real committed .pyc files (from its own
earlier clean-suite runs) that would be exactly this kind of stale artifact
the moment a mutation touched that module, if nothing excluded them.

Four places in this codebase build a scored tempdir copy: runner.py's
`_evaluate_one` (frozen -- the denominator/canary/determinism instrument),
agent.py's `gate_check` and `official_batch_rescore` (two call sites there,
including a nested per-mutant copy taken from an already-executed outer
copy), and baseline.py's `score_with_added_tests` (which delegates its
actual per-mutant scoring back to runner.py's `_evaluate_one`, so it inherits
that protection rather than needing its own). Every one excludes
"__pycache__" and "*.pyc" via `shutil.ignore_patterns`. This test exercises
the frozen, real `_evaluate_one` directly -- not a reimplementation of its
copytree call -- by spying on `shutil.copytree` from inside runner.py's own
module namespace, so a later edit that quietly drops the exclusion (as
"cleanup") fails this test immediately rather than being caught only if
someone happens to notice a wrong kill score.

Run directly (`python3 scripts/test_pyc_exclusion.py`) or via pytest.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from killcheck.engine import Mutant
from killcheck.runner import Target, _evaluate_one
from verify_targets import byte_size_canary_check  # noqa: E402 (path set above)


def _spy_copytree_pyc_count(target: Target, mutant: Mutant) -> tuple[int, int]:
    """Run the real, unmodified _evaluate_one() with shutil.copytree spied on
    from inside killcheck.runner's own namespace, so this observes exactly
    the call runner.py itself makes -- not a hand-rolled equivalent. Returns
    (pyc_count, pycache_dir_count) found in the copy immediately after
    copytree returns, before _evaluate_one's tempdir context manager deletes
    it."""
    # shutil.copytree recurses into itself by name for nested directories
    # (confirmed on 3.14), so patching the module-level name means this spy
    # is re-entered for every subdirectory, not just runner.py's own
    # top-level call. Only inspect once the outermost call has fully
    # returned -- depth tracks nesting, not "is this the call runner.py made".
    real_copytree = shutil.copytree
    captured: dict[str, int] = {}
    depth = [0]

    def spy(*args, **kwargs):
        depth[0] += 1
        try:
            result = real_copytree(*args, **kwargs)
        finally:
            depth[0] -= 1
        if depth[0] == 0:
            dst = args[1] if len(args) > 1 else kwargs["dst"]
            captured["pyc"] = len(list(Path(dst).rglob("*.pyc")))
            captured["pycache"] = len(list(Path(dst).rglob("__pycache__")))
        return result

    with mock.patch("killcheck.runner.shutil.copytree", side_effect=spy):
        _evaluate_one(target, mutant, timeout=30)

    return captured["pyc"], captured["pycache"]


def test_pyc_exclusion_on_targets_with_real_committed_pyc() -> None:
    """Precondition and assertion in one: every target checked here must
    actually have real .pyc files in its checkout (otherwise this test would
    pass vacuously, having exercised nothing) -- and the tempdir copy
    runner.py's real _evaluate_one() produces must contain none."""
    targets = json.loads((ROOT / "targets.json").read_text())
    garbage = Mutant(
        id="M-regression-test", module="x", lineno=0, col_offset=0,
        operator="canary", description="", original_line="", mutated_line="",
        source="\n\nNOT VALID PYTHON ((( unbalanced\n",
    )
    checked = 0
    for t in targets:
        target = Target.from_dict(t, ROOT)
        source_pyc = list(target.project_root.rglob("*.pyc"))
        if not source_pyc:
            continue  # this precondition doesn't hold for every checkout; skip, don't fail
        pyc_count, pycache_count = _spy_copytree_pyc_count(target, garbage)
        assert pyc_count == 0, (
            f"{t['name']}: {pyc_count} .pyc files reached the scored tempdir "
            f"(source checkout has {len(source_pyc)}) -- the exclusion regressed"
        )
        assert pycache_count == 0, f"{t['name']}: __pycache__ dir reached the scored tempdir"
        checked += 1
    assert checked >= 8, f"only {checked} targets had real .pyc to test against -- precondition too weak"
    print(f"pyc exclusion: PASS on {checked} targets with real committed .pyc files, 0 reached any tempdir")


def test_byte_size_canary_detects_same_length_mutation() -> None:
    """The behavioral counterpart: not just "no .pyc present" but "a
    same-byte-length behavioral mutation is actually detected", end to end,
    through the real test command. Reuses verify_targets.py's own check
    rather than duplicating its logic."""
    targets = json.loads((ROOT / "targets.json").read_text())
    passed = failed = na = 0
    for t in targets:
        target = Target.from_dict(t, ROOT)
        status, detail = byte_size_canary_check(target)
        if status == "FAIL":
            failed += 1
            print(f"  FAIL {t['name']}: {detail}")
        elif status == "PASS":
            passed += 1
        else:
            na += 1
    assert failed == 0, f"{failed} target(s) failed the byte-size canary -- see output above"
    assert passed >= 8, f"only {passed} targets had a same-length comparison operator to test with"
    print(f"byte-size canary: PASS on {passed} targets, N/A on {na}, 0 failures")


if __name__ == "__main__":
    test_pyc_exclusion_on_targets_with_real_committed_pyc()
    test_byte_size_canary_detects_same_length_mutation()
    print("scripts/test_pyc_exclusion.py: all checks passed")
