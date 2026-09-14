"""Reconstruct what three weaker agent designs would have produced, entirely
from one arm C (agent) run's log -- no extra model calls, no extra runner
calls. This is why results/generated_tests.jsonl logs every attempt, gated
or not, kept or not: attempt 1 and attempt 2 (the retry) are both there with
their own passed_on_clean/killed_target ground truth, so "what if there were
no retry" and "what if there were no gate" are just different filters over
rows that already exist.

Four conditions, from the actual design down to its stripped components:
  C             kept tests: passed_on_clean AND killed_target, any attempt
  C_minus_gate  all tests that passed_on_clean, any attempt (kill or not)
  C_minus_retry kept tests (same gate as C), attempt 1 only
  C_minus_both  attempt 1, unfiltered (no gate at all)

For each condition we report two different things:
  kept_count  how many tests would ship under that design.
  skr         survivor kill rate over all reachable survivors (METHODOLOGY.md's
              primary metric denominator; there is no held-out exclusion).

skr(C) == skr(C_minus_gate) is a true structural identity, not a bug: C's
inclusion rule (passed_on_clean AND killed_target) is a strict subset of
C_minus_gate's (passed_on_clean alone), so every record that counts as a
kill under C also counts under C_minus_gate, and nothing else can, since
anything with both flags true already satisfies C's own criterion. Removing
the gate cannot change which survivors get killed -- it only changes
kept_count (how much extra, non-killing material would ship), which is what
scripts/classify_tests.py's taxonomy is for.

skr(C_minus_retry) vs skr(C_minus_both) is NOT guaranteed to match, and a
gap there is a real finding, not a bug: C_minus_both drops the
passed_on_clean requirement too, so a test that fails on both clean and
mutant source (broken/flaky, not a real kill) can still show
killed_target=True and get counted -- inflating apparent kills with tests
that would fail the gate for a reason other than "didn't kill." A non-zero
gap there means some of C_minus_both's "kills" are artifacts of dropping the
clean-pass check, not evidence the design change was better.

The number that should differ, and the one that actually matters, is
skr(C) vs skr(C_minus_retry): both apply the full gate, differing only in
whether attempt 2 exists. That gap is retry's isolated contribution, the
thing METHODOLOGY.md's agent loop contract claims is the highest-leverage single
change in this system.

Reads results/generated_tests.jsonl (arm C rows) and results/
target_verification.json (for the work-queue reachable survivor denominator
per target). Writes results/ablation.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from killcheck.logs import read_jsonl, UNIT_SINGLE_TEST_FUNCTION

CONDITIONS = ["C", "C_minus_gate", "C_minus_retry", "C_minus_both"]


def _in_condition(record: dict, condition: str) -> bool:
    if condition == "C":
        return record["passed_on_clean"] and record["killed_target"]
    if condition == "C_minus_gate":
        return record["passed_on_clean"]
    if condition == "C_minus_retry":
        return record["attempt"] == 1 and record["passed_on_clean"] and record["killed_target"]
    if condition == "C_minus_both":
        return record["attempt"] == 1
    raise ValueError(condition)


def load_work_queue_denominator(verification: list[dict]) -> dict[str, set[str]]:
    """target name -> set of mutant_ids that are reachable survivors. This is
    what the agent's work queue is drawn from (all of it -- there is no
    held-out exclusion; that control was designed and then abandoned, see
    METHODOLOGY.md's "Abandoned: holdout transfer control" section), so it's the
    correct SKR denominator for these ablations."""
    denom = {}
    for entry in verification:
        if "mutants" not in entry:
            continue
        denom[entry["name"]] = {
            m["mutant_id"]
            for m in entry["mutants"]
            if m["outcome"] == "survived" and m["reachable"]
        }
    return denom


def main() -> int:
    records = [r for r in read_jsonl(ROOT / "results" / "generated_tests.jsonl") if r["arm"] == "C"]
    # CHECK C (unit metadata): every ablation condition below filters and
    # counts these records as if one row is one test function (kept_count
    # is a test-function count). That's true for arm C by construction, but
    # this script has no other guard against silently admitting a
    # differently-shaped row -- assert it instead of assuming it. Rows
    # logged before the `unit` field existed are skipped, not failed (see
    # scripts/classify_tests.py for the same legacy-row handling).
    for r in records:
        unit = r.get("unit")
        if unit is not None:
            assert unit == UNIT_SINGLE_TEST_FUNCTION, (
                f"ablate.py only handles arm C rows (unit={UNIT_SINGLE_TEST_FUNCTION!r}), got "
                f"unit={unit!r} for target {r['target']} mutant {r['mutant_id']}"
            )
    verification = json.loads((ROOT / "results" / "target_verification.json").read_text())
    work_queue = load_work_queue_denominator(verification)
    total_denominator = sum(len(s) for s in work_queue.values())

    if not records:
        print("results/generated_tests.jsonl has no arm C rows yet -- nothing to ablate.")
        print(f"Work-queue reachable-survivor denominator is already known: {total_denominator} "
              f"mutants across {sum(1 for s in work_queue.values() if s)} targets.")
        return 0

    report = {}
    for condition in CONDITIONS:
        included = [r for r in records if _in_condition(r, condition)]
        kept_count = len(included)
        killed_ids_by_target: dict[str, set[str]] = {}
        for r in included:
            if r["killed_target"]:
                killed_ids_by_target.setdefault(r["target"], set()).add(r["mutant_id"])

        addressed = 0
        for target_name, denom_ids in work_queue.items():
            addressed += len(denom_ids & killed_ids_by_target.get(target_name, set()))

        skr = round(addressed / total_denominator, 4) if total_denominator else None
        report[condition] = {
            "kept_count": kept_count,
            "kept_count_unit": "test_function",
            "survivors_addressed": addressed,
            "survivors_addressed_unit": "mutant",
            "work_queue_denominator": total_denominator,
            "work_queue_denominator_unit": "mutant",
            "skr": skr,
        }

    print(f"Work-queue reachable-survivor denominator: {total_denominator}\n")
    for condition in CONDITIONS:
        r = report[condition]
        skr_str = f"{r['skr']:.4f}" if r["skr"] is not None else "n/a"
        print(f"{condition:16s} kept={r['kept_count']:4d}  addressed={r['survivors_addressed']:3d}/{r['work_queue_denominator']:<3d}  skr={skr_str}")

    if report["C"]["skr"] != report["C_minus_gate"]["skr"]:
        print(
            "\nWARNING: skr(C) != skr(C_minus_gate) -- this should not happen by construction "
            "(everything C counts as a kill also satisfies C_minus_gate's weaker inclusion rule). "
            "Investigate before trusting these numbers."
        )
    if report["C_minus_retry"]["skr"] != report["C_minus_both"]["skr"]:
        print(
            "\nNOTE: skr(C_minus_retry) != skr(C_minus_both) -- not necessarily a bug. "
            "C_minus_both drops the passed_on_clean requirement too, so a test that fails on "
            "both clean and mutant source can register as a 'kill' there but not under the "
            "full gate. Check results/generated_tests.jsonl for attempt-1 rows with "
            "killed_target=true and passed_on_clean=false before citing C_minus_both's number."
        )
    if report["C"]["skr"] is not None and report["C_minus_retry"]["skr"] is not None:
        gap = report["C"]["skr"] - report["C_minus_retry"]["skr"]
        print(f"\nretry's isolated contribution to SKR: {gap:+.4f}")

    out_path = ROOT / "results" / "ablation.json"
    out_path.write_text(json.dumps(report, indent=2))
    print(f"\nWrote {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
