"""Arm C -- the agent loop.

For each target, for each reachable survivor (deterministic mutant-id
order): one call, asking for exactly one test targeting that specific
mutation. Gate it -- pass on clean, fail on the mutant -- and retry exactly
once, feeding back the real pytest output, if the gate rejects. Kept tests
accumulate per target; the OFFICIAL kill count comes from one batch rescore
of the final kept set at the end, never from the per-call gate outcome
(CLAUDE.md's batch-vs-incremental rule applies here exactly as it does to
arms A and B).

See CLAUDE.md's "Agent loop contract" for the design this implements, and
tripwires.md for the abort conditions checked during the run.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from killcheck.baseline import (
    MODEL,
    MAX_TOKENS,
    REPO,
    RULES,
    _client,
    call_model,
    extract_code,
    load_targets,
    load_verification,
    as_target,
    reachable_survivor_ids,
    existing_test_source,
    hoist_future_imports,
)
from killcheck.engine import generate_mutants, Mutant, OPERATOR_NAMES
from killcheck.logs import log_generated_test, log_trajectory
from killcheck.runner import Target
from killcheck.classify import classify_test, UNITTEST_VALUE_METHODS, UNITTEST_EXISTENCE_METHODS

# Run order per the design: smoke test first, then the two largest
# denominators, then the rest. Left as an explicit list, not "all targets in
# whatever order targets.json happens to have them," so the run order is
# itself part of the committed design, not incidental.
RUN_ORDER = [
    "slugify-special",
    "tenacity-stop",
    "cachetools-func",
    "validators-card",
    "natsort-ns-enum",
    "dictdiffer-resolve",
    "toolz-dicttoolz",
    "shortuuid-main",
    "boltons-typeutils",
    "aiofiles-temptypes",
]


# ---------------------------------------------------------------------------
# The pytest plugin -- built first, per instructions, before anything else
# depends on it. A conftest.py written into every scored tempdir. Records,
# per test x mutant run: nodeid, when (setup/call/teardown), outcome
# (passed/failed/skipped), exc_type (the exception class name if that phase
# raised, else None). Parses nothing from pytest's stdout -- this is pytest's
# own hook machinery, not string-scraping a terminal report.
# ---------------------------------------------------------------------------

_CONFTEST_PLUGIN_SOURCE = '''
# Written by killcheck/agent.py -- records per-test-phase outcomes for the
# agent's gate and official-scoring checks. Not part of the target's own
# test suite; reads KILLCHECK_PLUGIN_REPORT from the environment and no-ops
# if it isn't set (so this file is harmless if ever run outside the harness).
import json
import os

import pytest

_PATH = os.environ.get("KILLCHECK_PLUGIN_REPORT")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    report._killcheck_exc_type = (
        call.excinfo.typename if call.excinfo is not None else None
    )


def pytest_runtest_logreport(report):
    if not _PATH:
        return
    rec = {
        "nodeid": report.nodeid,
        "when": report.when,
        "outcome": report.outcome,
        "exc_type": getattr(report, "_killcheck_exc_type", None),
    }
    with open(_PATH, "a") as f:
        f.write(json.dumps(rec) + "\\n")
'''


def _write_plugin(work: Path) -> None:
    """Write (or extend) work/conftest.py with the plugin. Extends rather
    than overwrites if a target already has a root conftest.py (none of
    this eval set's 12 targets do -- checked directly -- but this is cheap
    insurance against silently deleting a target's own fixtures)."""
    path = work / "conftest.py"
    if path.exists():
        path.write_text(path.read_text() + "\n\n" + _CONFTEST_PLUGIN_SOURCE)
    else:
        path.write_text(_CONFTEST_PLUGIN_SOURCE)


def _run_with_plugin(
    cmd: list[str], cwd: Path, timeout: int, report_path: Path
) -> tuple[int, str, list[dict]]:
    """Like runner.py's _run(), plus the plugin's captured rows. Not reusing
    runner.py's _run() directly because it doesn't accept an env override
    and runner.py is frozen -- this is a separate, agent.py-owned helper."""
    if report_path.exists():
        report_path.unlink()
    env = os.environ.copy()
    env["KILLCHECK_PLUGIN_REPORT"] = str(report_path)
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env
        )
        code = proc.returncode
        output = (proc.stdout + proc.stderr)[-4000:]
    except subprocess.TimeoutExpired:
        code, output = -9, "TIMEOUT"
    rows = []
    if report_path.exists():
        for line in report_path.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return code, output, rows


