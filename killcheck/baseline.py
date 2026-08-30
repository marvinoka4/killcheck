"""Baseline arms A and B.

Arm A -- single prompt. One call per target. Module source plus existing test
file, asked for additional tests. No mutation information, no gate, no retry.
This is the baseline the challenge brief names ("one direct prompt with basic
instructions"). It is written to be a fair prompt, not a strawman.

Arm B -- budget matched. The same number of calls arm C will spend on that
target (its reachable survivor count, capped at CALL_CAP), same model, same
max tokens. Each call asks for one more test and is told which tests already
exist so it does not duplicate. Still no mutation context, no gate, no retry.

Arm B exists to answer the obvious objection: if C wins, was it the design or
just the compute? A->B isolates what budget buys. B->C isolates what the
design buys.

Scoring is BATCH, never incremental: the arm generates its full test set, the
set is appended to the suite once, and the whole thing is scored against every
reachable survivor in one pass. Incremental scoring would give an arm
compounding credit as its suite grows, which is a confound.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from killcheck.engine import generate_mutants
from killcheck.logs import log_generated_test, log_trajectory
from killcheck.runner import Target, _evaluate_one, _run

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2000
CALL_CAP = 25
REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Model call
# ---------------------------------------------------------------------------


def _client():
    from anthropic import Anthropic
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env")
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY not set. See .env.example.")
    return Anthropic(api_key=key)


def call_model(client, prompt: str) -> tuple[str, int, int]:
    """One call. Returns (text, prompt_tokens, completion_tokens).

    Retries only on transport/rate-limit errors, which is not the arm-level
    retry the gate uses -- arms A and B never retry on content.
    """
    last = None
    for attempt in range(4):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            return text, resp.usage.input_tokens, resp.usage.output_tokens
        except Exception as exc:  # transport, rate limit, overload
            last = exc
            time.sleep(2**attempt)
    raise RuntimeError(f"model call failed after 4 attempts: {last}")


# ---------------------------------------------------------------------------
# Extracting test code from a model response
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:python)?\n(.*?)```", re.DOTALL)


def extract_code(text: str) -> str:
    """Pull python out of fences; fall back to the raw text.

    Deliberately permissive. A response we cannot parse is recorded as a test
    that fails on clean source, not silently dropped -- an arm that emits
    unusable output should be penalised for it, not rescued.
    """
    blocks = _FENCE.findall(text)
    if blocks:
        return "\n\n".join(b.strip() for b in blocks)
    return text.strip()


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

RULES = """
Requirements for every test you write:
- Standalone pytest test functions. Include any imports they need.
- Deterministic. No network, no wall-clock sleeps, no dependence on
  execution order or on other tests.
