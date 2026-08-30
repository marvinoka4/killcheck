"""One-off verification: run the frozen runner against every target.json entry.

Not part of the pipeline -- a smoke test for the fetch step. Two checks:

1. Canary: overwrite the module under mutation with source that cannot even
   be imported, run the target's test command through the exact same
   tempdir-copy-and-overwrite path the frozen runner uses (_evaluate_one),
   and assert the suite does NOT report "survived". A canary that survives
   means mutations to that module never reach the interpreter -- the
   src-layout editable-install bug class, generalised to any future cause.
   This must pass for every target before any kill score from it is trusted.

2. Baseline scoring: run the real mutation set and confirm mutant count
   lands in the 15-60 band and the suite passes clean.

Exits non-zero if any target fails its canary.
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
for t in targets:
    target = Target.from_dict(t, ROOT)
    try:
        rep = score_target(target, timeout=30, workers=4)
        flag = "  [15-60 OK]" if 15 <= rep["total_mutants"] <= 60 else "  [OUT OF RANGE]"
        print(
            f"{t['name']:25s} mutants={rep['total_mutants']:4d} "
            f"killed={rep['killed']:4d} kill_score={rep['kill_score']:.4f}{flag}"
        )
    except Exception as e:
        print(f"{t['name']:25s} FAILED: {e}")