def _classify_outcome(code: int, output: str) -> str:
    """Mirrors runner.py's _evaluate_one() outcome classification exactly
    (killed/survived/timeout/error), duplicated rather than imported because
    runner.py is frozen and doesn't expose this as a standalone function."""
    if code == -9:
        return "timeout"
    if code == 0:
        return "survived"
    if "ERROR" in output and "collected 0 items" in output:
        return "error"
    return "killed"


def rows_for_nodeid_suffix(rows: list[dict], name: str) -> list[dict]:
    return [r for r in rows if r["nodeid"].endswith("::" + name)]


def plugin_outcome_for_test(rows: list[dict], name: str, exit_code: int) -> str:
    """One of: "timeout", "collection-error" (the plugin never saw this
    test's call phase at all -- the file broke before pytest could even
    collect it), "call-phase AssertionError", "call-phase other exception",
    "not-this-test" (the run failed but this specific test's own call phase
    passed -- something else in the batch is responsible; see tripwires.md's
    batch-rescore-disagreement condition)."""
    if exit_code == -9:
        return "timeout"
    my_rows = rows_for_nodeid_suffix(rows, name)
    call_rows = [r for r in my_rows if r["when"] == "call"]
    if not call_rows:
        return "collection-error"
    call = call_rows[0]
    if call["outcome"] != "failed":
        return "not-this-test"
    if call["exc_type"] == "AssertionError":
        return "call-phase AssertionError"
    return "call-phase other exception"


# ---------------------------------------------------------------------------
# Mechanical features -- pre-registered, computed identically for every
# draft regardless of outcome.
# ---------------------------------------------------------------------------


def enclosing_function(tree: ast.Module, lineno: int) -> str | None:
    """Name of the innermost function/method whose line range contains
    `lineno` in `tree`, or None if the line isn't inside any function."""
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = node.end_lineno or node.lineno
            if node.lineno <= lineno <= end:
                if best is None or (end - node.lineno) < (
                    (best.end_lineno or best.lineno) - best.lineno
                ):
                    best = node
    return best.name if best else None


def calls_function(code: str, name: str | None) -> bool:
    if not name:
        return False
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id == name:
                return True
            if isinstance(f, ast.Attribute) and f.attr == name:
                return True
    return False


def mechanical_features(code: str, mutated_function: str | None) -> dict:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {
            "parses": False,
            "calls_mutated_function": False,
            "assert_count": 0,
            "has_equality_to_literal": False,
            "has_other_comparison": False,
            "ast_node_count": 0,
        }
    assert_count = 0
    has_eq_literal = False
    has_other_cmp = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            assert_count += 1
            expr = node.test
            if isinstance(expr, ast.Compare):
                operands = [expr.left, *expr.comparators]
                is_eq = any(isinstance(op, (ast.Eq, ast.NotEq)) for op in expr.ops)
                touches_literal = any(isinstance(o, ast.Constant) for o in operands)
                if is_eq and touches_literal:
                    has_eq_literal = True
                else:
                    has_other_cmp = True
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            # unittest.TestCase style (self.assertEqual(...) etc.) has no
            # ast.Assert node at all -- checked directly against a real
            # model response before assuming bare `assert` coverage was
            # enough; same UNITTEST_VALUE_METHODS/UNITTEST_EXISTENCE_METHODS
            # split as scripts/classify_tests.py uses for its taxonomy.
            call = node.value
            if isinstance(call.func, ast.Attribute):
                assert_count += 1 if call.func.attr.startswith("assert") else 0
                if call.func.attr in UNITTEST_VALUE_METHODS:
                    args = call.args
                    touches_literal = any(isinstance(a, ast.Constant) for a in args)
                    if call.func.attr in ("assertEqual", "assertNotEqual") and touches_literal:
                        has_eq_literal = True
                    else:
                        has_other_cmp = True
                elif call.func.attr in UNITTEST_EXISTENCE_METHODS:
                    pass  # existence-only signal, not a comparison either way
    return {
        "parses": True,
        "calls_mutated_function": calls_function(code, mutated_function),
        "assert_count": assert_count,
        "has_equality_to_literal": has_eq_literal,
        "has_other_comparison": has_other_cmp,
        "ast_node_count": sum(1 for _ in ast.walk(tree)),
    }


