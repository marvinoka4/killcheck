"""Negative controls: two cases where the harness's reported outcome SHOULD
be extreme, built specifically so a flattering bug in a case that never
surprises the normal scoring pipeline gets a chance to fail loudly instead.

Everything else this project checks is oriented "high is good, low prompts
investigation" (kill score, keep rate, SKR) -- a bug that inflates one of
those never trips an alarm on its own, because a high number is what we
expect anyway. These two controls invert that: a HIGH kill score in control
A, or a KEPT draft in control B, is itself the finding, on cases engineered
so neither should be possible if the harness is doing what it claims.

Control A costs nothing (pure scoring, no model calls) and is fast enough
to run every time -- see main() below. Control B makes real model calls
(3-5, x2 for retries) and is NOT wired into verify_targets.py's automatic
run for exactly that reason: this project's reproduction steps promise the
harness-verification steps are free, and control B would break that
promise on every invocation. Run it deliberately: `python3
scripts/negative_controls.py`. See README's Reproducing section.

Writes results/negative_controls.json.
"""
from __future__ import annotations

import ast
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from killcheck.engine import Mutant
from killcheck.runner import Target, score_target, verify_clean
from killcheck.baseline import load_targets, as_target, existing_test_source, _client
from killcheck.agent import draft_and_gate, enclosing_function
from verify_targets import canary_check, byte_size_canary_check, measure_reachable_lines

VACUOUS_SUITE = '''"""Synthetic vacuous suite -- negative control A. Imports the module,
constructs and calls every public decorator once, asserts nothing that
could ever distinguish a mutated implementation from the original."""
import cachetools.func as m


def test_fifo_cache_constructs_and_runs():
    decorated = m.fifo_cache(maxsize=2)(lambda x: x * 2)
    assert decorated is not None
    decorated(3)
    assert True


def test_lfu_cache_constructs_and_runs():
    decorated = m.lfu_cache(maxsize=2)(lambda x: x * 2)
    assert decorated is not None
    decorated(3)
    assert True


def test_lru_cache_constructs_and_runs():
    decorated = m.lru_cache(maxsize=2)(lambda x: x * 2)
    assert decorated is not None
    decorated(3)
    assert True


def test_rr_cache_constructs_and_runs():
    decorated = m.rr_cache(maxsize=2)(lambda x: x * 2)
    assert decorated is not None
    decorated(3)
    assert True


def test_ttl_cache_constructs_and_runs():
    decorated = m.ttl_cache(maxsize=2, ttl=10)(lambda x: x * 2)
    assert decorated is not None
    decorated(3)
    assert True
'''

NEAR_ZERO_THRESHOLD = 0.10  # stated explicitly; not a hard pass/fail gate, a framing for the report


