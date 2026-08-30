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

This section reached its final form after an adversarial review found three
threats to the primary metric while it still had zero results to protect:
mutants that no suite could ever reach regardless of test quality, the
agent being scored on exactly the mutants it was told about (no transfer
evidence), and a kill gate that proves sensitivity to one mutation without
proving the test specifies anything. All three are addressed below, and all
three were fixed before any arm ran.

- **PRIMARY:** survivor kill rate over REACHABLE survivors = (reachable
  survivors killed) / (reachable survivors available), computed over the
  agent's work queue (reachable survivors minus held-out-operator mutants —
  see Held-out operators below), before vs after.
- **REPORT ALSO, every time PRIMARY is reported:**
  - raw SKR (same formula, denominator = ALL survivors, unreachable
    included) — so the curated primary number is never presented without
    the uncurated one next to it.
  - kill-outcome breakdown: killed / timeout / error, as counts and
    fractions of total mutants (see Kill outcome breakdown below).
  - assertion taxonomy per arm (see Assertion taxonomy below).
  - transfer rate on held-out operators (see Held-out operators below).
  - tokens in and out per arm.
  - wall clock per target and per arm.
  - waste rate: tests discarded by the kill gate (agent arm only).

**Why survivor kill rate over reachable survivors, not mean kill score, is
primary:** mean kill score is dominated by whichever module happens to have
the most mutants, and a target already at 0.97 has almost no headroom left
to move no matter how good an intervention is. Averaging that together with
a target starting at 0.27 understates or overstates the effect depending on
which targets happen to be in the mix. Survivor kill rate is scoped to
exactly the population the tool is meant to act on — and "reachable" scopes
it further to mutants a test could conceivably kill at all, so the agent is
never blamed for source lines its own test suite structurally cannot see
(see Denominator below).

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

### Denominator: reachable vs unreachable survivors

A mutant whose mutated line the clean suite never executes cannot be killed
by that suite no matter how good its assertions are — the line simply never
runs, so no assertion is ever positioned to see the difference. Scoring such
a mutant as a "miss" blames the test suite's assertion quality for something
that is actually a coverage gap, which is a different failure mode than the
one this tool targets.

