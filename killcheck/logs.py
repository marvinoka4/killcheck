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
# Anti-circularity now rests on the assertion taxonomy instead (CLAUDE.md).
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
) -> None:
    """Append one row to results/generated_tests.jsonl.

    Schema (see CLAUDE.md's Logging section):
      arm              "A" | "B" | "C"
      target           target name from targets.json
      mutant_id        the specific mutant this test was asked to target
      attempt          1 or 2 (arms A/B never retry, so always 1 there;
                        arm C retries once on gate failure per the agent
                        loop contract)
      passed_on_clean  bool -- did the test pass against unmutated source
      killed_target    bool -- did the test fail against mutant_id's mutant
      test_source      full source of the generated test function
      prompt_tokens    tokens in for the call that produced this test
      completion_tokens tokens out for the call that produced this test

    This is written for EVERY generated test in every arm, gated or not --
    arms A and B have no gate, so "kept" for them means "generated"; arm C's
    kept set is the subset where passed_on_clean and killed_target are both
    true. Logging every attempt regardless of outcome is what lets
    scripts/ablate.py reconstruct what a weaker design (no retry, no gate,
    both) would have kept from a single arm C run, with no extra calls.
    """
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
) -> None:
    """Append one row to trajectories/<run_id>.jsonl. Schema is CLAUDE.md's
    Logging section verbatim; this just adds the timestamp and does the
    append-and-flush."""
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
        },
    )
