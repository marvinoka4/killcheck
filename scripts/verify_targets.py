"""One-off verification: run the frozen runner against every target.json entry.

Not part of the pipeline -- a smoke test for the fetch step. Two checks:

1. Canary: overwrite the module under mutation with source that cannot even
   be imported, run the target's test command through the exact same
   tempdir-copy-and-overwrite path the frozen runner uses (_evaluate_one),
   and assert the suite does NOT report "survived". A canary that survives
   means mutations to that module never reach the interpreter -- the
   src-layout editable-install bug class, generalised to any future cause.
   This must pass for every target before any kill score from it is trusted.

2. Baseline scoring: run the real mutation set, confirm mutant count lands
   in the 15-60 band, and report the killed/timeout/error breakdown per
   CLAUDE.md's Kill outcome breakdown section -- all three count as a kill,
   but `error` is weaker evidence (import-time breakage, not an assertion
   firing), and a target where error outcomes account for more than half
   its kills gets flagged rather than folded silently into the aggregate.

Writes results/target_verification.json with the same breakdown. Exits
non-zero if any target fails its canary.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from killcheck.engine import Mutant
from killcheck.runner import Target, score_target, verify_clean
from killcheck.runner import _evaluate_one  # frozen internal; used read-only here

GARBAGE_SOURCE = "\n\nTHIS IS NOT VALID PYTHON !!! ((( unbalanced and unparseable\n"

ERROR_DOMINANT_THRESHOLD = 0.5  # error outcomes as a share of all kills


def outcome_breakdown(report: dict) -> dict:
    """Disaggregate score_target()'s pooled kill count into killed/timeout/error,
    per CLAUDE.md's Kill outcome breakdown section. score_target() itself only
    reports the pooled total (by design, all three count as a kill) -- this is
    a read-only post-processing pass over the same report, not a change to the
    frozen runner.
    """
    results = report["results"]
    total = len(results)
    counts = {"killed": 0, "timeout": 0, "error": 0, "survived": 0}
    for r in results:
        counts[r["outcome"]] += 1
    fractions = {k: round(v / total, 4) if total else 0.0 for k, v in counts.items()}
    killed_total = counts["killed"] + counts["timeout"] + counts["error"]
    error_share_of_kills = (counts["error"] / killed_total) if killed_total else 0.0
    return {
        "counts": counts,
        "fractions": fractions,
        "killed_total": killed_total,
        "error_share_of_kills": round(error_share_of_kills, 4),
        "error_dominant": error_share_of_kills > ERROR_DOMINANT_THRESHOLD,
    }


def canary_check(target: Target, timeout: int = 30) -> tuple[bool, str]:
    """Return (passed, detail). passed=False means the module edit was invisible
    to the test process -- the suite still reported success on unimportable code."""
    fake = Mutant(
        id="M-canary",
        module=str(target.module_path),
        lineno=0,
        col_offset=0,
        operator="canary",
        description="deliberately invalid source, not a real mutation",
        original_line="",
        mutated_line="",
        source=GARBAGE_SOURCE,
    )
    result = _evaluate_one(target, fake, timeout)
    if result.outcome == "survived":
        return False, "suite PASSED on unimportable source -- mutations are not reaching this target"
    return True, f"suite correctly reported '{result.outcome}' on unimportable source"


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
    sys.exit(1)

print()
print("=== baseline scoring (real mutants, frozen runner) ===")
verification_results = []
error_dominant_targets = []
for t in targets:
    target = Target.from_dict(t, ROOT)
    try:
        rep = score_target(target, timeout=30, workers=4)
        breakdown = outcome_breakdown(rep)
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
        if breakdown["error_dominant"]:
            print(
                f"{'':25s} WARNING: {breakdown['error_share_of_kills']:.1%} of this "
                f"target's kills are import-time errors, not assertions firing"
            )
            error_dominant_targets.append(t["name"])
        verification_results.append(
            {
                "name": t["name"],
                "total_mutants": rep["total_mutants"],
                "kill_score": rep["kill_score"],
                **breakdown,
            }
        )
    except Exception as e:
        print(f"{t['name']:25s} FAILED: {e}")
        verification_results.append({"name": t["name"], "failed": str(e)})

results_path = ROOT / "results" / "target_verification.json"
results_path.parent.mkdir(parents=True, exist_ok=True)
results_path.write_text(json.dumps(verification_results, indent=2))
print()
print(f"Wrote {results_path.relative_to(ROOT)}")

if error_dominant_targets:
    print()
    print(
        f"NOTE: kill score for {', '.join(error_dominant_targets)} is driven "
        f"substantially by import-time errors, not test assertions. See "
        f"CLAUDE.md's Kill outcome breakdown section before citing these numbers."
    )
