"""One-off verification: run the frozen runner against every target.json entry,
and build the authoritative denominator manifest (results/target_verification.json)
that scripts/ablate.py, killcheck/report.py, and the arms' scoring all read from.

Three checks, in order:

1. Canary: overwrite the module under mutation with source that cannot even
   be imported, run the target's test command through the exact same
   tempdir-copy-and-overwrite path the frozen runner uses (_evaluate_one),
   and assert the suite does NOT report "survived". A canary that survives
   means mutations to that module never reach the interpreter -- the
   src-layout editable-install bug class, generalised to any future cause.
   This must pass for every target before any kill score from it is trusted.
   Note what this does NOT prove: that execution is isolated between runs.
   That is a separate property, checked next.

2. Determinism: run each target's full mutation scoring three times serially
   and assert the survivor SET (not just the count) is byte-identical across
   all three. This exists because the runner's old default (workers=4,
   concurrent mutant evaluation) produced a different survivor set on every
   run for one real target (async I/O against real temp files) -- caught by
   hand during eval-set verification, before it could reach a committed
   number. The canary and the determinism check prove two different things:
   the canary proves a mutation reaches the interpreter; determinism proves
   the execution that observes it is isolated. A harness needs both. A
   target that fails this check is quarantined -- excluded from reachability
   scoring and the pooled count, reported separately, never averaged in.

3. Baseline scoring + reachability: run the real mutation set (reusing one of
   the three determinism runs -- they're identical by construction once a
   target passes check 2, so a fourth run would be wasted work), confirm
   mutant count lands in the 15-60 band, report the killed/timeout/error
   breakdown per METHODOLOGY.md's Kill outcome breakdown section, and run the
   clean suite under `coverage` to determine which surviving mutants sit on a
   line the suite never executes at all ("unreachable" -- structurally
   impossible for this suite to kill, independent of assertion quality)
   versus lines it does execute ("reachable-survivor" -- a real assertion
   gap). Per METHODOLOGY.md's Metrics section, the primary metric's denominator is
   reachable survivors only; raw SKR (unreachable included) is reported
   alongside it.

There is no held-out-operator partition here. One was built and then
abandoned -- see METHODOLOGY.md's "Abandoned: holdout transfer control" section
for the numbers that killed it. All reachable survivors are eligible.

Writes results/target_verification.json with a full per-mutant record per
target (mutant_id, operator, outcome, reachable) plus the aggregate counts,
so downstream scripts can slice however they need without recomputing
anything. Exits non-zero if any target fails its canary.

The checks themselves (canary_check, byte_size_canary_check,
determinism_check, measure_reachable_lines, build_mutant_records,
summarize_reachability, outcome_breakdown) now live in
killcheck/verify_core.py, not here -- moved so they are part of the
installable `killcheck` package and reachable from `killcheck.cli`'s
score/verify/harden commands without those depending on scripts/, which is
eval-set batch tooling. Pure code motion, re-verified by diffing a full
re-run's output against the previously-committed target_verification.json;
see CHANGELOG.md. This file's own logic (main(), below) is unchanged.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from killcheck.invariants import assert_outcome_conservation, assert_pooled_conservation, assert_reachability_conservation
from killcheck.runner import Target, score_target, verify_clean

# byte_size_preserving_mutation isn't called directly by anything in this
# file -- it's re-exported here (unused-import warnings aside) because
# scripts/test_pyc_exclusion.py does `from verify_targets import
# byte_size_canary_check, byte_size_preserving_mutation`, i.e. it's imported
# BY NAME off this module, not off killcheck.verify_core directly. Removing
# it as an "unused import" cleanup breaks that test file's collection.
from killcheck.verify_core import (
    build_mutant_records,
    byte_size_canary_check,
    byte_size_preserving_mutation,
    canary_check,
    determinism_check,
    measure_reachable_lines,
    outcome_breakdown,
    summarize_reachability,
)


def main() -> int:
    targets = json.loads((ROOT / "targets.json").read_text())

    print("=== canary check (must pass before any kill score below is trusted) ===")
    canary_failures = []
    for t in targets:
        target = Target.from_dict(t, ROOT)
        try:
            verify_clean(target)
            passed, detail = canary_check(target)
            status = "PASS" if passed else "FAIL"
            print(f"{t['name']:25s} {status:4s}  {detail}")
            if not passed:
                canary_failures.append(t["name"])
        except Exception as e:
            print(f"{t['name']:25s} FAIL  clean suite did not pass: {e}")
            canary_failures.append(t["name"])

    if canary_failures:
        print()
        print(f"CANARY FAILED for: {', '.join(canary_failures)}")
        print("Do not trust kill scores for these targets until fixed. Aborting before scoring.")
        return 1

    print()
    print("=== byte-size-preserving canary (catches stale-bytecode substitution the garbage-source canary cannot) ===")
    byte_canary_failures = []
    for t in targets:
        target = Target.from_dict(t, ROOT)
        status, detail = byte_size_canary_check(target)
        print(f"{t['name']:25s} {status:4s}  {detail}")
        if status == "FAIL":
            byte_canary_failures.append(t["name"])

    if byte_canary_failures:
        print()
        print(f"BYTE-SIZE CANARY FAILED for: {', '.join(byte_canary_failures)}")
        print("A same-byte-length mutation went undetected -- possible stale .pyc execution. Aborting before scoring.")
        return 1

    print()
    print("=== determinism check (survivor SET must be identical across 3 serial runs) ===")
    determinism_reports: dict[str, list[dict]] = {}
    quarantined = []
    for t in targets:
        target = Target.from_dict(t, ROOT)
        deterministic, detail, reports = determinism_check(target)
        status = "PASS" if deterministic else "FAIL"
        print(f"{t['name']:25s} {status:4s}  {detail}")
        if deterministic:
            determinism_reports[t["name"]] = reports
        else:
            quarantined.append(t["name"])

    if quarantined:
        print()
        print(f"QUARANTINED (non-deterministic even at workers=1): {', '.join(quarantined)}")
        print("Excluded from reachability scoring and the pooled count below, not averaged in.")

    print()
    print("=== baseline scoring + reachability ===")
    verification_results = []
    error_dominant_targets = []
    unknown_reachability_targets = []
    pooled_reachable_survivors = 0
    for t in targets:
        if t["name"] in quarantined:
            verification_results.append({"name": t["name"], "quarantined": True})
            continue
        target = Target.from_dict(t, ROOT)
        try:
            rep = determinism_reports[t["name"]][0]  # already ran 3x identically; reuse, don't re-run
            breakdown = outcome_breakdown(rep)
            reachable_lines = measure_reachable_lines(target)
            mutants = build_mutant_records(target, rep, reachable_lines)
            reach = summarize_reachability(mutants)
            # CHECK B (conservation invariants) -- a violation here means the
            # scorer lost or duplicated a mutant somewhere; abort the whole
            # run rather than publish a pooled number built on it (caught
            # separately from the except Exception below -- see there).
            assert_outcome_conservation(breakdown["counts"], rep["total_mutants"], t["name"])
            assert_reachability_conservation(reach, rep["total_mutants"], t["name"])
            pooled_reachable_survivors += reach["reachable_survivor"]

            c, f = breakdown["counts"], breakdown["fractions"]
            flag = "  [15-60 OK]" if 15 <= rep["total_mutants"] <= 60 else "  [OUT OF RANGE]"
            print(
                f"{t['name']:25s} mutants={rep['total_mutants']:4d} "
                f"kill_score={rep['kill_score']:.4f}{flag}"
            )
            print(
                f"{'':25s} killed={c['killed']:3d} ({f['killed']:.1%})  "
                f"timeout={c['timeout']:3d} ({f['timeout']:.1%})  "
                f"error={c['error']:3d} ({f['error']:.1%})  "
                f"survived={c['survived']:3d} ({f['survived']:.1%})"
            )
            print(
                f"{'':25s} reachability: unreachable={reach['unreachable']:3d}  "
                f"reachable-survivor={reach['reachable_survivor']:3d}  "
                f"killed={reach['killed']:3d}"
                + (f"  unknown={reach['unknown_reachability_survivor']}" if reach["unknown_reachability_survivor"] else "")
            )
            if breakdown["error_dominant"]:
                print(
                    f"{'':25s} WARNING: {breakdown['error_share_of_kills']:.1%} of this "
                    f"target's kills are import-time errors, not assertions firing"
                )
                error_dominant_targets.append(t["name"])
            if reach["unknown_reachability_survivor"]:
                unknown_reachability_targets.append(t["name"])

            verification_results.append(
                {
                    "name": t["name"],
                    "total_mutants": rep["total_mutants"],
                    "kill_score": rep["kill_score"],
                    "outcome_counts": breakdown["counts"],
                    "outcome_fractions": breakdown["fractions"],
                    "error_share_of_kills": breakdown["error_share_of_kills"],
                    "error_dominant": breakdown["error_dominant"],
                    "reachability_counts": reach,
                    "mutants": mutants,
                }
            )
        except AssertionError:
            # CHECK B violation: a conservation invariant broke. This is
            # not a per-target flake to record and move past -- it means
            # the scorer's own arithmetic is internally inconsistent, so
            # abort the whole run rather than write a pooled number built
            # on a number that doesn't add up.
            raise
        except Exception as e:
            print(f"{t['name']:25s} FAILED: {e}")
            verification_results.append({"name": t["name"], "failed": str(e)})

    # CHECK B, pooled form: the published pooled figure must equal the sum
    # of the per-target figures it was built from -- METHODOLOGY.md's primary
    # metric IS this sum, so this is the same invariant as the per-target
    # one above, checked once more at the point where the number a reader
    # actually sees gets assembled.
    assert_pooled_conservation(
        pooled_reachable_survivors,
        [r["reachability_counts"]["reachable_survivor"] for r in verification_results if "reachability_counts" in r],
        "pooled reachable survivors",
    )

    results_path = ROOT / "results" / "target_verification.json"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(verification_results, indent=2))
    print()
    print(f"Wrote {results_path.relative_to(ROOT)}")
    scored = len(targets) - len(quarantined)
    print(f"Pooled reachable survivors across {scored} scored targets: {pooled_reachable_survivors}"
          + (f"  ({len(quarantined)} quarantined, excluded)" if quarantined else ""))

    if error_dominant_targets:
        print()
        print(
            f"NOTE: kill score for {', '.join(error_dominant_targets)} is driven "
            f"substantially by import-time errors, not test assertions. See "
            f"METHODOLOGY.md's Kill outcome breakdown section before citing these numbers."
        )
    if unknown_reachability_targets:
        print()
        print(
            f"NOTE: coverage measurement failed for {', '.join(unknown_reachability_targets)} "
            f"-- their survivors have reachability=unknown and are excluded from both the "
            f"reachable-survivor and work-queue counts until this is fixed."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
