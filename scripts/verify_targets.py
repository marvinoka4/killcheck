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
   breakdown per CLAUDE.md's Kill outcome breakdown section, and run the
   clean suite under `coverage` to determine which surviving mutants sit on a
   line the suite never executes at all ("unreachable" -- structurally
   impossible for this suite to kill, independent of assertion quality)
   versus lines it does execute ("reachable-survivor" -- a real assertion
   gap). Per CLAUDE.md's Metrics section, the primary metric's denominator is
   reachable survivors only; raw SKR (unreachable included) is reported
   alongside it.

There is no held-out-operator partition here. One was built and then
abandoned -- see CLAUDE.md's "Abandoned: holdout transfer control" section
for the numbers that killed it. All reachable survivors are eligible.

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

from killcheck.engine import Mutant
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


def determinism_check(target: Target, timeout: int = 30, runs: int = 3) -> tuple[bool, str, list[dict]]:
    """Run full mutation scoring `runs` times serially and compare the survivor
    SET (not just the count) across every run. Returns (deterministic, detail,
    reports) -- `reports` holds every run's full score_target() output so a
    deterministic target's scoring loop doesn't have to pay for a 4th run.

    This exists because workers=4 (the runner's old default) produced a
    different survivor set on every run for one real target (async I/O
    against real temp files) -- confirmed by hand, not assumed fixed by
    switching the default to workers=1. This check is the standing gate that
    replaces "confirmed by hand": any target that varies, even at workers=1,
    is quarantined and reported, never silently averaged into the pooled
    number. See CHANGELOG.md for how the original bug was found.
    """
    reports = [score_target(target, timeout=timeout, workers=1) for _ in range(runs)]
    survivor_sets = [
        frozenset(r["mutant_id"] for r in rep["results"] if r["outcome"] == "survived")
        for rep in reports
    ]
    deterministic = all(s == survivor_sets[0] for s in survivor_sets[1:])
    if deterministic:
        detail = f"survivor set identical across {runs} runs ({len(survivor_sets[0])} survivors)"
    else:
        sizes = [len(s) for s in survivor_sets]
        detail = f"survivor set VARIED across {runs} runs (sizes: {sizes}) -- QUARANTINED"
    return deterministic, detail, reports


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
    """One record per mutant: id, operator, lineno, outcome, reachable.
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
        except Exception as e:
            print(f"{t['name']:25s} FAILED: {e}")
            verification_results.append({"name": t["name"], "failed": str(e)})

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