- Assert on observable behaviour, not on private implementation details.
- Do not modify or restate the existing tests. Emit only new ones.
Return only the test code in a single ```python block, no commentary.
"""

ARM_A = """You are adding tests to an existing Python test suite.

Module under test ({module_path}):
```python
{module_source}
```

Its existing tests:
```python
{test_source}
```
{RULES}
Write additional tests that improve this suite's ability to catch bugs in the
module. Write as many as you think are warranted."""

ARM_B = """You are adding tests to an existing Python test suite.

Module under test ({module_path}):
```python
{module_source}
```

Its existing tests:
```python
{test_source}
```
{already}{RULES}
Write exactly ONE additional test that improves this suite's ability to catch
bugs in the module."""


# ---------------------------------------------------------------------------
# Target loading
# ---------------------------------------------------------------------------


def load_targets() -> list[dict]:
    return json.loads((REPO / "targets.json").read_text())


def load_verification() -> dict[str, dict]:
    path = REPO / "results" / "target_verification.json"
    if not path.exists():
        sys.exit(
            "results/target_verification.json missing. Run "
            "scripts/verify_targets.py first -- the arms need its reachability "
            "buckets and must not recompute them independently."
        )
    return {t["name"]: t for t in json.loads(path.read_text())}


def as_target(spec: dict) -> Target:
    return Target(
        name=spec["name"],
        project_root=(REPO / spec["project_root"]).resolve(),
        module_path=Path(spec["module_path"]),
        test_command=spec["test_command"],
    )


def reachable_survivor_ids(v: dict) -> list[str]:
    return [
        m["mutant_id"]
        for m in v["mutants"]
        if m["outcome"] == "survived" and m.get("reachable") is True
    ]


def existing_test_source(spec: dict, target: Target) -> tuple[Path, str]:
    """The test file the arm is shown, and where generated tests get appended.

    Heuristic, in order: (1) a candidate whose name contains the module's own
    stem (e.g. `card.py` -> `test_card.py`); (2) failing that, a candidate
    whose name contains the module's immediate parent directory name (e.g.
    `tempfile/temptypes.py` -> `test_tempfile.py`, since a module's sibling
    tests are often filed under the package subdirectory rather than the
    module's own name); (3) failing both, the largest test file in scope, on
    the theory that it's the one a human would read first.

    Tier 3 is a guess, not a match, and a bad guess here silently degrades
    whichever arm sees it -- it happened once, on aiofiles-temptypes (picked
    test_os.py, a module temptypes.py has nothing to do with; see
    CHANGELOG.md). It now announces itself instead of requiring a hand audit
    to find.
    """
    root = target.project_root
    stem = target.module_path.stem
    parent = target.module_path.parent.name
    candidates: list[Path] = []
    for arg in spec["test_command"]:
        if arg.startswith("-"):
            continue
        p = root / arg
        if p.is_file() and p.name.endswith(".py"):
            candidates.append(p)
        elif p.is_dir():
            candidates.extend(sorted(p.rglob("test*.py")))
    if not candidates:
        candidates = sorted(root.rglob("test*.py"))
    if not candidates:
        raise RuntimeError(f"{target.name}: no test file found")

    matched = [c for c in candidates if stem in c.name]
    tier = "stem"
    if not matched and parent:
        matched = [c for c in candidates if parent in c.name]
        tier = "parent-dir"
    if not matched:
        matched = candidates
        tier = "largest-file fallback"

    chosen = max(matched, key=lambda p: p.stat().st_size)
    if tier == "largest-file fallback" and len(candidates) > 1:
        # Only worth a warning when there was an actual choice to get wrong --
        # a test_command that names exactly one file leaves nothing to guess
        # among, so "fallback" there is just the correct, only file.
        print(
            f"  WARNING [{target.name}] existing_test_source: no filename "
            f"matched module stem {stem!r} or parent dir {parent!r} among "
            f"{len(candidates)} candidates -- guessed {chosen.relative_to(root)} "
            f"by size alone. This is a degraded pick; verify it by hand.",
            flush=True,
        )
    return chosen, chosen.read_text()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_with_added_tests(
    target: Target,
    test_file: Path,
    added: str,
    survivor_ids: list[str],
    timeout: int = 120,
) -> dict:
    """Batch scoring. Append `added` once, then score every survivor.

    Returns clean-pass status and per-mutant kill results. Runs entirely in a
    tempdir copy; the real checkout is never written to.
    """
    rel_test = test_file.relative_to(target.project_root)

    with tempfile.TemporaryDirectory(prefix="killcheck-arm-") as tmp:
        work = Path(tmp) / "project"
        shutil.copytree(
            target.project_root,
            work,
            ignore=shutil.ignore_patterns(
                "__pycache__", ".git", ".pytest_cache", "*.pyc", ".venv"
            ),
        )
        augmented = work / rel_test
        augmented.write_text(augmented.read_text() + "\n\n" + added + "\n")

        code, output = _run(target.test_command, work, timeout)
        clean_pass = code == 0
        if not clean_pass:
            return {
                "clean_pass": False,
                "clean_output": output[-2000:],
                "kills": {},
            }

        # Score each survivor against the augmented suite. workers=1 for
        # determinism (see the concurrency bug in the README).
        source = (target.project_root / target.module_path).read_text()
        wanted = set(survivor_ids)
        mutants = [m for m in generate_mutants(source, str(target.module_path))
                   if m.id in wanted]

        augmented_target = Target(
            name=target.name,
            project_root=work,
            module_path=target.module_path,
            test_command=target.test_command,
        )
        kills = {}
        for m in mutants:
            r = _evaluate_one(augmented_target, m, timeout)
            kills[m.id] = r.outcome

        return {"clean_pass": True, "clean_output": "", "kills": kills}


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------


def run_arm(arm: str, spec: dict, verification: dict, client, run_id: str) -> dict:
    target = as_target(spec)
    survivors = reachable_survivor_ids(verification)
    test_file, test_source = existing_test_source(spec, target)
    module_source = (target.project_root / target.module_path).read_text()
    results_dir = REPO / "results"
    traj_dir = REPO / "trajectories"

    started = time.monotonic()
    tokens_in = tokens_out = 0
    pieces: list[str] = []

    if not survivors:
        return {
            "arm": arm,
            "target": target.name,
            "reachable_survivors": 0,
            "skipped": "no reachable survivors",
        }

    n_calls = 1 if arm == "A" else min(len(survivors), CALL_CAP)

    for i in range(n_calls):
        if arm == "A":
            prompt = ARM_A.format(
                module_path=target.module_path,
                module_source=module_source,
                test_source=test_source,
                RULES=RULES,
            )
        else:
            already = ""
            if pieces:
                names = re.findall(r"def (test_\w+)", "\n".join(pieces))
                if names:
                    already = (
                        "\nTests already added in this session (do not "
                        "duplicate): " + ", ".join(names) + "\n"
                    )
            prompt = ARM_B.format(
                module_path=target.module_path,
                module_source=module_source,
                test_source=test_source,
                already=already,
                RULES=RULES,
            )

        text, pin, pout = call_model(client, prompt)
        tokens_in += pin
        tokens_out += pout
        code = extract_code(text)
        pieces.append(code)

        log_trajectory(
            trajectories_dir=traj_dir,
            run_id=run_id,
            target=target.name,
            mutant_id="",
            phase="generate",
            prompt_tokens=pin,
            completion_tokens=pout,
            content=code,
            outcome="generated",
        )
        print(f"  [{arm}] {target.name} call {i+1}/{n_calls} "
              f"({pin}+{pout} tok)", flush=True)

    added = "\n\n".join(pieces)
    scored = score_with_added_tests(target, test_file, added, survivors)

    killed = [mid for mid, out in scored["kills"].items() if out != "survived"]

    # One log row per arm-target. Arms A and B have no gate and no per-mutant
    # targeting, so mutant_id is empty and attempt is always 1; killed_target
    # records whether this arm's test set killed anything at all.
    log_generated_test(
        results_dir=results_dir,
        arm=arm,
        target=target.name,
        mutant_id="",
        attempt=1,
        passed_on_clean=scored["clean_pass"],
        killed_target=bool(killed),
        test_source=added,
        prompt_tokens=tokens_in,
        completion_tokens=tokens_out,
    )

    return {
        "arm": arm,
        "target": target.name,
        "reachable_survivors": len(survivors),
        "calls": n_calls,
        "clean_pass": scored["clean_pass"],
        "clean_output": scored["clean_output"],
        "killed_ids": killed,
        "killed": len(killed),
        "kills_by_outcome": scored["kills"],
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "wall_clock_s": round(time.monotonic() - started, 1),
        "test_file": str(test_file.relative_to(target.project_root)),
        "generated_tests": added,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=["A", "B"], required=True)
    ap.add_argument("--target", help="run one target only")
    args = ap.parse_args()

    specs = load_targets()
    if args.target:
        specs = [s for s in specs if s["name"] == args.target]
        if not specs:
            sys.exit(f"no target named {args.target}")

    verification = load_verification()
    client = _client()
    run_id = f"arm{args.arm}-{time.strftime('%Y%m%d-%H%M%S')}"

    out = []
    for spec in specs:
        v = verification.get(spec["name"])
        if v is None:
            print(f"  skip {spec['name']}: not in verification", flush=True)
            continue
        print(f"[{args.arm}] {spec['name']}", flush=True)
        out.append(run_arm(args.arm, spec, v, client, run_id))

    scored = [r for r in out if not r.get("skipped")]
    denom = sum(r["reachable_survivors"] for r in scored)
    num = sum(r["killed"] for r in scored)

    summary = {
        "arm": args.arm,
        "run_id": run_id,
        "model": MODEL,
        "call_cap": CALL_CAP,
        "pooled_reachable_survivors": denom,
        "pooled_killed": num,
        "pooled_skr": round(num / denom, 4) if denom else None,
        "tokens_in": sum(r["tokens_in"] for r in scored),
        "tokens_out": sum(r["tokens_out"] for r in scored),
        "clean_pass_failures": [r["target"] for r in scored
                                if not r["clean_pass"]],
        "targets": out,
    }

    path = REPO / "results" / f"baseline_arm_{args.arm.lower()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2))

    print(f"\narm {args.arm}: killed {num} of {denom} reachable survivors "
          f"(pooled SKR {summary['pooled_skr']})")
    print(f"tokens in={summary['tokens_in']} out={summary['tokens_out']}")
    if summary["clean_pass_failures"]:
        print(f"CLEAN-PASS FAILURES: {summary['clean_pass_failures']}")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()