"""Print one mutant's complete path, end to end, for a demo video.

Usage: python3 scripts/show_mutant.py M-583a4d4a

Reads results/target_verification.json (mutant record: operator, line,
reachability), targets.json + killcheck.engine (regenerates the exact
before/after line text -- not stored in target_verification.json, so this
recomputes it deterministically from the same frozen mutation engine rather
than duplicating it into a third file), results/generated_tests.jsonl (the
kept test's source, attempt number, gate outcome), trajectories/ (confirms
the logged decision), and results/agent_arm_c.json (the one field genuinely
not captured in either of the two logs above: the kill's exception type --
see CHANGELOG's arm C entries for why that split exists).

No JSON dumps. This is meant to be read on screen, not grepped.
"""
from __future__ import annotations

import ast
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from killcheck.agent import build_prompt
from killcheck.baseline import as_target, load_targets
from killcheck.engine import generate_mutants

WIDE = "=" * 78


def heading(title: str) -> None:
    print()
    pad = max(3, 78 - len(title) - 4)
    print(f"-- {title} " + "-" * pad)


def find_mutant_record(mutant_id: str) -> tuple[str, dict]:
    verification = json.loads((ROOT / "results" / "target_verification.json").read_text())
    for t in verification:
        for m in t["mutants"]:
            if m["mutant_id"] == mutant_id:
                return t["name"], m
    sys.exit(f"{mutant_id} not found in results/target_verification.json")


def find_kept_test(target: str, mutant_id: str) -> dict | None:
    """The generated_tests.jsonl row where this mutant's draft was kept
    (passed_on_clean and killed_target both true) -- there is at most one,
    by construction of the gate."""
    path = ROOT / "results" / "generated_tests.jsonl"
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    candidates = [
        r for r in rows
        if r["arm"] == "C" and r["target"] == target and r["mutant_id"] == mutant_id
    ]
    for r in candidates:
        if r["passed_on_clean"] and r["killed_target"]:
            return r
    return None


def find_prior_attempts(target: str, mutant_id: str, kept_attempt: int) -> int:
    """How many attempts before the kept one -- 0 if it was kept on attempt 1."""
    path = ROOT / "results" / "generated_tests.jsonl"
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    return sum(
        1 for r in rows
        if r["arm"] == "C" and r["target"] == target and r["mutant_id"] == mutant_id
        and r["attempt"] < kept_attempt
    )


def find_exception_type(target: str, mutant_id: str, attempt: int) -> str | None:
    """The one field not in either raw log -- see module docstring."""
    path = ROOT / "results" / "agent_arm_c.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    t = next((x for x in data if x["target"] == target), None)
    if t is None:
        return None
    draft = next((d for d in t["per_mutant_drafts"] if d["mutant_id"] == mutant_id), None)
    if draft is None:
        return None
    a = next((x for x in draft["attempts"] if x["attempt"] == attempt), None)
    return a["plugin_outcome"] if a else None


def find_decision(target: str, mutant_id: str) -> str | None:
    """Confirms the logged decision phase from trajectories/, rather than
    inferring it from the gate booleans alone. Rows are append-only and
    chronological, so the last decision row for this mutant (there may be
    an earlier "retry" decision before a final "kept"/"discarded") is the
    one that matters."""
    last = None
    for path in sorted(glob.glob(str(ROOT / "trajectories" / "armC-*.jsonl"))):
        for line in Path(path).read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r["target"] == target and r["mutant_id"] == mutant_id and r["phase"] == "decision":
                last = r["outcome"]
    return last


def mutation_diff_text(module_path: str, operator: str, lineno: int, original_line: str, mutated_line: str) -> str:
    """Reuses the exact prompt template arm C sends, extracting just the
    operator/line/before/after block -- not the whole module/test-file
    context, and not the surrounding sentence, which section 1 already
    covers."""

    class _Stub:
        pass

    stub = _Stub()
    stub.operator = operator
    stub.lineno = lineno
    stub.original_line = original_line
    stub.mutated_line = mutated_line
    full_prompt = build_prompt(module_path, "", "", stub, [])
    start = full_prompt.index("operator:")
    end = full_prompt.index("Write exactly ONE test function")
    return full_prompt[start:end].rstrip()


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python3 scripts/show_mutant.py <mutant_id>")
    mutant_id = sys.argv[1]

    target_name, record = find_mutant_record(mutant_id)
    specs = {s["name"]: s for s in load_targets()}
    spec = specs[target_name]
    target = as_target(spec)
    module_source = (target.project_root / target.module_path).read_text()
    mutants = generate_mutants(module_source, str(target.module_path))
    mutant = next(m for m in mutants if m.id == mutant_id)

    print(WIDE)
    print(f"MUTANT {mutant_id}  --  {target_name}")
    print(WIDE)

    heading("1. THE MUTANT")
    print(f"file: {target.module_path}   line: {mutant.lineno}   operator: {mutant.operator}")
    print(f"  before:  {mutant.original_line.strip()}")
    print(f"  after:   {mutant.mutated_line.strip()}")

    heading("2. STATUS BEFORE")
    print(f"survived the existing suite: {record['outcome'] == 'survived'}   "
          f"reachable: {record['reachable']}")

    heading("3. WHAT THE AGENT WAS SHOWN  (mutant-diff portion of the prompt)")
    print(mutation_diff_text(
        str(target.module_path), mutant.operator, mutant.lineno,
        mutant.original_line, mutant.mutated_line,
    ))

    kept = find_kept_test(target_name, mutant_id)
    if kept is None:
        heading("RESULT")
        print("No kept test found for this mutant -- it was discarded.")
        print(WIDE)
        return

    prior = find_prior_attempts(target_name, mutant_id, kept["attempt"])
    retried = ", kept on retry" if prior else ""

    heading(f"4. THE TEST IT WROTE  (attempt {kept['attempt']}{retried})")
    if prior:
        print(f"(attempt 1 did not pass the gate; the model retried with the real pytest output)")
        print()
    print(kept["test_source"].rstrip())

    exc_type = find_exception_type(target_name, mutant_id, kept["attempt"])
    heading("5. THE GATE")
    print(f"passed_on_clean: {kept['passed_on_clean']}   "
          f"killed_target: {kept['killed_target']}   "
          f"kill type: {exc_type or 'n/a'}")

    decision = find_decision(target_name, mutant_id)
    heading("6. DECISION")
    print('KEPT' if decision == 'kept' else (decision or 'unknown').upper())

    print(WIDE)


if __name__ == "__main__":
    main()