# ---------------------------------------------------------------------------
# Draft generation
# ---------------------------------------------------------------------------

GENERATE_PROMPT = """You are hardening a Python test suite against one specific mutation the current suite fails to detect.

Module under test ({module_path}):
```python
{module_source}
```

Its existing tests:
```python
{test_source}
```
{already}
The following mutation survives the current suite -- the code changed at the
line below and no existing test noticed:

operator: {operator} ({operator_description})
line {lineno}:
  before: {original_line}
  after:  {mutated_line}

Write exactly ONE test function that passes against the module as shown
above (before the mutation) and fails against the mutated version (the
"after" line in place of the "before" line). Assert on observable
behaviour, not on implementation internals. Deterministic: no network, no
wall-clock sleeps, no dependence on execution order or other tests.
Standalone: include any imports it needs; do not rely on names defined only
in the existing test file, and do not define a helper function or constant
whose name might already exist there.

Return only the test in a single ```python code block, no commentary."""

RETRY_SUFFIX = """

Your previous attempt did not pass this check:
```python
{previous_code}
```
Actual pytest output:
```
{pytest_output}
```

Write a corrected test, still targeting the same mutation. Return only the
test in a single ```python code block, no commentary."""


def _first_test_def(body: list[ast.stmt]) -> ast.AST | None:
    """First test def/method in `body`, in document order, recursing into
    classes (unittest.TestCase-style tests are a method inside a class, not
    a top-level def -- checked directly against a real model response that
    used this shape; a top-level-only search silently treats every
    class-based test as if no test existed at all)."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            return node
        if isinstance(node, ast.ClassDef):
            found = _first_test_def(node.body)
            if found is not None:
                return found
    return None


def _count_test_defs(body: list[ast.stmt]) -> int:
    count = 0
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            count += 1
        elif isinstance(node, ast.ClassDef):
            count += _count_test_defs(node.body)
    return count


def _enforce_one_test(code: str) -> tuple[str, bool]:
    """Hard cap: exactly one test (top-level function or a class method,
    either counts) per response. If more than one is present anywhere, keep
    everything up to and including the end of the FIRST one (preserving
    anything it might depend on that precedes it -- an import, a class's
    setUp, other lines of the same class up to that method) and drop
    everything after. Works by exact line span, not re-unparsing, so
    nothing gets reformatted. Returns (kept_code, overflowed).

    Without this, arm C rebuilds arm A's batching inside itself one call at
    a time, and the volume/design comparison the two arms exist to make
    stops meaning anything.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, False
    if _count_test_defs(tree.body) <= 1:
        return code, False
    first = _first_test_def(tree.body)
    end_line = first.end_lineno
    lines = code.splitlines()
    return "\n".join(lines[:end_line]), True


def _test_name(code: str) -> str | None:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    node = _first_test_def(tree.body)
    return node.name if node else None


def build_prompt(
    module_path: str,
    module_source: str,
    test_source: str,
    mutant: Mutant,
    already_names: list[str],
) -> str:
    already = ""
    if already_names:
        already = (
            "\nTests already written this run for this target (do not "
            "duplicate): " + ", ".join(already_names) + "\n"
        )
    return GENERATE_PROMPT.format(
        module_path=module_path,
        module_source=module_source,
        test_source=test_source,
        already=already,
        operator=mutant.operator,
        operator_description=OPERATOR_NAMES.get(mutant.operator, mutant.operator),
        lineno=mutant.lineno,
        original_line=mutant.original_line.strip(),
        mutated_line=mutant.mutated_line.strip(),
    )


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def gate_check(
    target: Target, test_file: Path, candidate_code: str, mutant: Mutant | None, timeout: int = 60
) -> dict:
    """Run one candidate test against clean source (mutant=None) or a
    mutant's source, in an isolated tempdir copy. Returns exit_code, output,
    plugin rows, and the derived outcome."""
    rel_test = test_file.relative_to(target.project_root)
    with tempfile.TemporaryDirectory(prefix="killcheck-agent-gate-") as tmp:
        work = Path(tmp) / "project"
        shutil.copytree(
            target.project_root,
            work,
            ignore=shutil.ignore_patterns(
                "__pycache__", ".git", ".pytest_cache", "*.pyc", ".venv"
            ),
        )
        if mutant is not None:
            (work / target.module_path).write_text(mutant.source)
        aug = work / rel_test
        aug.write_text(hoist_future_imports(aug.read_text(), candidate_code) + "\n")
        _write_plugin(work)
        report_path = Path(tmp) / "plugin_report.jsonl"
        code, output, rows = _run_with_plugin(target.test_command, work, timeout, report_path)
    outcome = _classify_outcome(code, output)
    return {
        "exit_code": code,
        "output": output,
        "rows": rows,
        "outcome": outcome,
    }


