"""Target-verification primitives: canary checks, determinism check, and
reachability measurement.

Extracted from scripts/verify_targets.py (pure code motion, no logic
changed -- every function body below is byte-identical to what previously
lived there) so this logic is part of the installable `killcheck` package
and reachable from `killcheck.cli`'s `score`/`verify`/`harden` commands
without depending on `scripts/`, which is eval-set batch tooling, not part
of the library. scripts/verify_targets.py now imports from here instead of
defining these itself, so its own eval-set behavior (results/
target_verification.json, the denominator manifest the primary metric
reads) is unchanged -- verified by re-running it in full and diffing the
output against the previously-committed file, zero mismatches. See
CHANGELOG.md for that verification.

Nothing here is frozen (only killcheck/engine.py and killcheck/runner.py
are, per METHODOLOGY.md invariant 5) -- but build_mutant_records/
summarize_reachability/outcome_breakdown feed the primary metric's
denominator, so changes here get the same re-verify-before-trusting
discipline as a frozen-core change even though it isn't formally required.

One deliberate change since the move: measure_reachable_lines() invoked the
literal command name "python3" for its coverage subprocess rather than
sys.executable -- harmless in every environment this project had actually
run in (this venv's own bin/ always has a python3), but wrong for a
stranger's environment where only `python` is on PATH, where it would
silently degrade every survivor to reachability=UNKNOWN rather than report
a wrong interpreter. Changed to sys.executable and re-verified against all
12 eval-set targets the same way as the extraction itself -- see
CHANGELOG.md's "python3 -> sys.executable" entry.
"""
import ast
import io
import json
import shutil
import subprocess
import sys
import tempfile
import tokenize
from pathlib import Path

from killcheck.engine import Mutant
from killcheck.runner import Target, score_target
from killcheck.runner import _evaluate_one  # frozen internal; used read-only here

GARBAGE_SOURCE = "\n\nTHIS IS NOT VALID PYTHON !!! ((( unbalanced and unparseable\n"

ERROR_DOMINANT_THRESHOLD = 0.5  # error outcomes as a share of all kills

COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__", ".git", ".pytest_cache", "*.pyc", ".venv", "node_modules"
)


