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
import ast
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
from killcheck.invariants import assert_ids_subset, assert_no_duplicates, assert_pooled_conservation
from killcheck.logs import log_generated_test, log_trajectory, UNIT_TEST_BATCH
from killcheck.runner import Target, _evaluate_one, _run

MODEL = "claude-sonnet-4-6"
# 8000, not the original 2000: recorded in METHODOLOGY.md as the per-call
# max_tokens for all three arms (invariant 3 requires the same budget across
# arms, so agent.py must match this when it exists). 2000 was a placeholder
# that turned out to be wrong -- Arm A's prompt asks for "as many tests as
# warranted" and then capped the response below what that takes, truncating
# mid-fence. See CHANGELOG.md.
MAX_TOKENS = 8000
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


def call_model(client, prompt: str) -> tuple[str, int, int, bool]:
    """One call. Returns (text, prompt_tokens, completion_tokens, truncated).

    `truncated` is true when completion_tokens == MAX_TOKENS -- the response
    was cut off by the ceiling, not finished on its own. This is a real
    failure mode expected to recur in arm C across its ~53 generate calls,
    so it is surfaced as its own signal rather than left to show up only as
    a mysterious clean-pass failure downstream.

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
            pout = resp.usage.output_tokens
            return text, resp.usage.input_tokens, pout, pout == MAX_TOKENS
        except Exception as exc:  # transport, rate limit, overload
            last = exc
            time.sleep(2**attempt)
    raise RuntimeError(f"model call failed after 4 attempts: {last}")


# ---------------------------------------------------------------------------
# Extracting test code from a model response
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:python)?\n(.*?)```", re.DOTALL)
_OPEN_FENCE = re.compile(r"```(?:python)?\n")


def _salvage_parseable_prefix(blob: str) -> str:
    """Strip trailing lines one at a time until `blob` parses as valid
    Python, or nothing is left.

    A response truncated mid-statement should cost that one incomplete
    statement, not every complete test that came before it in the same
    call -- which is what injecting the raw, unparseable tail used to do:
    one bad line turned "the file doesn't parse" and destroyed the whole
    batch, complete tests included.
    """
    lines = blob.rstrip().splitlines()
    while lines:
        candidate = "\n".join(lines)
        try:
            ast.parse(candidate)
            return candidate
        except SyntaxError:
            lines.pop()
    return ""


def extract_code(text: str) -> str:
    """Pull python out of fences, salvaging what's parseable from a response
    truncated mid-fence rather than discarding it or injecting it raw.

    Closed ```...``` blocks are taken whole -- these are known-complete.
    Anything after the last closed block (or the whole response, if there
    were no closed blocks) that still has an unmatched opening fence is a
    truncated tail: everything after that opening fence is AST-salvaged via
    `_salvage_parseable_prefix` rather than kept as raw text. A response with
    no fences at all gets the same salvage treatment directly, so a plain
    (unfenced) truncated response is handled the same way instead of being
    injected unvalidated.

    If nothing parses, returns "" -- an empty contribution from this call,
    not a corrupted file. The caller records the call's `truncated` status
    separately (see call_model), so this failure mode is visible as itself
    rather than only showing up as an unexplained clean-pass failure.
    """
    matches = list(_FENCE.finditer(text))
    pieces = [m.group(1).strip() for m in matches]

    tail = text[matches[-1].end():] if matches else text
    open_match = _OPEN_FENCE.search(tail)
    if open_match:
        salvaged = _salvage_parseable_prefix(tail[open_match.end():])
        if salvaged:
            pieces.append(salvaged)
    elif not matches:
        salvaged = _salvage_parseable_prefix(text)
        if salvaged:
            pieces.append(salvaged)

    return "\n\n".join(pieces)


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


def _split_future_import_lines(source: str) -> tuple[list[str], str]:
    """Return (future_import_lines, source_with_those_lines_removed).

    Lines are extracted by their original source span (node.lineno ..
    end_lineno), not reformatted -- only the __future__ lines themselves
    move, everything else keeps its exact original text.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], source
    lines = source.splitlines()
    remove_ranges = [
        (node.lineno, node.end_lineno)
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "__future__"
    ]
    if not remove_ranges:
        return [], source
    future_lines: list[str] = []
    for start, end in remove_ranges:
        future_lines.extend(lines[start - 1:end])
    remove_set = {i for start, end in remove_ranges for i in range(start, end + 1)}
    kept = [line for i, line in enumerate(lines, start=1) if i not in remove_set]
    return future_lines, "\n".join(kept)