def draft_and_gate(
    client,
    target: Target,
    test_file: Path,
    module_path: str,
    module_source: str,
    test_source: str,
    mutant: Mutant,
    already_names: list[str],
    run_id: str,
    traj_dir: Path,
    results_dir: Path,
    mutated_function: str | None,
) -> dict:
    """One mutant's full attempt sequence: draft, gate, retry once on
    rejection. Returns a record with everything needed for logging and the
    final tables -- both attempts' data if a retry fired."""
    attempts = []
    prev_code = None
    prev_output = None

    for attempt in (1, 2):
        if attempt == 1:
            prompt = build_prompt(module_path, module_source, test_source, mutant, already_names)
        else:
            base = build_prompt(module_path, module_source, test_source, mutant, already_names)
            prompt = base + RETRY_SUFFIX.format(
                previous_code=prev_code, pytest_output=prev_output
            )

        text, pin, pout, truncated = call_model(client, prompt)
        raw_code = extract_code(text)
        code, overflowed = _enforce_one_test(raw_code)
        name = _test_name(code)

        log_trajectory(
            trajectories_dir=traj_dir,
            run_id=run_id,
            target=target.name,
            mutant_id=mutant.id,
            phase="generate",
            prompt_tokens=pin,
            completion_tokens=pout,
            content=code,
            outcome=f"attempt{attempt}",
            truncated=truncated,
        )

        clean = gate_check(target, test_file, code, None) if name else {
            "exit_code": 1, "output": "no test function found", "rows": [], "outcome": "error"
        }
        passed_on_clean = clean["outcome"] == "survived"  # "survived" here means the clean suite passed

        killed_target = False
        mutant_gate = None
        if passed_on_clean:
            mutant_gate = gate_check(target, test_file, code, mutant)
            killed_target = mutant_gate["outcome"] in ("killed", "timeout", "error")

        log_trajectory(
            trajectories_dir=traj_dir,
            run_id=run_id,
            target=target.name,
            mutant_id=mutant.id,
            phase="gate_clean",
            prompt_tokens=0,
            completion_tokens=0,
            content=clean["output"][-1000:],
            outcome="pass" if passed_on_clean else "fail",
        )
        if mutant_gate is not None:
            log_trajectory(
                trajectories_dir=traj_dir,
                run_id=run_id,
                target=target.name,
                mutant_id=mutant.id,
                phase="gate_mutant",
                prompt_tokens=0,
                completion_tokens=0,
                content=mutant_gate["output"][-1000:],
                outcome="killed" if killed_target else "survived",
            )

        kept = passed_on_clean and killed_target
        feats = mechanical_features(code, mutated_function)
        p_outcome = (
            plugin_outcome_for_test(mutant_gate["rows"], name, mutant_gate["exit_code"])
            if (mutant_gate is not None and name)
            else ("n/a" if not passed_on_clean else "no-test-name")
        )
        assertion_class = classify_test(code) if code.strip() else "none"

        log_generated_test(
            results_dir=results_dir,
            arm="C",
            target=target.name,
            mutant_id=mutant.id,
            attempt=attempt,
            passed_on_clean=passed_on_clean,
            killed_target=killed_target,
            test_source=code,
            prompt_tokens=pin,
            completion_tokens=pout,
        )

        record = {
            "target": target.name,
            "mutant_id": mutant.id,
            "attempt": attempt,
            "test_source": code,
            "test_name": name,
            "passed_on_clean": passed_on_clean,
            "killed_target": killed_target,
            "kept": kept,
            "overflowed": overflowed,
            "truncated": truncated,
            "tokens_in": pin,
            "tokens_out": pout,
            "assertion_class": assertion_class,
            "plugin_outcome": p_outcome,
            "clean_output": clean["output"][-1500:] if not passed_on_clean else "",
            **feats,
        }
        attempts.append(record)

        decision = "kept" if kept else ("retry" if attempt == 1 else "discarded")
        log_trajectory(
            trajectories_dir=traj_dir,
            run_id=run_id,
            target=target.name,
            mutant_id=mutant.id,
            phase="decision",
            prompt_tokens=0,
            completion_tokens=0,
            content="",
            outcome=decision,
        )

        if kept:
            break
        if attempt == 1:
            prev_code = code
            prev_output = (mutant_gate["output"] if mutant_gate is not None else clean["output"])[-1500:]
            continue
        break

    return {"attempts": attempts, "kept": attempts[-1]["kept"]}


