"""Shared logging helpers for trajectories/ and results/generated_tests.jsonl.

Not frozen -- engine.py and runner.py are the measuring instrument; this is
just I/O plumbing used by baseline.py, agent.py, and the ablation/taxonomy
scripts that read what they wrote. Kept tiny and dependency-free on purpose:
every arm writes through the same function so the schema can't drift between
arms, which is the whole point of doing this before any arm runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ABANDONED (Task 2b) -- kept only as a historical record, no longer used to
# filter any arm's work queue. This was meant to withhold boolop/unary_not
# survivors as a held-out transfer-rate control. Killed by the numbers: see
# CHANGELOG.md's "Abandoned: holdout transfer control" entry -- across all 12
# targets, the held-out-and-reachable population was too small (single
# digits) to support a rate at all, before or after widening reachability.
# Anti-circularity now rests on the assertion taxonomy instead (METHODOLOGY.md).
# Nothing in this codebase reads this constant for scoring; it is not
# imported by verify_targets.py or ablate.py.
HELD_OUT_OPERATORS = frozenset({"boolop", "unary_not"})


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON object as a line. Opens in append mode and flushes
    immediately -- this is the "log live, never reconstruct" mechanism, so a
    crash mid-run loses at most the record in flight, never anything already
    written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


# CHECK C (unit metadata) -- see CHANGELOG.md's "The scorer itself was
# never checked" entry. Bug 4 was a unit mismatch: the taxonomy classifier
# documented "one test function's source" as its input contract and was
# handed a whole multi-test batch instead -- both sides were individually
# correct code, and nothing in the data itself recorded which unit a given
# row actually was, so the mismatch was invisible until someone checked by
# hand. `test_source` in a generated_tests.jsonl row means a genuinely
# different thing depending on which arm wrote it: arm C logs one row per
# attempt, and test_source is exactly one test function's source. Arms A
# and B log one row per arm-target (their tests are scored as a whole
# batch, never individually -- see METHODOLOGY.md's Clean-pass failures
# section), and test_source is the WHOLE accumulated batch, potentially
# many test functions concatenated. These two constants name that
# difference explicitly so a row states which one it is instead of a
# reader having to infer it from which arm wrote it.
UNIT_SINGLE_TEST_FUNCTION = "single_test_function"  # arm C: test_source is exactly one test
UNIT_TEST_BATCH = "test_batch"  # arms A/B: test_source is a whole accumulated batch


def log_generated_test(
    *,
    results_dir: Path,
    arm: str,
    target: str,
    mutant_id: str,
    attempt: int,
    passed_on_clean: bool,
    killed_target: bool,
    test_source: str,
    prompt_tokens: int,
    completion_tokens: int,
    unit: str,
) -> None:
    """Append one row to results/generated_tests.jsonl.

    Schema (see METHODOLOGY.md's Logging section):
      arm              "A" | "B" | "C"
      target           target name from targets.json
      mutant_id        the specific mutant this test was asked to target
      attempt          1 or 2 (arms A/B never retry, so always 1 there;
                        arm C retries once on gate failure per the agent
                        loop contract)
      passed_on_clean  bool -- did the test pass against unmutated source
      killed_target    bool -- did the test fail against mutant_id's mutant
      test_source      full source -- see `unit` for what it actually contains
      prompt_tokens    tokens in for the call that produced this test
      completion_tokens tokens out for the call that produced this test
      unit             UNIT_SINGLE_TEST_FUNCTION or UNIT_TEST_BATCH -- what
                        test_source actually is. Required, no default: every
                        caller must state it, not inherit whatever the last
                        caller happened to mean. A consumer must assert the
                        unit it expects before treating test_source as
                        containing what it assumes (see
                        scripts/classify_tests.py and CHECK C's meta-test in
                        scripts/test_scorer_checks.py).

    This is written for EVERY generated test in every arm, gated or not --
    arms A and B have no gate, so "kept" for them means "generated"; arm C's
    kept set is the subset where passed_on_clean and killed_target are both
    true. Logging every attempt regardless of outcome is what lets
    scripts/ablate.py reconstruct what a weaker design (no retry, no gate,
    both) would have kept from a single arm C run, with no extra calls.
    """
    if unit not in (UNIT_SINGLE_TEST_FUNCTION, UNIT_TEST_BATCH):
        raise ValueError(f"log_generated_test: unrecognized unit {unit!r}")
    append_jsonl(
        results_dir / "generated_tests.jsonl",
        {
            "arm": arm,
            "target": target,
            "mutant_id": mutant_id,
            "attempt": attempt,
            "passed_on_clean": passed_on_clean,
            "killed_target": killed_target,
            "test_source": test_source,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "unit": unit,
        },
    )


def log_trajectory(
    *,
    trajectories_dir: Path,
    run_id: str,
    target: str,
    mutant_id: str,
    phase: str,
    prompt_tokens: int,
    completion_tokens: int,
    content: str,
    outcome: str,
    truncated: bool = False,
) -> None:
    """Append one row to trajectories/<run_id>.jsonl. Schema is METHODOLOGY.md's
    Logging section verbatim; this just adds the timestamp and does the
    append-and-flush.

    `truncated` is true when a "generate" call's completion_tokens hit the
    call's max_tokens ceiling -- the response was cut off, not finished.
    Defaults false since it only applies to generate-phase calls; gate and
    decision phases don't call the model and never truncate."""
    import time

    append_jsonl(
        trajectories_dir / f"{run_id}.jsonl",
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "target": target,
            "mutant_id": mutant_id,
            "phase": phase,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "content": content,
            "outcome": outcome,
            "truncated": truncated,
        },
    )