def _future_import_insertion_point(existing_source: str) -> int:
    """Line index (0-based, into existing_source.splitlines()) right after
    any leading module docstring and any leading __future__ imports --
    i.e. the one legal place to insert more __future__ imports without
    disturbing what's already correctly placed in `existing_source`."""
    try:
        tree = ast.parse(existing_source)
    except SyntaxError:
        return 0
    body = tree.body
    idx = 0
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        idx = 1
    while idx < len(body) and isinstance(body[idx], ast.ImportFrom) and body[idx].module == "__future__":
        idx += 1
    return body[idx - 1].end_lineno if idx else 0


def hoist_future_imports(existing_source: str, added_source: str) -> str:
    """Combine existing + added file content, hoisting any `from __future__
    import ...` statement in `added_source` to right after
    `existing_source`'s own leading docstring/future-imports, instead of
    leaving it wherever it fell in the appended content.

    Needed because appending generated tests after existing content can
    never satisfy Python's requirement that __future__ imports be a file's
    first statements, regardless of whether the appended tests are
    otherwise correct. Confirmed necessary on natsort-ns-enum for both arm A
    and arm B: neither had a single bad test, and 40 (arm A) / 2 (arm B)
    individually-correct tests were invalidated by this alone, because the
    model included its own (redundant -- the existing file already has one)
    `from __future__ import annotations`. See CHANGELOG.md.
    """
    future_lines, added_rest = _split_future_import_lines(added_source)
    if not future_lines:
        return existing_source + "\n\n" + added_source
    existing_lines = existing_source.splitlines()
    insert_at = _future_import_insertion_point(existing_source)
    new_existing = "\n".join(existing_lines[:insert_at] + future_lines + existing_lines[insert_at:])
    return new_existing + "\n\n" + added_rest


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
        augmented.write_text(hoist_future_imports(augmented.read_text(), added) + "\n")

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
    truncated_calls = 0
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

        text, pin, pout, truncated = call_model(client, prompt)
        tokens_in += pin
        tokens_out += pout
        if truncated:
            truncated_calls += 1
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
            truncated=truncated,
        )
        flag = " TRUNCATED" if truncated else ""
        print(f"  [{arm}] {target.name} call {i+1}/{n_calls} "
              f"({pin}+{pout} tok){flag}", flush=True)

    added = "\n\n".join(p for p in pieces if p)
    scored = score_with_added_tests(target, test_file, added, survivors)

    killed = [mid for mid, out in scored["kills"].items() if out != "survived"]

    # CHECK B (conservation invariants): killed_ids must be a subset of the
    # survivors this call was actually scoring -- a stray id here would mean
    # a mutant from a different target or a stale engine.py run leaked in.
    # If the suite passed clean, every survivor handed to score_with_added_
    # tests must appear exactly once in its kills dict -- neither dropped
    # nor scored twice would be visible any other way than this.
    assert_no_duplicates(killed, context=f"arm {arm}/{target.name} killed_ids")
    assert_ids_subset(set(killed), set(survivors), context=f"arm {arm}/{target.name} killed_ids")
    if scored["clean_pass"]:
        assert len(scored["kills"]) == len(survivors), (
            f"arm {arm}/{target.name}: scored {len(scored['kills'])} mutants but was handed "
            f"{len(survivors)} survivors -- work queue did not conserve"
        )

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
        unit=UNIT_TEST_BATCH,  # the whole accumulated batch, not one test -- see killcheck/logs.py
        prompt_tokens=tokens_in,
        completion_tokens=tokens_out,
    )

    return {
        "arm": arm,
        "target": target.name,
        "reachable_survivors": len(survivors),
        "calls": n_calls,
        "truncated_calls": truncated_calls,
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
    # CHECK B, pooled form -- see scripts/verify_targets.py's identical check
    # for why this matters even though denom/num are already direct sums
    # here: it's a standing guard against a future refactor (an incremental
    # accumulator, a cache) silently breaking the identity METHODOLOGY.md's
    # primary metric is defined by.
    assert_pooled_conservation(denom, [r["reachable_survivors"] for r in scored], f"arm {args.arm} pooled reachable survivors")
    assert_pooled_conservation(num, [r["killed"] for r in scored], f"arm {args.arm} pooled killed")

    total_calls = sum(r["calls"] for r in scored)
    truncated_calls = sum(r["truncated_calls"] for r in scored)

    summary = {
        "arm": args.arm,
        "run_id": run_id,
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "call_cap": CALL_CAP,
        "pooled_reachable_survivors": denom,
        "pooled_killed": num,
        "pooled_skr": round(num / denom, 4) if denom else None,
        "tokens_in": sum(r["tokens_in"] for r in scored),
        "tokens_out": sum(r["tokens_out"] for r in scored),
        "total_calls": total_calls,
        "truncated_calls": truncated_calls,
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
    print(f"truncated calls: {truncated_calls} of {total_calls}")
    if summary["clean_pass_failures"]:
        print(f"CLEAN-PASS FAILURES: {summary['clean_pass_failures']}")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()