`scripts/verify_targets.py` runs each target's clean suite under `coverage`
and cross-references every surviving mutant's line number against the
executed-lines set for that module. This partitions every mutant into
exactly one of three buckets: `killed` (implies reachable, trivially —
something had to execute the line to observe the failure), `reachable
survivor` (the line executes, but nothing asserts on the resulting
behavior — a real test-quality gap), or `unreachable` (the line never
executes under this suite — not this tool's failure mode). The primary
metric's denominator is reachable survivors only; raw SKR (all survivors)
is always reported alongside it, per the rule above.

**Known limitation of this check:** `coverage`'s line tracer does not
register a bare docstring statement (the sole string literal as the first
statement of a class or function body) as executed, even when the
enclosing class or function is defined and imported — confirmed by direct
test against `voluptuous/error.py`, where importing the module executes
every `class Foo(Invalid): """docstring"""` definition but the docstring
line itself never appears in `executed_lines`. This means `constant`-operator
mutants that target a bare docstring will be classified `unreachable`
regardless of whether the class is ever exercised. This is judged acceptable
rather than worth engineering around: virtually no test asserts on a class's
`__doc__` attribute, so these mutants are close to unkillable in practice
anyway, and excluding them from the denominator is directionally
conservative (it can only remove near-impossible mutants from what the
agent is scored on, never inflate its apparent performance on a mutant it
could plausibly have caught). Two targets — `voluptuous-error` and
`dotenv-variables` — have 0 reachable survivors as of the eval-set
verification run; for `voluptuous-error` this is entirely the docstring
artifact (all 23 survivors are docstring constants), for `dotenv-variables`
it is genuine (the scoped test file never exercises `__repr__`/`__hash__`/
`resolve` on the classes it tests — confirmed by grep, not assumed). Both
targets therefore contribute 0/0 to the primary metric and must be excluded
from the pooled calculation, not silently treated as 0%.

### Held-out operators

`boolop` and `unary_not` survivors are excluded from every arm's work queue
— no arm is ever told about them, gated against them, or asked to target
them — but they are still scored, across every operator, in the full kill
report. This is the anti-circularity control: an arm cannot inflate its
apparent effectiveness by being handed exactly the mutants it will be
graded on. If a held-out survivor dies anyway once an arm's tests are added,
it died because some test written for a different, disclosed mutant
happened to also exercise that code path differently — real evidence the
test generalizes, not evidence the arm gamed its own scoring.

Report this as **transfer rate** = (held-out reachable survivors killed) /
(held-out reachable survivors available), computed the same way as the
primary metric but scoped to the held-out set instead of the work queue.
Report it separately from the primary metric; do not fold it in.

### Assertion taxonomy

The kill gate (pass on clean, fail on mutant) proves a test is sensitive to
one specific syntactic neighbour of the source. It does not prove the test
specifies correct behavior — a test that merely checks `result is not None`
can clear the gate against a mutant that makes a function return `None`,
without ever checking that the *correct* non-None value came back.
`scripts/classify_tests.py` assigns every generated test exactly one
category via a deterministic AST decision procedure (no LLM): `value`,
`mock`, `exception`, `existence`, or `none` — see that file's docstring for
the exact decision rule. Report the distribution per arm, both over all
generated tests and over the subset that clears the gate.

Hypothesis, stated before any test exists to classify: a meaningful share of
gate-passing tests will be `none` or `existence`, meaning the gate selects
differential probes rather than specifications. If the data contradicts
this, say so plainly rather than reframing the hypothesis after the fact.

## Architecture

```
targets.json               eval set: 12 (project, module, test command) cases
killcheck/engine.py        AST mutation operators -> deterministic Mutant list   [FROZEN]
killcheck/runner.py        isolated execution, kill/survive ground truth         [FROZEN]
killcheck/logs.py          shared JSONL append helpers + HELD_OUT_OPERATORS
killcheck/agent.py         the loop: survivor -> context -> test -> gate -> keep
killcheck/baseline.py      arm A (single prompt) and arm B (budget-matched)
killcheck/report.py        results table, markdown output
scripts/verify_targets.py  canary + baseline scoring + reachability + held-out split
scripts/ablate.py          arm C's log -> what gate/retry each individually bought
scripts/classify_tests.py  deterministic AST assertion taxonomy per arm
trajectories/              one JSONL per run, every turn appended live
results/generated_tests.jsonl   every generated test, every arm, every attempt
results/target_verification.json denominator manifest: per-mutant reachability + held-out
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

Two logs, both append-only and written live — never reconstructed after the
fact. `killcheck/logs.py` is the single place both are written from, so the
schema can't drift between arms.

**`trajectories/<run-id>.jsonl`** — one object per model turn:

```json
{"ts": "...", "target": "...", "mutant_id": "M-abc123", "phase": "generate|gate_clean|gate_mutant|decision",
 "prompt_tokens": 0, "completion_tokens": 0, "content": "...", "outcome": "kept|discarded|retry"}
```

**`results/generated_tests.jsonl`** — one object per generated test, from
every arm, every attempt, gated or not. This is what makes
`scripts/ablate.py` possible from a single arm C run with no extra calls,
and what `scripts/classify_tests.py` reads for the assertion taxonomy:

```json
{"arm": "A|B|C", "target": "...", "mutant_id": "M-abc123", "attempt": 1,
 "passed_on_clean": true, "killed_target": false, "test_source": "def test_x(): ...",
 "prompt_tokens": 0, "completion_tokens": 0}
```

`attempt` is always 1 for arms A and B (neither retries); arm C logs both
attempt 1 and, when attempt 1 fails the gate, attempt 2. `passed_on_clean`
and `killed_target` are logged for every arm, not just arm C — arms A and B
need them too to compute their own kill scores, and logging them uniformly
is what lets one objective rule ("gate would keep" = both true) be applied
identically across all three arms for comparison, regardless of which arms
actually enforce it.

## Style

Plain, direct code. Docstrings that say why, not what. No emoji in output. The
report and README should read as if written by an engineer who would put their
name on it — that is an explicit 20-point criterion.