def control_a() -> dict:
    """A suite that cannot kill anything. Expected: kill score at or very
    near zero -- and NOT for the boring reason (module never loaded) that
    made bug 1 read as a finding instead of a fault. Checked explicitly,
    separately, and in this order, before the score is trusted at all."""
    spec = next(s for s in load_targets() if s["name"] == "cachetools-func")
    real_target = as_target(spec)

    work_root = Path(tempfile.mkdtemp(prefix="killcheck-negctrl-a-"))
    project = work_root / "project"
    shutil.copytree(
        real_target.project_root, project,
        ignore=shutil.ignore_patterns("__pycache__", ".git", ".pytest_cache", "*.pyc", ".venv"),
    )
    tests_dir = project / "tests"
    shutil.rmtree(tests_dir)
    tests_dir.mkdir()
    (tests_dir / "test_vacuous.py").write_text(VACUOUS_SUITE)

    vacuous_target = Target(
        name="cachetools-func-vacuous-control",
        project_root=project,
        module_path=real_target.module_path,
        test_command=real_target.test_command,
    )

    result: dict = {"target": "cachetools-func", "checks": {}}

    # (a) the synthetic suite must pass on clean source
    try:
        verify_clean(vacuous_target)
        result["checks"]["clean_pass"] = True
    except RuntimeError as e:
        result["checks"]["clean_pass"] = False
        result["checks"]["clean_pass_error"] = str(e)[-1500:]
        result["ABORT"] = "synthetic suite does not pass on clean source -- cannot proceed"
        shutil.rmtree(work_root, ignore_errors=True)
        return result

    # (b) the module actually imported and its functions actually executed
    reachable = measure_reachable_lines(vacuous_target, timeout=30)
    result["checks"]["executed_lines"] = len(reachable) if reachable is not None else None
    result["checks"]["module_actually_executed"] = bool(reachable)

    # (c) both canaries still pass on this target, using the vacuous suite
    # as the test command -- confirms the vacuous suite doesn't somehow
    # swallow an import-time failure.
    canary_passed, canary_detail = canary_check(vacuous_target)
    result["checks"]["unparseable_canary"] = {"passed": canary_passed, "detail": canary_detail}
    byte_status, byte_detail = byte_size_canary_check(vacuous_target)
    result["checks"]["byte_size_canary"] = {"status": byte_status, "detail": byte_detail}

    preconditions_ok = (
        result["checks"]["clean_pass"]
        and result["checks"]["module_actually_executed"]
        and canary_passed
        and byte_status != "FAIL"
    )
    result["preconditions_ok"] = preconditions_ok
    if not preconditions_ok:
        result["ABORT"] = "a precondition failed -- any kill score below is not trustworthy evidence of anything"

    # The actual control: score through the frozen runner, unmodified.
    report = score_target(vacuous_target, timeout=30, workers=1)
    result["total_mutants"] = report["total_mutants"]
    result["killed"] = report["killed"]
    result["kill_score"] = report["kill_score"]
    outcome_counts: dict[str, int] = {}
    by_operator: dict[str, int] = {}
    killed_by_operator: dict[str, int] = {}
    for r in report["results"]:
        outcome_counts[r["outcome"]] = outcome_counts.get(r["outcome"], 0) + 1
        by_operator[r["operator"]] = by_operator.get(r["operator"], 0) + 1
        if r["outcome"] != "survived":
            killed_by_operator[r["operator"]] = killed_by_operator.get(r["operator"], 0) + 1
    result["outcome_counts"] = outcome_counts
    result["mutants_by_operator"] = by_operator
    result["killed_by_operator"] = killed_by_operator
    result["kills_outside_return_none"] = sum(
        v for k, v in killed_by_operator.items() if k != "return_none"
    )
    result["near_zero"] = report["kill_score"] <= NEAR_ZERO_THRESHOLD
    result["near_zero_threshold"] = NEAR_ZERO_THRESHOLD
    result["explanation"] = (
        "This suite asserts `is not None` on every decorated function, which is a real, "
        "narrow existence check against exactly one operator class (return_none) on "
        "whichever lines this specific call pattern (maxsize=2, non-None, non-callable) "
        "actually executes -- not a harness bug. kills_outside_return_none == 0 is the "
        "load-bearing number here, not kill_score alone: it confirms no constant or "
        "compare mutant is being counted as detected by an assertion that cannot "
        "possibly distinguish them."
    )

    shutil.rmtree(work_root, ignore_errors=True)
    return result


# Real lines from the current cachetools-func checkout, verified to match at
# runtime below; fabricated mutated_line/operator/description for each --
# never applied to the scored source, which is passed through byte-for-byte.
_FABRICATED_DIFFS = [
    (35, "def fifo_cache(maxsize=128, typed=False):",
         "def fifo_cache(maxsize=256, typed=False):", "constant", "128 -> 256 (fabricated, not applied)"),
    (41, "    if maxsize is None:",
         "    if maxsize is not None:", "compare", "is None -> is not None (fabricated, not applied)"),
    (63, "def lru_cache(maxsize=128, typed=False):",
         "def lru_cache(maxsize=127, typed=False):", "constant", "128 -> 127 (fabricated, not applied)"),
    (91, "def ttl_cache(maxsize=128, ttl=600, timer=time.monotonic, typed=False):",
         "def ttl_cache(maxsize=128, ttl=601, timer=time.monotonic, typed=False):", "constant",
         "600 -> 601 (fabricated, not applied)"),
    (77, "def rr_cache(maxsize=128, choice=random.choice, typed=False):",
         "def rr_cache(maxsize=129, choice=random.choice, typed=False):", "constant",
         "128 -> 129 (fabricated, not applied)"),
]


