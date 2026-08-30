# killcheck — project context

## What this is

An agentic workflow that hardens test suites. It finds mutations of the source
that the existing tests fail to detect, writes tests that detect them, and
verifies each new test against a hard gate before keeping it.

Submission for the micro1 Frontier Engineering Challenge 2026.

## The thesis

Line coverage is the industry's default test-quality signal and it is gameable.
AI-generated tests are especially good at gaming it: they execute lines without
asserting anything that would fail if the behaviour were wrong. Mutation testing
measures the thing that actually matters, but it has never gone mainstream
because it hands you a wall of surviving mutants and no path to fixing them.
The agent closes that loop.

## Non-negotiable invariants

These define the experiment. Do not change them to make results look better.

1. **The kill gate.** A generated test is kept only if it BOTH
   (a) passes on clean source, and (b) fails on the specific mutant it targets.
   Anything else is discarded. No exceptions, no "close enough".
2. **The agent never sees the eval set's expected results.** It sees source,
   existing tests, and the mutant diff. Nothing else.
3. **Same cases, same budget, both arms.** Baseline and agent run on identical
   targets with the same model and the same max token budget. Any resource
   difference gets stated explicitly in the report.
4. **Ground truth comes from `runner.py`, never from a model.** No LLM judge
   anywhere in the primary metric.
5. **`engine.py` and `runner.py` are frozen once the baseline has been run.**
   Changing the measuring instrument mid-experiment invalidates the comparison.
   If a bug forces a change, re-run both arms and note it in the changelog.

## Metrics

- **Primary:** kill score = killed / total_mutants, before vs after.
- **Secondary:** line coverage before vs after (expected to move much less than
  kill score for the baseline arm — this is the "coverage lies" evidence).
- **Cost:** tokens and wall-clock per target.
- **Waste rate:** generated tests discarded by the kill gate. Agent-only metric;
  a useful signal about how often the model writes vacuous tests.

## Architecture

```
targets.json          eval set: 12 (project, module, test command) cases
killcheck/engine.py   AST mutation operators -> deterministic Mutant list   [FROZEN]
killcheck/runner.py   isolated execution, kill/survive ground truth         [FROZEN]
killcheck/agent.py    the loop: survivor -> context -> test -> gate -> keep
killcheck/baseline.py single-prompt arm: "write more tests for this file"
killcheck/report.py   results table, markdown output
trajectories/         one JSONL per run, every turn appended live
```

## Agent loop contract

For each surviving mutant, in order:

1. Build context: the module source, the existing test file, the mutant diff
   (original line vs mutated line), and the names of tests already written this
   run so it does not duplicate them.
2. Ask for ONE test function. Constrain: must be deterministic, must not use
   network or wall-clock time, must assert on behaviour rather than on
   implementation internals.
3. Gate it. Run against clean source (must pass) then against the mutant
   (must fail). Log both outcomes.
4. On failure, retry ONCE with the actual pytest output appended to the context.
   Feeding back the real failure output is the highest-leverage single change in
   this system — verify that claim in the changelog with the experiment.
5. Keep or discard. Append the decision to the trajectory log.

Batching multiple survivors into one call is worth trying and is expected to
degrade quality. If it does, keep it in the changelog as a removed experiment
rather than deleting it silently — the brief explicitly asks for one.

## Eval set rules

12 targets from permissively licensed public repos (MIT/Apache/BSD only), plus
the local fixture. Requirements per target:

- Test suite passes on clean code in under ~20 seconds.
- Module has 15–60 mutants. Fewer is not informative; more is slow.
- At least one target must be a genuinely hard case — async, I/O, or heavy
  mocking — where naive test generation produces flaky or vacuous tests.
  Include it even if the agent does badly. Especially if the agent does badly.

Record the exact commit SHA for every target. Reproducibility is 15 points.

## Logging

Append to `trajectories/<run-id>.jsonl` as the run happens, never reconstructed
afterwards. One object per turn:

```json
{"ts": "...", "target": "...", "mutant_id": "M-abc123", "phase": "generate|gate_clean|gate_mutant|decision",
 "prompt_tokens": 0, "completion_tokens": 0, "content": "...", "outcome": "kept|discarded|retry"}
```

## Style

Plain, direct code. Docstrings that say why, not what. No emoji in output. The
report and README should read as if written by an engineer who would put their
name on it — that is an explicit 20-point criterion.
