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

2. Baseline scoring + reachability: run the real mutation set, confirm mutant
   count lands in the 15-60 band, report the killed/timeout/error breakdown
   per CLAUDE.md's Kill outcome breakdown section, and run the clean suite
   under `coverage` to determine which surviving mutants sit on a line the
   suite never executes at all ("unreachable" -- structurally impossible for
   this suite to kill, independent of assertion quality) versus lines it does
   execute ("reachable-survivor" -- a real assertion gap). Per CLAUDE.md's
   Metrics section, the primary metric's denominator is reachable survivors
   only; raw SKR (unreachable included) is reported alongside it.

3. Held-out partition: reachable survivors are split into the agent's actual
   work queue (operators other than boolop/unary_not) and the held-out set
   (boolop/unary_not survivors, per CLAUDE.md's Held-out operators section).
   The held-out set is never targeted by any arm; it exists to measure
   transfer rate once an arm has run.

Writes results/target_verification.json with a full per-mutant record per
target (mutant_id, operator, outcome, reachable) plus the aggregate counts,
so downstream scripts can slice however they need without recomputing
anything. Exits non-zero if any target fails its canary.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from killcheck.engine import Mutant, generate_mutants
from killcheck.logs import HELD_OUT_OPERATORS
from killcheck.runner import Target, score_target, verify_clean
from killcheck.runner import _evaluate_one  # frozen internal; used read-only here

GARBAGE_SOURCE = "\n\nTHIS IS NOT VALID PYTHON !!! ((( unbalanced and unparseable\n"

ERROR_DOMINANT_THRESHOLD = 0.5  # error outcomes as a share of all kills

COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__", ".git", ".pytest_cache", "*.pyc", ".venv", "node_modules"
)


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


def measure_reachable_lines(target: Target, timeout: int = 60) -> set[int] | None:
    """Run the clean suite under coverage.py and return the set of line numbers
    actually executed in target.module_path. Returns None if coverage itself
    could not produce a reading (reported, never silently treated as "0 lines
    reachable" -- that would misclassify every survivor as unreachable).
    """
    cmd = list(target.test_command)
    try:
        pytest_idx = cmd.index("pytest")
    except ValueError:
        return None
    coverage_cmd = (
        ["python3", "-m", "coverage", "run", "--data-file", ".cov_reach", "-m", "pytest"]
        + cmd[pytest_idx + 1 :]
    )

    with tempfile.TemporaryDirectory(prefix="killcheck-reach-") as tmp:
        work = Path(tmp) / "project"
        shutil.copytree(target.project_root, work, ignore=COPY_IGNORE)
        try:
            subprocess.run(
                coverage_cmd, cwd=work, capture_output=True, text=True, timeout=timeout
            )
            json_proc = subprocess.run(
                ["python3", "-m", "coverage", "json", "--data-file", ".cov_reach",
                 "-o", "cov.json", "-i"],
                cwd=work, capture_output=True, text=True, timeout=timeout,
            )
            if json_proc.returncode != 0:
                return None
            cov_data = json.loads((work / "cov.json").read_text())
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            return None

    module_str = str(target.module_path)
    for file_path, file_data in cov_data.get("files", {}).items():
        if file_path == module_str or file_path.replace("\\", "/").endswith(module_str):
            return set(file_data["executed_lines"])
    return None


def build_mutant_records(target: Target, report: dict, reachable_lines: set[int] | None) -> list[dict]:
    """One record per mutant: id, operator, lineno, outcome, reachable, held_out.
    `reachable` is None (not False) when coverage measurement failed, so a
    downstream consumer can tell "known unreachable" apart from "unknown"."""
    records = []
    for r in report["results"]:
        if reachable_lines is None:
            reachable = None
        else:
            reachable = r["lineno"] in reachable_lines
        records.append(
            {
                "mutant_id": r["mutant_id"],
                "operator": r["operator"],
                "lineno": r["lineno"],
                "outcome": r["outcome"],
                "reachable": reachable,
                "held_out": r["operator"] in HELD_OUT_OPERATORS,
            }
        )
    return records


def summarize_reachability(mutants: list[dict]) -> dict:
    unreachable = sum(1 for m in mutants if m["outcome"] == "survived" and m["reachable"] is False)
    reachable_survivor = sum(1 for m in mutants if m["outcome"] == "survived" and m["reachable"] is True)
    unknown_survivor = sum(1 for m in mutants if m["outcome"] == "survived" and m["reachable"] is None)
    killed = sum(1 for m in mutants if m["outcome"] != "survived")
    return {
        "unreachable": unreachable,
        "reachable_survivor": reachable_survivor,
        "unknown_reachability_survivor": unknown_survivor,
        "killed": killed,
    }


def summarize_held_out(mutants: list[dict]) -> dict:
    work_queue_reachable_survivors = sum(
        1 for m in mutants if m["outcome"] == "survived" and m["reachable"] and not m["held_out"]
    )
    held_out_reachable_survivors = sum(
        1 for m in mutants if m["outcome"] == "survived" and m["reachable"] and m["held_out"]
    )
    held_out_total = sum(1 for m in mutants if m["held_out"])
    return {
        "work_queue_reachable_survivors": work_queue_reachable_survivors,
        "held_out_reachable_survivors": held_out_reachable_survivors,
        "held_out_total_mutants": held_out_total,
    }


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
    print("=== baseline scoring + reachability + held-out partition ===")
    verification_results = []
    error_dominant_targets = []
    unknown_reachability_targets = []
    for t in targets:
        target = Target.from_dict(t, ROOT)
        try:
            rep = score_target(target, timeout=30, workers=4)
            breakdown = outcome_breakdown(rep)
            reachable_lines = measure_reachable_lines(target)
            mutants = build_mutant_records(target, rep, reachable_lines)
            reach = summarize_reachability(mutants)
            held_out = summarize_held_out(mutants)

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
            print(
                f"{'':25s} held-out: work-queue-reachable-survivors={held_out['work_queue_reachable_survivors']:3d}  "
                f"held-out-reachable-survivors={held_out['held_out_reachable_survivors']:3d}  "
                f"held-out-total={held_out['held_out_total_mutants']:3d}"
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
                    "held_out_counts": held_out,
                    "mutants": mutants,
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
