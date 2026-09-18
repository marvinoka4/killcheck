"""Design 3 (METHODOLOGY.md, "Design 3: within-function transfer against a
fresh mutant population") -- the holdout control that actually has a
denominator. Suggested by a reader, joinwell52 (dev.to/joinwell52).

Freezes the 44 kept tests from arm C exactly as gated -- no regeneration,
no retries, zero model calls anywhere in this file -- and asks a question
neither Design 1 nor Design 2 (both abandoned in METHODOLOGY.md for an
insufficient denominator) could answer: does a kept test constrain
behaviour WITHIN the function it was written for, beyond the one mutation
it was shown? It generates a FRESH mutant population restricted to the
functions the 44 kept tests target, excludes everything that was ever in
the original 53-survivor work queue, and scores the frozen kept tests
against that fresh population IN ISOLATION -- without the target's own
original suite present -- because the original suite already kills most
sites in these functions (that is why they were never survivors), and a
run that included it would credit the kept tests with kills that were
never theirs.

Zero model calls anywhere in this file. Serial execution throughout.
engine.py and runner.py are not modified; classify_outcome, _evaluate_one,
build_mutant_records, summarize_reachability, and generate_mutants are
imported and used read-only, exactly as verify_core.py and agent.py
already do. _write_plugin / _run_with_plugin / plugin_outcome_for_test /
enclosing_function / _test_name are reused directly from killcheck.agent
(leading-underscore helpers imported across a module boundary is an
established pattern in this codebase already -- baseline.py and
verify_core.py both import runner.py's _evaluate_one / _run the same way)
rather than re-implemented, so there is exactly one copy of this logic in
the codebase, same reasoning as runner.py's classify_outcome unification
after instrument bug eleven.

Writes results/within_function_transfer.json. Run directly:
    python3 scripts/within_function_transfer.py
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from killcheck.agent import (
    _run_with_plugin,
    _test_name,
    _write_plugin,
    enclosing_function,
    plugin_outcome_for_test,
)
from killcheck.baseline import (
    REPO,
    as_target,
    existing_test_source,
    hoist_future_imports,
    load_targets,
    load_verification,
    reachable_survivor_ids,
)
from killcheck.classify import classify_test
from killcheck.engine import Mutant, generate_mutants
from killcheck.invariants import (
    assert_disjoint,
    assert_ids_subset,
    assert_no_duplicates,
    assert_pooled_conservation,
    assert_reachability_conservation,
)
from killcheck.logs import read_jsonl
from killcheck.runner import Target, classify_outcome
from killcheck.verify_core import (
    COPY_IGNORE,
    build_mutant_records,
    byte_size_canary_check,
    canary_check,
    summarize_reachability,
)

TIMEOUT = 60
FRESH_POPULATION_FLOOR = 100  # per METHODOLOGY.md Design 3: say so before proceeding if pooled < this
NEW_TEST_FILENAME = "test_killcheck_fresh_transfer.py"

COPY_IGNORE_ARGS = dict(ignore=shutil.ignore_patterns(
    "__pycache__", ".git", ".pytest_cache", "*.pyc", ".venv", "node_modules"
))


# ---------------------------------------------------------------------------
# Step 1: freeze the 44 kept tests, loaded from generated_tests.jsonl by
# their recorded gate outcomes -- not from agent_arm_c.json's own 'kept'
# flag, so reconstructing them here is a live cross-check that the two
# records of "kept" still agree, not a second read of the same field.
# ---------------------------------------------------------------------------


def load_kept_tests() -> dict[str, dict[str, tuple[str, str]]]:
    """target -> {mutant_id: (test_name, test_source)}, reconstructed from
    generated_tests.jsonl's own passed_on_clean/killed_target fields: a
    mutant is "kept" if exactly one of its (up to two) logged arm-C
    attempts has both true, per the agent loop contract (retry fires only
    once, and stops the moment an attempt is kept)."""
    records = read_jsonl(ROOT / "results" / "generated_tests.jsonl")
    by_target_mutant: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in records:
        if r["arm"] != "C" or not r["mutant_id"]:
            continue
        by_target_mutant[(r["target"], r["mutant_id"])].append(r)

    kept: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
    for (target, mutant_id), attempts in by_target_mutant.items():
        winning = [a for a in attempts if a["passed_on_clean"] and a["killed_target"]]
        assert len(winning) <= 1, (
            f"{target}/{mutant_id}: {len(winning)} of {len(attempts)} logged attempts both "
            f"passed_on_clean and killed_target -- the agent loop contract allows at most one "
            f"kept attempt per mutant, since it stops retrying the moment one succeeds"
        )
        if winning:
            row = winning[0]
            name = _test_name(row["test_source"])
            assert name, f"{target}/{mutant_id}: kept row has no discoverable test_ function name"
            kept[target][mutant_id] = (name, row["test_source"])
    return kept


def cross_check_against_agent_arm_c(kept: dict[str, dict[str, tuple[str, str]]]) -> None:
    """The kept set reconstructed above from generated_tests.jsonl must
    exactly match agent_arm_c.json's own per_mutant_drafts 'kept' flags --
    two independently-populated records of the same fact agreeing is
    itself evidence, not assumed."""
    arm_c = json.loads((ROOT / "results" / "agent_arm_c.json").read_text())
    for t in arm_c:
        if t.get("skipped"):
            continue
        name = t["target"]
        from_arm_c = {d["mutant_id"] for d in t["per_mutant_drafts"] if d["kept"]}
        from_log = set(kept.get(name, {}).keys())
        if from_arm_c != from_log:
            raise AssertionError(
                f"{name}: kept set from generated_tests.jsonl ({sorted(from_log)}) disagrees "
                f"with agent_arm_c.json's own per_mutant_drafts kept flags ({sorted(from_arm_c)})"
            )


# ---------------------------------------------------------------------------
# Step 2: fresh mutant population, restricted to the functions the kept
# tests target, excluding everything ever in the original work queue.
# ---------------------------------------------------------------------------


def functions_for_kept_mutants(
    module_tree: ast.Module, all_by_id: dict[str, Mutant], kept_mutant_ids: list[str]
) -> set[str]:
    functions = set()
    for mid in kept_mutant_ids:
        fn = enclosing_function(module_tree, all_by_id[mid].lineno)
        if fn is not None:
            functions.add(fn)
    return functions


def fresh_population(
    all_mutants: list[Mutant],
    module_tree: ast.Module,
    kept_functions: set[str],
    excluded_ids: set[str],
) -> dict[str, list[Mutant]]:
    """Every mutant (from a fresh, current-source generate_mutants() call)
    whose enclosing function is one the kept tests target, minus anything
    ever in the original work queue -- grouped by function name."""
    by_function: dict[str, list[Mutant]] = defaultdict(list)
    for m in all_mutants:
        if m.id in excluded_ids:
            continue
        fn = enclosing_function(module_tree, m.lineno)
        if fn in kept_functions:
            by_function[fn].append(m)
    return by_function


# ---------------------------------------------------------------------------
# Step 3: score the frozen kept tests, alone, against the fresh population.
# ---------------------------------------------------------------------------


def _command_for_file(test_command: list[str], project_root: Path, rel_file: str) -> list[str]:
    """Replace every positional argument that resolves to a real path under
    project_root with rel_file, preserving every flag and flag-value
    untouched (e.g. cachetools-func's `-o pythonpath=src`). Same
    file-vs-flag heuristic existing_test_source() already uses
    (killcheck/baseline.py): a bare `-o`'s value ("pythonpython=src") never
    resolves to a real path, so it is left alone by construction, not by a
    special case here."""
    new_cmd = []
    replaced = 0
    for arg in test_command:
        # Path(project_root) / arg silently DISCARDS project_root and
        # returns Path(arg) unchanged when arg is itself absolute (pathlib's
        # documented "/" behaviour) -- so an absolute arg (e.g. an
        # interpreter given as sys.executable rather than the bare "python3"
        # every real targets.json test_command uses) would resolve to a
        # real, existing path REGARDLESS of project_root and get wrongly
        # treated as "this command's test path." Excluding absolute
        # arguments up front closes that gap; caught by this project's own
        # scorer-check fixtures using sys.executable, not by a real target
        # (every real test_command's own interpreter arg is the bare string
        # "python3" -- confirmed directly against targets.json).
        if not arg.startswith("-") and not Path(arg).is_absolute() and (project_root / arg).exists():
            new_cmd.append(rel_file)
            replaced += 1
        else:
            new_cmd.append(arg)
    if replaced == 0:
        raise AssertionError(
            f"_command_for_file: no positional path argument found to replace in {test_command} "
            f"under {project_root} -- cannot point this command at {rel_file} alone"
        )
    if replaced > 1:
        print(
            f"  WARNING: _command_for_file replaced {replaced} positional arguments in "
            f"{test_command}, not just one -- verify {rel_file} is really the only thing this "
            f"command now runs",
            flush=True,
        )
    return new_cmd


def prepare_kept_only_project(target: Target, test_file: Path, kept_sources: list[tuple[str, str]], tmp: Path) -> Target:
    """Copy target.project_root into tmp, write a brand-new file (not an
    edit of test_file -- test_file is left untouched, and target.test_command
    normally scans a whole tests/ directory, so overwriting test_file alone
    would still collect every OTHER file in that directory alongside it)
    containing only the joined kept sources, and return a Target whose
    test_command points at that one new file exclusively."""
    work = tmp / "project"
    shutil.copytree(target.project_root, work, **COPY_IGNORE_ARGS)
    rel_test_dir = test_file.parent.relative_to(target.project_root)
    new_file = work / rel_test_dir / NEW_TEST_FILENAME
    joined = "\n\n".join(src for _, src in kept_sources)
    new_file.write_text(hoist_future_imports("", joined) + "\n")
    _write_plugin(work)
    rel_new_file = str((rel_test_dir / NEW_TEST_FILENAME))
    new_command = _command_for_file(target.test_command, work, rel_new_file)
    return Target(
        name=target.name, project_root=work, module_path=target.module_path, test_command=new_command,
    )


def measure_reachable_lines_single_file(target: Target, timeout: int = 60) -> set[int] | None:
    """Same approach as killcheck.verify_core.measure_reachable_lines --
    same coverage_cmd construction, same COPY_IGNORE, same file-matching --
    but NOT that function, and not a change to it: this experiment's
    coverage runs are scoped to one freshly-written file
    (NEW_TEST_FILENAME), not the target's normal full test_command, and
    every target here has a repo-level fail_under threshold sized for its
    OWN full suite (e.g. slugify: `coverage json` exits 2, "total of 23 is
    less than fail-under=97", confirmed directly against this target's own
    single-file run before writing this function). measure_reachable_lines
    treats any nonzero `coverage json` exit as measurement failure --
    correct for its own callers, all of which run coverage over the
    target's full, normally-configured suite scope, where a nonzero exit
    really does mean the JSON was never produced. That assumption doesn't
    hold for a narrower, single-file scope: `coverage json` can write a
    complete, valid report AND still exit nonzero purely because a
    fail_under threshold tuned for the full suite doesn't clear against a
    handful of kept tests. So this checks for cov.json actually existing
    and parsing, not the subprocess's exit code -- the one behavioural
    difference from measure_reachable_lines, and the reason this is a
    separate function instead of a change to killcheck/verify_core.py."""
    cmd = list(target.test_command)
    try:
        pytest_idx = cmd.index("pytest")
    except ValueError:
        return None
    coverage_cmd = (
        [sys.executable, "-m", "coverage", "run", "--data-file", ".cov_reach", "-m", "pytest"]
        + cmd[pytest_idx + 1 :]
    )
    with tempfile.TemporaryDirectory(prefix="killcheck-wft-reach-") as tmp:
        work = Path(tmp) / "project"
        shutil.copytree(target.project_root, work, ignore=COPY_IGNORE)
        try:
            subprocess.run(coverage_cmd, cwd=work, capture_output=True, text=True, timeout=timeout)
            subprocess.run(
                [sys.executable, "-m", "coverage", "json", "--data-file", ".cov_reach",
                 "-o", "cov.json", "-i"],
                cwd=work, capture_output=True, text=True, timeout=timeout,
            )
            if not (work / "cov.json").exists():
                return None
            cov_data = json.loads((work / "cov.json").read_text())
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            return None
    module_str = str(target.module_path)
    for file_path, file_data in cov_data.get("files", {}).items():
        if file_path == module_str or file_path.replace("\\", "/").endswith(module_str):
            return set(file_data["executed_lines"])
    return None


def score_fresh_mutant_kept_alone(kept_only_target: Target, mutant: Mutant, timeout: int) -> dict:
    """One fresh mutant, scored against the kept-tests-alone file, with
    per-test (plugin) attribution -- mirrors killcheck/agent.py's
    official_batch_rescore's per-mutant loop exactly, minus the original
    suite's presence."""
    with tempfile.TemporaryDirectory(prefix="killcheck-wft-m-") as tmp2:
        work = Path(tmp2) / "project"
        shutil.copytree(kept_only_target.project_root, work, **COPY_IGNORE_ARGS)
        (work / kept_only_target.module_path).write_text(mutant.source)
        _write_plugin(work)
        report_path = Path(tmp2) / "report.jsonl"
        code, output, rows = _run_with_plugin(kept_only_target.test_command, work, timeout, report_path)
    outcome = classify_outcome(code, output)
    return {"outcome": outcome, "exit_code": code, "rows": rows}


def responsible_kept_tests(rows: list[dict], code: int, kept_sources: list[tuple[str, str]]) -> list[str]:
    responsible = []
    for name, _ in kept_sources:
        po = plugin_outcome_for_test(rows, name, code)
        if po.startswith("call-phase"):
            responsible.append(name)
    return responsible


# ---------------------------------------------------------------------------
# Per-target run
# ---------------------------------------------------------------------------


def run_target(spec: dict, verification: dict, kept: dict[str, tuple[str, str]]) -> dict:
    target = as_target(spec)
    name = target.name
    print(f"[{name}] canary + byte-size canary", flush=True)
    ok, detail = canary_check(target)
    if not ok:
        raise AssertionError(f"{name}: canary FAILED before trusting any number from this target: {detail}")
    bs_status, bs_detail = byte_size_canary_check(target)
    if bs_status == "FAIL":
        raise AssertionError(f"{name}: byte-size canary FAILED: {bs_detail}")
    print(f"  canary PASS; byte-size canary {bs_status} ({bs_detail})", flush=True)

    test_file, _ = existing_test_source(spec, target)
    module_source = (target.project_root / target.module_path).read_text()
    module_tree = ast.parse(module_source)
    all_mutants = generate_mutants(module_source, str(target.module_path))
    all_by_id = {m.id: m for m in all_mutants}

    kept_mutant_ids = sorted(kept.keys())
    kept_sources = [kept[mid] for mid in kept_mutant_ids]

    survivor_ids = set(reachable_survivor_ids(verification))
    kept_functions = functions_for_kept_mutants(module_tree, all_by_id, kept_mutant_ids)
    by_function = fresh_population(all_mutants, module_tree, kept_functions, survivor_ids)
    fresh_ids = {m.id for fns in by_function.values() for m in fns}

    # CHECK B-style, this experiment's own pipeline: the fresh population
    # must be provably disjoint from the original work queue, and every
    # fresh id must be a real mutant id this target's committed
    # verification already knows about (guards against engine.py or the
    # module source having drifted since verification ran).
    assert_disjoint(fresh_ids, survivor_ids, context=f"{name} fresh population vs original work queue")
    all_verified_ids = {m["mutant_id"] for m in verification["mutants"]}
    assert_ids_subset(fresh_ids, all_verified_ids, context=f"{name} fresh population vs committed verification")

    pooled_this_target = sum(len(v) for v in by_function.values())
    print(
        f"  kept_functions={sorted(kept_functions)} fresh_population={pooled_this_target} "
        f"({', '.join(f'{fn}={len(ms)}' for fn, ms in sorted(by_function.items()))})",
        flush=True,
    )

    with tempfile.TemporaryDirectory(prefix="killcheck-wft-") as tmp_str:
        tmp = Path(tmp_str)
        kept_only_target = prepare_kept_only_project(target, test_file, kept_sources, tmp)

        clean_code_out = _run_with_plugin(
            kept_only_target.test_command, kept_only_target.project_root, TIMEOUT,
            tmp / "clean_report.jsonl",
        )
        clean_code, clean_output, _ = clean_code_out
        clean_pass = clean_code == 0
        if not clean_pass:
            print(f"  CLEAN-PASS FAILURE for kept-tests-alone file: exit {clean_code}", flush=True)
            return {
                "target": name, "kept_count": len(kept_sources), "clean_pass": False,
                "clean_output": clean_output[-2000:], "kept_functions": sorted(kept_functions),
                "fresh_population_by_function": {fn: len(ms) for fn, ms in by_function.items()},
                "fresh_population_total": pooled_this_target,
            }

        reachable_lines = measure_reachable_lines_single_file(kept_only_target, timeout=TIMEOUT)

        per_mutant = {}
        for m in [mm for fns in by_function.values() for mm in fns]:
            r = score_fresh_mutant_kept_alone(kept_only_target, m, TIMEOUT)
            responsible = responsible_kept_tests(r["rows"], r["exit_code"], kept_sources)
            per_mutant[m.id] = {
                "operator": m.operator,
                "lineno": m.lineno,
                "function": enclosing_function(module_tree, m.lineno),
                "outcome_kept_alone": r["outcome"],
                "responsible_kept_tests": responsible,
            }

    # Reuse build_mutant_records / summarize_reachability UNCHANGED (same
    # functions the main pipeline's own denominator is built from) rather
    # than a parallel reimplementation of the reachable/unreachable split.
    pseudo_report = {
        "results": [
            {"mutant_id": mid, "operator": r["operator"], "lineno": r["lineno"], "outcome": r["outcome_kept_alone"]}
            for mid, r in per_mutant.items()
        ]
    }
    records = build_mutant_records(kept_only_target, pseudo_report, reachable_lines)
    reach = summarize_reachability(records)
    assert_reachability_conservation(reach, pooled_this_target, context=f"{name} fresh population (kept-alone reachability)")
    for rec in records:
        per_mutant[rec["mutant_id"]]["reachable_under_kept"] = rec["reachable"]

    orig_outcome_by_id = {m["mutant_id"]: m["outcome"] for m in verification["mutants"]}
    killed_by_original_alone = sum(
        1 for mid in per_mutant if orig_outcome_by_id.get(mid, "survived") != "survived"
    )

    reachable_ids = {r["mutant_id"] for r in records if r["reachable"] is True}
    killed_by_neither = sum(
        1 for mid in reachable_ids
        if per_mutant[mid]["outcome_kept_alone"] == "survived" and orig_outcome_by_id.get(mid) == "survived"
    )

    kept_test_class = {name_: classify_test(src) for name_, src in kept_sources}
    class_breakdown = Counter()
    class_denominator = Counter()
    for mid in reachable_ids:
        resp = per_mutant[mid]["responsible_kept_tests"]
        classes = {kept_test_class[n] for n in resp}
        for c in classes:
            class_breakdown[c] += 1

    breadth = Counter()  # test_name -> fresh mutants it is responsible for
    for r in per_mutant.values():
        for n in r["responsible_kept_tests"]:
            breadth[n] += 1

    t5_rows = [
        {
            "mutant_id": mid, "function": r["function"], "operator": r["operator"], "lineno": r["lineno"],
            "responsible_kept_tests": r["responsible_kept_tests"],
            "responsible_test_classes": [kept_test_class[n] for n in r["responsible_kept_tests"]],
        }
        for mid, r in per_mutant.items()
        if mid in reachable_ids
        and r["outcome_kept_alone"] != "survived"
        and orig_outcome_by_id.get(mid) == "survived"
    ]

    return {
        "target": name,
        "kept_count": len(kept_sources),
        "clean_pass": True,
        "kept_functions": sorted(kept_functions),
        "fresh_population_by_function": {fn: len(ms) for fn, ms in by_function.items()},
        "fresh_population_by_operator": dict(Counter(m.operator for fns in by_function.values() for m in fns)),
        "fresh_population_total": pooled_this_target,
        "reachable_under_kept": reach["reachable_survivor"] + reach["killed"],
        "unreachable_under_kept": reach["unreachable"],
        "unknown_reachability_under_kept": reach["unknown_reachability_survivor"],
        "killed_by_kept_alone": reach["killed"],
        "survived_kept_alone": reach["reachable_survivor"],
        "killed_by_original_alone": killed_by_original_alone,
        "killed_by_neither": killed_by_neither,
        "class_breakdown_killed_by_kept_alone": dict(class_breakdown),
        "breadth_per_kept_test": dict(breadth),
        "per_mutant": per_mutant,
        "t5_killed_by_kept_not_by_original": t5_rows,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    kept_all = load_kept_tests()
    total_kept = sum(len(v) for v in kept_all.values())
    print(f"loaded kept tests from generated_tests.jsonl: {total_kept} total "
          f"across {len(kept_all)} targets", flush=True)
    assert total_kept == 44, (
        f"expected exactly 44 kept tests (README's/METHODOLOGY.md's published figure), got {total_kept} "
        f"-- STOP, results/generated_tests.jsonl does not match what was pre-registered against"
    )
    cross_check_against_agent_arm_c(kept_all)
    print("cross-check vs agent_arm_c.json's own kept flags: agree", flush=True)

    specs = {s["name"]: s for s in load_targets()}
    verification = load_verification()

    results = []
    started = time.monotonic()
    for target_name, kept in sorted(kept_all.items()):
        spec = specs[target_name]
        v = verification[target_name]
        print(f"\n=== {target_name} ({len(kept)} kept tests) ===", flush=True)
        results.append(run_target(spec, v, kept))

    scored = [r for r in results if r["clean_pass"]]
    pooled_fresh = sum(r["fresh_population_total"] for r in scored)
    pooled_reachable = sum(r["reachable_under_kept"] for r in scored)
    pooled_killed = sum(r["killed_by_kept_alone"] for r in scored)

    assert_pooled_conservation(pooled_fresh, [r["fresh_population_total"] for r in scored], "pooled fresh population")
    assert_pooled_conservation(pooled_reachable, [r["reachable_under_kept"] for r in scored], "pooled reachable-under-kept")
    assert_pooled_conservation(pooled_killed, [r["killed_by_kept_alone"] for r in scored], "pooled killed-by-kept-alone")

    if pooled_fresh < FRESH_POPULATION_FLOOR:
        print(
            f"\nWARNING per METHODOLOGY.md Design 3: pooled fresh population is {pooled_fresh}, "
            f"under the {FRESH_POPULATION_FLOOR} floor -- this has the same denominator problem "
            f"as Design 1 and Design 2. Say so plainly; do not interpret the rate below as if it "
            f"didn't.",
            flush=True,
        )

    pooled_class = Counter()
    for r in scored:
        for c, n in r["class_breakdown_killed_by_kept_alone"].items():
            pooled_class[c] += n

    summary = {
        "kept_count": total_kept,
        "targets_scored": len(scored),
        "targets_clean_fail": [r["target"] for r in results if not r["clean_pass"]],
        "pooled_fresh_mutants_generated": pooled_fresh,
        "pooled_reachable_under_kept": pooled_reachable,
        "pooled_killed_by_kept_alone": pooled_killed,
        "primary_metric_fresh_mutant_kill_rate": round(pooled_killed / pooled_reachable, 4) if pooled_reachable else None,
        "raw_rate_unreachable_included": round(pooled_killed / pooled_fresh, 4) if pooled_fresh else None,
        "pooled_class_breakdown_killed_by_kept_alone": dict(pooled_class),
        "wall_clock_s": round(time.monotonic() - started, 1),
        "targets": results,
    }

    out_path = REPO / "results" / "within_function_transfer.json"
    out_path.write_text(json.dumps(summary, indent=2))

    print(f"\n=== POOLED ===")
    print(f"fresh mutants generated: {pooled_fresh}")
    print(f"reachable under kept-tests-alone: {pooled_reachable}")
    print(f"killed by kept tests alone: {pooled_killed}")
    print(f"primary metric (fresh-mutant kill rate): {summary['primary_metric_fresh_mutant_kill_rate']}")
    print(f"raw rate (unreachable included): {summary['raw_rate_unreachable_included']}")
    print(f"class breakdown of kills: {dict(pooled_class)}")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
