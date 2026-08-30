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

- **Primary:** survivor kill rate = (survivors killed) / (survivors available),
  before vs after. Scoped to exactly the mutants a suite was blind to before
  the intervention — the thing the tool actually claims to do.
- **Secondary:** mean kill score across targets, before vs after.
- **Also:** line coverage delta before vs after (expected to move much less
  than kill score for the baseline arm — this is the "coverage lies"
  evidence); tokens per target; wall-clock per target; and, agent arm only,
  waste rate = tests discarded by the kill gate — a signal of how often the
  model writes vacuous tests.

**Why survivor kill rate is primary and mean kill score is secondary, not the
reverse:** mean kill score is dominated by whichever module happens to have
the most mutants, and a target already at 0.97 has almost no headroom left to
move no matter how good an intervention is. Averaging that together with a
target starting at 0.27 understates or overstates the effect depending on
which targets happen to be in the mix that run. Survivor kill rate is scoped
to exactly the population the tool is meant to act on.

This ordering was decided and committed before either baseline arm was run,
so it could not have been picked after seeing a result it needed to flatter.

### Kill outcome breakdown

`runner.py` counts three outcomes as a kill: the suite actually failed
(`killed`), the suite hung and was cut off (`timeout`), or the mutant broke
collection entirely (`error`). All three count toward kill score and
survivor kill rate — a mutation the suite hung on or couldn't even import is
still a mutation the suite noticed, and treating it as a survivor would be
wrong.

They are not equally strong evidence, though. `killed` means some test's
assertion caught a real behavioral difference. `error` usually means the
mutant produced something that doesn't even import — a much weaker signal
about test quality than an assertion actually firing. `timeout` sits in
between and should be rare in this eval set (no target's mutation surface
involves real concurrency or unbounded loops).

Every report of kill score or survivor kill rate must show the
killed/timeout/error breakdown alongside it, as counts and as a fraction of
total mutants. If `error` accounts for more than half of a target's kills,
say so explicitly — do not let a healthy-looking aggregate hide that most of
it came from mutants that never ran a single assertion.

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