def outcome_breakdown(report: dict) -> dict:
    """Disaggregate score_target()'s pooled kill count into killed/timeout/error,
    per METHODOLOGY.md's Kill outcome breakdown section. score_target() itself only
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


# CPython's default timestamp-based .pyc invalidation keys on source mtime
# (whole seconds) + source size. GARBAGE_SOURCE above has a different byte
# size than the real module, which itself invalidates any stale .pyc header
# -- so the canary above cannot detect a stale-bytecode-executes-instead-of
# the-mutation failure mode by construction, even though ignore_patterns
# already strips __pycache__/*.pyc from every tempdir copy this pipeline
# makes (see the runner.py comment above COPY_IGNORE's use, and CHANGELOG.md
# for the empirical check that confirmed this both ways: a synthetic
# reproducer proved CPython really is fooled by a matching mtime+size, and a
# real copytree from a target with genuine committed .pyc files produced
# zero .pyc in the destination). This canary closes that gap: same total
# file byte length as the real module, syntactically valid (unlike
# GARBAGE_SOURCE), a single same-length comparison-operator flip
# (== <-> !=, < <-> >, <= <-> >=) so a stale-but-header-matching .pyc would
# have to be silently substituted for this exact source to go undetected.
_SAME_LEN_FLIPS = [("==", "!="), ("!=", "=="), ("<=", ">="), (">=", "<="), ("<", ">"), (">", "<")]


def _byte_size_flip_candidates(source: str) -> list[tuple[str, str, int]]:
    """Every eligible same-length comparison-operator flip found via
    `tokenize` (so an occurrence inside a string or comment is never
    touched -- tokenize already classifies those separately from OP tokens,
    which is the actual guarantee here, not the parse check below), as
    (mutated_source, description, lineno) triples in file order. Split out
    from byte_size_preserving_mutation so a caller can filter candidates by
    reachability without re-tokenizing the module."""
    candidates = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type != tokenize.OP:
            continue
        for orig, flipped in _SAME_LEN_FLIPS:
            if tok.string != orig:
                continue
            lines = source.splitlines(keepends=True)
            row, col = tok.start
            line = lines[row - 1]
            new_line = line[: col] + flipped + line[tok.end[1] :]
            if len(new_line) != len(line):
                continue
            lines[row - 1] = new_line
            candidate = "".join(lines)
            if len(candidate) != len(source):
                continue
            try:
                ast.parse(candidate)
            except SyntaxError:
                continue
            candidates.append((candidate, f"{orig} -> {flipped} at line {row}, col {col}", row))
    return candidates


def byte_size_preserving_mutation(
    source: str, reachable_lines: set[int] | None = None
) -> tuple[str, str] | None:
    """Return (mutated_source, description) for a same-length
    comparison-operator flip, or None if none is eligible.

    If `reachable_lines` is given (the set of line numbers the clean suite
    actually executes, from coverage), prefers the first eligible flip that
    sits on one of those lines, and returns None if no eligible flip does.
    A flip on a line the suite never executes can't distinguish "detected"
    from "stale" from "just never ran" -- it isn't a usable test of
    staleness at all. An earlier version of this function had no such
    filter, always returning the first eligible flip found anywhere in the
    file regardless of reachability; byte_size_canary_check then asserted
    non-survival on it as evidence of staleness. On a real, uncurated
    target (jsonschema's _utils.py) this produced a confident, WRONG
    diagnosis: the flagged line was never executed by the test command in
    play at all (confirmed directly via coverage), so the mutation
    "survived" for the mundane reason that it never ran, not because of
    stale bytecode. See CHANGELOG.md's "byte_size_canary_check must check
    reachability" entry.

    If `reachable_lines` is None, no reachability filter is applied -- the
    original, non-reachability-aware selection. Still used as-is by
    scripts/test_pyc_exclusion.py's own reproduction, which deliberately
    wants any eligible same-length mutation to exercise the copy-and-score
    path, regardless of what the suite happens to cover.
    """
    candidates = _byte_size_flip_candidates(source)
    if not candidates:
        return None
    if reachable_lines is None:
        candidate, desc, _row = candidates[0]
        return candidate, desc
    for candidate, desc, row in candidates:
        if row in reachable_lines:
            return candidate, desc
    return None


def byte_size_canary_check(target: Target, timeout: int = 30) -> tuple[str, str]:
    """Return (status, detail). status is "PASS" (a same-byte-length
    mutation on a line the suite actually executes was detected -- no
    stale-bytecode substitution occurred), "FAIL" (that mutation was
    reported as survived -- exactly the failure mode this check exists to
    catch, and a warranted staleness diagnosis because reachability was
    confirmed FIRST, not assumed), or "N/A" for any of three different
    underlying reasons, distinguished in `detail`: no eligible
    comparison-operator flip exists anywhere in the module; eligible flips
    exist but none sit on a line the suite executes under this test
    command (so there is no reachable site left to test); or reachability
    itself couldn't be measured for this module (coverage failed).

    Reachability-gated: picks a mutation site among lines coverage confirms
    the suite executes, not merely the first eligible site in the file. See
    byte_size_preserving_mutation's docstring for why -- the un-gated
    version produced a false "possible stale-bytecode execution" diagnosis
    on a real, uncurated target."""
    source = (target.project_root / target.module_path).read_text()
    reachable_lines = measure_reachable_lines(target, timeout=timeout)
    if reachable_lines is None:
        return (
            "N/A",
            "could not measure line coverage for this module -- cannot confirm a reachable "
            "same-length mutation site to test",
        )

    result = byte_size_preserving_mutation(source, reachable_lines)
    if result is None:
        if _byte_size_flip_candidates(source):
            return (
                "N/A",
                "same-length comparison operator(s) exist in this module, but none sit on a "
                "line the suite executes under this test command -- no reachable site available",
            )
        return "N/A", "no same-length comparison operator found in this module"

    candidate, desc = result
    fake = Mutant(
        id="M-byte-canary",
        module=str(target.module_path),
        lineno=0,
        col_offset=0,
        operator="byte-canary",
        description=desc,
        original_line="",
        mutated_line="",
        source=candidate,
    )
    result_ = _evaluate_one(target, fake, timeout)
    if result_.outcome == "survived":
        return (
            "FAIL",
            f"suite PASSED on a same-byte-length mutation on a REACHABLE line ({desc}) -- "
            f"possible stale-bytecode execution",
        )
    return "PASS", f"suite correctly reported '{result_.outcome}' on a same-byte-length mutation ({desc})"


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
        [sys.executable, "-m", "coverage", "run", "--data-file", ".cov_reach", "-m", "pytest"]
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
                [sys.executable, "-m", "coverage", "json", "--data-file", ".cov_reach",
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