# ---------------------------------------------------------------------------
# Official scoring -- batch rescore, serial, never incremental
# ---------------------------------------------------------------------------


def official_batch_rescore(
    target: Target,
    test_file: Path,
    kept_sources: list[tuple[str, str]],  # (test_name, source)
    reachable_mutants: list[Mutant],
    timeout: int = 60,
) -> dict:
    """Append every kept test once, then score every reachable-survivor
    mutant in one pass, serially. Returns per-mutant outcome plus, for each
    killed mutant, which kept test(s) had a call-phase failure in that run
    (collateral-kill attribution) and their exc_type."""
    rel_test = test_file.relative_to(target.project_root)
    added = "\n\n".join(src for _, src in kept_sources)

    with tempfile.TemporaryDirectory(prefix="killcheck-agent-official-") as tmp:
        base_work = Path(tmp) / "project"
        shutil.copytree(
            target.project_root,
            base_work,
            ignore=shutil.ignore_patterns(
                "__pycache__", ".git", ".pytest_cache", "*.pyc", ".venv"
            ),
        )
        aug = base_work / rel_test
        aug.write_text(hoist_future_imports(aug.read_text(), added) + "\n")
        _write_plugin(base_work)

        clean_report = Path(tmp) / "clean_report.jsonl"
        clean_code, clean_output, _ = _run_with_plugin(
            target.test_command, base_work, timeout, clean_report
        )
        clean_pass = clean_code == 0

        per_mutant = {}
        if clean_pass:
            for m in reachable_mutants:
                with tempfile.TemporaryDirectory(prefix="killcheck-agent-official-m-") as tmp2:
                    work = Path(tmp2) / "project"
                    shutil.copytree(
                        base_work,
                        work,
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
                    )
                    (work / target.module_path).write_text(m.source)
                    report_path = Path(tmp2) / "report.jsonl"
                    code, output, rows = _run_with_plugin(
                        target.test_command, work, timeout, report_path
                    )
                    outcome = _classify_outcome(code, output)
                    responsible = []
                    for name, _ in kept_sources:
                        po = plugin_outcome_for_test(rows, name, code)
                        if po.startswith("call-phase"):
                            responsible.append({"test_name": name, "plugin_outcome": po})
                    per_mutant[m.id] = {
                        "outcome": outcome,
                        "responsible_tests": responsible,
                    }

    return {
        "clean_pass": clean_pass,
        "clean_output": clean_output[-2000:] if not clean_pass else "",
        "per_mutant": per_mutant,
    }


# ---------------------------------------------------------------------------
# Per-target run
# ---------------------------------------------------------------------------