def control_b(n: int = 5) -> dict:
    """A mutant that does not exist. Runs the real arm C draft_and_gate loop
    against n fabricated original/mutated line pairs, on a module whose
    source is left genuinely untouched (Mutant.source is the real, current,
    clean file content, byte for byte -- not a paraphrase of it). A test
    cannot fail against a mutant that was never applied: killed_target must
    be False for every attempt, including retries. Any KEPT draft means the
    gate passed something on a property other than the one we believe it
    checks."""
    spec = next(s for s in load_targets() if s["name"] == "cachetools-func")
    target = as_target(spec)
    module_source = (target.project_root / target.module_path).read_text()
    module_tree = ast.parse(module_source)
    test_file, test_source = existing_test_source(spec, target)
    real_lines = module_source.splitlines()

    fabricated = _FABRICATED_DIFFS[:n]
    for lineno, orig, _mut, _op, _desc in fabricated:
        actual = real_lines[lineno - 1]
        assert actual == orig, (
            f"fabricated original_line does not match the real source at L{lineno}: "
            f"{actual!r} != {orig!r} -- the checkout has drifted, fix the fabricated diff, "
            f"don't silently proceed with a diff that isn't even plausible"
        )

    client = _client()
    run_id = f"negctrl-{time.strftime('%Y%m%d-%H%M%S')}"
    traj_dir = ROOT / "trajectories" / "negative_controls"
    results_dir = ROOT / "results" / "negative_control_b"

    records = []
    any_kept = False
    for lineno, orig, mut, op, desc in fabricated:
        mfunc = enclosing_function(module_tree, lineno)
        fake_mutant = Mutant(
            id=f"M-fabricated-L{lineno}",
            module=str(target.module_path),
            lineno=lineno,
            col_offset=0,
            operator=op,
            description=desc,
            original_line=orig,
            mutated_line=mut,
            source=module_source,  # UNCHANGED -- this is the entire point of the control
        )
        result = draft_and_gate(
            client, target, test_file, str(target.module_path), module_source, test_source,
            fake_mutant, [], run_id, traj_dir, results_dir, mfunc,
        )
        kept = result["kept"]
        any_kept = any_kept or kept
        attempts_summary = [
            {"attempt": a["attempt"], "passed_on_clean": a["passed_on_clean"],
             "killed_target": a["killed_target"], "test_name": a["test_name"]}
            for a in result["attempts"]
        ]
        records.append({
            "mutant_id": fake_mutant.id,
            "lineno": lineno,
            "fabricated_original_line": orig,
            "fabricated_mutated_line": mut,
            "kept": kept,
            "attempts": attempts_summary,
        })
        print(f"  [control B] {fake_mutant.id}: kept={kept}  "
              f"killed_target per attempt={[a['killed_target'] for a in attempts_summary]}")

    return {
        "target": "cachetools-func",
        "n_fabricated": len(fabricated),
        "any_kept": any_kept,
        "PASS": not any_kept,
        "records": records,
        "run_id": run_id,
    }


def main() -> int:
    print("=== negative control A: a suite that cannot kill anything ===")
    started = time.monotonic()
    a = control_a()
    a_wall = round(time.monotonic() - started, 1)
    a["wall_clock_s"] = a_wall
    print(json.dumps({k: v for k, v in a.items() if k != "checks"}, indent=2, default=str))
    print(f"wall clock: {a_wall}s")
    if a.get("ABORT"):
        print(f"\nABORT: {a['ABORT']}")
        out = {"control_a": a, "control_b": None}
        (ROOT / "results" / "negative_controls.json").write_text(json.dumps(out, indent=2))
        return 1

    print()
    print("=== negative control B: a mutant that does not exist ===")
    started = time.monotonic()
    b = control_b()
    b_wall = round(time.monotonic() - started, 1)
    b["wall_clock_s"] = b_wall
    print(f"any_kept={b['any_kept']}  PASS={b['PASS']}  wall clock: {b_wall}s")
    if not b["PASS"]:
        print("\nFAIL: at least one fabricated-mutant draft was KEPT. Report immediately, do not")
        print("reconcile or explain away -- the gate is passing something other than what we believe.")

    out = {"control_a": a, "control_b": b}
    results_path = ROOT / "results" / "negative_controls.json"
    results_path.write_text(json.dumps(out, indent=2))
    print(f"\nwrote {results_path}")
    return 0 if b["PASS"] else 1


if __name__ == "__main__":
    sys.exit(main())
