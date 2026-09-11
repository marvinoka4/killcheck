"""Regression test for a real, confirmed CPython vulnerability: a reader's
"tenth instrument bug" report, verified rather than reasoned about, and a
follow-up from the same reader sharper than the first (see CHANGELOG.md for
both investigations).

CPython's default timestamp-based .pyc invalidation keys on source mtime
(whole seconds) + source size. A synthetic reproducer confirmed this really
does fool the interpreter: overwrite a module with byte-size-identical,
behaviorally-different source, force the mtime back to match a stale .pyc's
header, and the stale bytecode executes -- not the source on disk. Every
target checkout in this eval set has real committed .pyc files (from its own
earlier clean-suite runs) that would be exactly this kind of stale artifact
the moment a mutation touched that module, if nothing excluded them.

This file originally asserted a PROXY: that zero .pyc files land inside the
tempdir copy. That assertion is not the property that actually matters, and
the gap is real: with `PYTHONPYCACHEPREFIX` set, CPython writes compiled
bytecode to a *separate* tree keyed on the copy's absolute path, not beside
the source at all -- so the proxy (0 .pyc in the copy) holds even when a
*reused* absolute work path reads stale bytecode from that external tree.
Confirmed by direct reproduction across three branches: no prefix (fresh,
regardless of path reuse, because .pyc lives inside the copy and gets
deleted with it); prefix + a *fixed*, reused work path (STALE -- 0 .pyc in
the copy, wrong behavior observed); prefix + a fresh unique work path per
call (fresh). The actual protection this codebase relies on is **two**
things together, not one: `ignore_patterns` (stops a stale .pyc from being
copied IN from the source checkout) AND every scored copy getting its own
unique `tempfile.TemporaryDirectory()` path, never reused (stops a stale
.pyc from accumulating in an *external*, prefix-keyed cache across calls).
Checked directly, not assumed: every copytree call site in this codebase
(runner.py's `_evaluate_one` and `verify_clean`, agent.py's `gate_check` and
`official_batch_rescore` including its nested per-mutant copy, baseline.py's
`score_with_added_tests`, `verify_targets.py`, `negative_controls.py`) uses
`tempfile.TemporaryDirectory()`/`mkdtemp()` -- none reuses a fixed path. Ran
the full harness once with `PYTHONPYCACHEPREFIX` set and diffed
`target_verification.json` against the committed version: byte-identical.

The tests below assert the PROPERTY -- that a byte-size-preserving mutation
run through this codebase's real, unmodified copy-and-score path is actually
*observed*, under a hostile `PYTHONPYCACHEPREFIX` -- not the proxy. The
original proxy check is kept as a secondary diagnostic only, clearly
labeled: useful for narrowing down *where* an exclusion regressed, never
sufficient on its own to prove nothing went stale.

Run directly (`python3 scripts/test_pyc_exclusion.py`) or via pytest.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from killcheck.engine import Mutant, generate_mutants
from killcheck.runner import Target, _evaluate_one
from verify_targets import byte_size_canary_check, byte_size_preserving_mutation  # noqa: E402


def _first_target_with_byte_size_mutation() -> tuple[Target, Mutant]:
    """A real target + a real, same-byte-length behavioral mutation for it
    (the same construction verify_targets.py's byte-size canary uses) --
    not a synthetic module, so this exercises the real copytree/ignore_
    patterns/test-command path exactly as arm C's scoring does."""
    targets = json.loads((ROOT / "targets.json").read_text())
    for t in targets:
        target = Target.from_dict(t, ROOT)
        source = (target.project_root / target.module_path).read_text()
        result = byte_size_preserving_mutation(source)
        if result is None:
            continue
        candidate, desc = result
        mutant = Mutant(
            id="M-pycprefix-property-test", module=str(target.module_path),
            lineno=0, col_offset=0, operator="byte-canary", description=desc,
            original_line="", mutated_line="", source=candidate,
        )
        return target, mutant
    raise AssertionError("no target in targets.json has an eligible same-length mutation")


def test_pyc_exclusion_on_targets_with_real_committed_pyc() -> None:
    """SECONDARY DIAGNOSTIC ONLY -- see module docstring for why this proxy
    (zero .pyc land in the copy) is not sufficient on its own; it holds even
    in the deliberately-stale PYTHONPYCACHEPREFIX + reused-path case. Kept
    because it's useful for localizing *where* an ignore_patterns exclusion
    regressed, if one ever does -- not as proof nothing went stale."""
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
            continue
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
            _evaluate_one(target, garbage, timeout=30)
        assert captured["pyc"] == 0, f"{t['name']}: {captured['pyc']} .pyc reached the copy"
        assert captured["pycache"] == 0, f"{t['name']}: __pycache__ reached the copy"
        checked += 1
    assert checked >= 8, f"only {checked} targets had real .pyc to test against -- precondition too weak"
    print(f"[diagnostic] pyc-in-copy proxy: 0 on {checked} targets (not sufficient alone -- see property test)")


def test_byte_size_canary_detects_same_length_mutation() -> None:
    """A same-byte-length behavioral mutation is actually detected, end to
    end, through the real test command -- without a hostile
    PYTHONPYCACHEPREFIX. Reuses verify_targets.py's own check."""
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


def test_byte_size_mutation_correctly_observed_under_pycache_prefix() -> None:
    """PRIMARY: the property that matters. Runs a real, same-byte-length
    behavioral mutation through the real, unmodified `_evaluate_one` --
    production code, not a reimplementation -- with PYTHONPYCACHEPREFIX set
    to a fresh external cache directory for the duration. `_evaluate_one`
    gives every call its own unique tempfile.TemporaryDirectory(); asserting
    the outcome here is correct is what actually proves that uniqueness
    holds under this hostile environment variable, not just that .pyc files
    are absent from the copy (see the diagnostic test above, and the module
    docstring for why that's not the same claim)."""
    target, mutant = _first_target_with_byte_size_mutation()
    prefix_dir = Path(tempfile.mkdtemp(prefix="killcheck-pycprefix-property-"))
    old = os.environ.get("PYTHONPYCACHEPREFIX")
    os.environ["PYTHONPYCACHEPREFIX"] = str(prefix_dir)
    try:
        result = _evaluate_one(target, mutant, timeout=30)
    finally:
        if old is None:
            os.environ.pop("PYTHONPYCACHEPREFIX", None)
        else:
            os.environ["PYTHONPYCACHEPREFIX"] = old
        shutil.rmtree(prefix_dir, ignore_errors=True)
    assert result.outcome != "survived", (
        f"a same-byte-length mutation on {mutant.module} was reported '{result.outcome}' "
        f"as 'survived' under PYTHONPYCACHEPREFIX -- stale bytecode may have executed instead "
        f"of the mutation"
    )
    print(f"[property] byte-size mutation under PYTHONPYCACHEPREFIX: outcome={result.outcome} (not survived)")


def _copy_and_run_reusing_one_path(target: Target, content: str, mtime_like: float | None, work: Path) -> str:
    """Deliberately broken: same copytree/ignore_patterns runner.py itself
    uses, but the caller controls (and can reuse) `work`'s absolute path --
    the exact "prefix + fixed work path" branch confirmed stale. Exists only
    to validate the property test above has teeth; not used by any
    production code path."""
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(
        target.project_root, work,
        ignore=shutil.ignore_patterns("__pycache__", ".git", ".pytest_cache", "*.pyc", ".venv", "node_modules"),
    )
    (work / target.module_path).write_text(content)
    if mtime_like is not None:
        os.utime(work / target.module_path, (mtime_like, mtime_like))
    import subprocess
    proc = subprocess.run(target.test_command, cwd=work, capture_output=True, text=True, timeout=30)
    return "survived" if proc.returncode == 0 else "killed"


def test_property_check_catches_the_deliberately_stale_case() -> None:
    """Meta-validation, run once: proves the property assertion above would
    actually fail against the branch confirmed stale in the reader's
    report, before trusting it as a regression test. If this test doesn't
    demonstrate the failure mode, the property test above proves nothing."""
    target, mutant = _first_target_with_byte_size_mutation()
    clean_source = (target.project_root / target.module_path).read_text()

    prefix_dir = Path(tempfile.mkdtemp(prefix="killcheck-pycprefix-meta-"))
    fixed_work = Path(tempfile.mkdtemp(prefix="killcheck-pycprefix-meta-fixed-")) / "reused"
    old = os.environ.get("PYTHONPYCACHEPREFIX")
    os.environ["PYTHONPYCACHEPREFIX"] = str(prefix_dir)
    try:
        _copy_and_run_reusing_one_path(target, clean_source, mtime_like=None, work=fixed_work)
        primed_mtime = (fixed_work / target.module_path).stat().st_mtime
        outcome = _copy_and_run_reusing_one_path(
            target, mutant.source, mtime_like=primed_mtime, work=fixed_work
        )
    finally:
        if old is None:
            os.environ.pop("PYTHONPYCACHEPREFIX", None)
        else:
            os.environ["PYTHONPYCACHEPREFIX"] = old
        shutil.rmtree(prefix_dir, ignore_errors=True)
        shutil.rmtree(fixed_work.parent, ignore_errors=True)

    assert outcome == "survived", (
        f"expected the reused-fixed-path branch to reproduce as stale (outcome='survived') "
        f"the same way the reader's report demonstrated -- got '{outcome}' instead. Either the "
        f"reproduction conditions drifted (different Python version, prefix not honored) or this "
        f"meta-check itself is broken; the property test above cannot be trusted until this "
        f"reproduces as designed."
    )
    print(f"[meta] deliberately-reused-path branch under PYTHONPYCACHEPREFIX: outcome={outcome} "
          f"(confirms the property test above has teeth)")


if __name__ == "__main__":
    test_pyc_exclusion_on_targets_with_real_committed_pyc()
    test_byte_size_canary_detects_same_length_mutation()
    test_property_check_catches_the_deliberately_stale_case()
    test_byte_size_mutation_correctly_observed_under_pycache_prefix()
    print("scripts/test_pyc_exclusion.py: all checks passed")