def run_target(client, spec: dict, verification: dict, run_id: str) -> dict:
    target = as_target(spec)
    survivors = sorted(reachable_survivor_ids(verification))
    if not survivors:
        return {"target": target.name, "reachable_survivors": 0, "skipped": "no reachable survivors"}

    test_file, test_source = existing_test_source(spec, target)
    module_source = (target.project_root / target.module_path).read_text()
    module_tree = ast.parse(module_source)
    mutant_by_id = {m.id: m for m in generate_mutants(module_source, str(target.module_path))}

    results_dir = REPO / "results"
    traj_dir = REPO / "trajectories"

    started = time.monotonic()
    kept_sources: list[tuple[str, str]] = []
    already_names: list[str] = []
    per_mutant_drafts = []
    overflow_count = 0
    retries_fired = 0
    retries_succeeded = 0
    tokens_in = tokens_out = 0

    for mid in survivors:
        mutant = mutant_by_id[mid]
        mfunc = enclosing_function(module_tree, mutant.lineno)
        result = draft_and_gate(
            client, target, test_file, str(target.module_path), module_source, test_source,
            mutant, already_names, run_id, traj_dir, results_dir, mfunc,
        )
        per_mutant_drafts.append({"mutant_id": mid, "function": mfunc, **result})
        for a in result["attempts"]:
            tokens_in += a["tokens_in"]
            tokens_out += a["tokens_out"]
            if a["overflowed"]:
                overflow_count += 1
        if len(result["attempts"]) > 1:
            retries_fired += 1
            if result["kept"]:
                retries_succeeded += 1
        if result["kept"]:
            kept = result["attempts"][-1]
            kept_sources.append((kept["test_name"], kept["test_source"]))
            already_names.append(kept["test_name"])
        print(
            f"  [C] {target.name} mutant {mid} "
            f"({'kept' if result['kept'] else 'discarded'}, "
            f"{len(result['attempts'])} attempt(s))",
            flush=True,
        )

    reachable_mutants = [mutant_by_id[mid] for mid in survivors]
    official = official_batch_rescore(target, test_file, kept_sources, reachable_mutants)

    official_killed = sum(
        1 for r in official["per_mutant"].values() if r["outcome"] != "survived"
    )

    return {
        "target": target.name,
        "reachable_survivors": len(survivors),
        "drafts": sum(len(d["attempts"]) for d in per_mutant_drafts),
        "kept": len(kept_sources),
        "discarded": len(survivors) - len(kept_sources),
        "retries_fired": retries_fired,
        "retries_succeeded": retries_succeeded,
        "overflow_count": overflow_count,
        "official_clean_pass": official["clean_pass"],
        "official_clean_output": official["clean_output"],
        "official_killed": official_killed,
        "official_per_mutant": official["per_mutant"],
        "per_mutant_drafts": per_mutant_drafts,
        "mutant_function": {mid: enclosing_function(module_tree, mutant_by_id[mid].lineno) for mid in survivors},
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "wall_clock_s": round(time.monotonic() - started, 1),
        "test_file": str(test_file.relative_to(target.project_root)),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", action="append", help="run only this target (repeatable)")
    args = ap.parse_args()

    specs = {s["name"]: s for s in load_targets()}
    verification = load_verification()
    client = _client()
    run_id = f"armC-{time.strftime('%Y%m%d-%H%M%S')}"

    targets = args.target if args.target else RUN_ORDER
    out = []
    for name in targets:
        spec = specs.get(name)
        if spec is None:
            print(f"  skip {name}: not in targets.json", flush=True)
            continue
        v = verification.get(name)
        if v is None:
            print(f"  skip {name}: not in verification", flush=True)
            continue
        print(f"[C] {name}", flush=True)
        out.append(run_target(client, spec, v, run_id))

    results_dir = REPO / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / "agent_arm_c.json"
    existing = []
    if path.exists():
        existing = json.loads(path.read_text())
    by_name = {r["target"]: r for r in existing}
    for r in out:
        by_name[r["target"]] = r
    path.write_text(json.dumps(list(by_name.values()), indent=2))

    scored = [r for r in out if not r.get("skipped")]
    for r in scored:
        print(
            f"\n[{r['target']}] reachable={r['reachable_survivors']} "
            f"drafts={r['drafts']} kept={r['kept']} discarded={r['discarded']} "
            f"retries={r['retries_fired']} (succeeded {r['retries_succeeded']}) "
            f"overflow={r['overflow_count']} "
            f"official_killed={r['official_killed']}/{r['reachable_survivors']} "
            f"clean_pass={r['official_clean_pass']} "
            f"tokens={r['tokens_in']}+{r['tokens_out']} "
            f"wall={r['wall_clock_s']}s"
        )
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
