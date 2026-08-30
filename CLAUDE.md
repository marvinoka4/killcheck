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

That was the premise going in, and it was partially wrong -- forced by data,
not revised after the fact. Before the agent wrote a single test, the eval
set's 12 targets were checked for how many of their surviving mutants sit on
a line their own test suite even executes (see Denominator below). Pooled
across all 12, only 53 of 455 total mutants (133 of which survive at all)
sit on a line that's reachable by the broadest suite each project's own
maintainers run. Widening every target's test scope 6x-40x did not raise
that number -- see "Widening the suites" below for the full before/after
table. Where this eval set's suites fail, most of the time they fail by
never executing the mutated code at all, not by executing it and failing to
check the result. The agent this project builds only ever operates on the
minority case -- the reachable survivor, where code ran and nothing
asserted on it. That is still a real and worthwhile problem to close the
loop on; it is not the whole problem the opening paragraph implied it was.

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

This section reached its final form after two rounds of adversarial review,
both completed while it still had zero results to protect. Round one found
three threats: mutants that no suite could ever reach regardless of test
quality, the agent being scored on exactly the mutants it was told about
(no transfer evidence), and a kill gate that proves sensitivity to one
mutation without proving the test specifies anything. Round two found that
the fix for the reachability problem exposed a new one: several targets'
reachable-survivor counts were too small to support a rate at all, which
also killed the held-out-operator transfer-rate design outright (see
Abandoned: holdout transfer control below). All resolved before any arm ran.

- **PRIMARY:** pooled survivor kill rate over REACHABLE survivors = (sum
  across all targets of reachable survivors killed) / (sum across all
  targets of reachable survivors available), before vs after. Pooled, not
  averaged per-target and not reported per-target as a rate -- see Why
  pooled, not per-target below.
- **REPORT ALSO, every time PRIMARY is reported:**
  - per-target reachable survivors killed, as raw counts (X of Y), never as
    a percentage -- see Why pooled, not per-target below.
  - raw SKR (same formula, denominator = ALL survivors, unreachable
    included) — so the curated primary number is never presented without
    the uncurated one next to it.
  - kill-outcome breakdown: killed / timeout / error, as counts and
    fractions of total mutants (see Kill outcome breakdown below).
  - assertion taxonomy per arm (see Assertion taxonomy below).
  - tokens in and out per arm.
  - wall clock per target and per arm.
  - waste rate: tests discarded by the kill gate (agent arm only).

There is no transfer-rate metric. A held-out-mutant anti-circularity control
was designed and built, then abandoned when the numbers came in too small to
support any rate -- see Abandoned: holdout transfer control below.
Anti-circularity instead rests on the assertion taxonomy: an arm cannot
inflate its apparent effectiveness by writing vacuous tests, because the
taxonomy reports exactly how many of its gate-passing tests are vacuous.

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

**Why pooled, not per-target:** most targets' reachable-survivor counts are
single digits to low teens even after widening test scope (see Denominator
below). "Killed 1 of 2" is not a rate, it is an anecdote wearing a
percentage sign -- the next mutant flips it to 50% or 100% with zero
underlying change in test quality. Pooling across all 12 targets is what
gives the denominator enough size for a rate to mean anything. Per-target
numbers are still reported, in full, as raw counts -- never suppressed,
never converted to a percentage that implies more precision than a
single-digit denominator can support.

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
it is because its 15 remaining survivors sit on lines only exercised by
`tests/test_cli.py`, the one file excluded from its widened suite (below)
for a platform reason unrelated to the module. Both targets therefore
contribute 0/0 to the primary metric and must be excluded from the pooled
calculation, not silently treated as 0%.

**Widening the suites, and what that turned out to mean.** The first
reachability pass (single narrow test file per target) put pooled reachable
survivors at 54 of 138 -- too thin a denominator to trust. The working
hypothesis was that this was a scoping artifact: narrow test commands, so
most of each module never runs. Every target's `test_command` was widened
to the broadest scope that still runs clean in well under the 20s ceiling
(full `tests/` directories in place of single files; three needed narrow,
unrelated exclusions -- an optional-dependency subtree in `validators`, a
missing `pytest-mock` fixture in `natsort`, and one platform-incompatible
CLI test in `dotenv` -- each confirmed by hand, not assumed). Test counts
run per target went up 6x to 40x.

Widening did not move the number much, and it did not move it up. Pooled
reachable survivors under the widened suites, confirmed by a 3x-serial
determinism check across all 12 targets with zero quarantined (see "Frozen
core reopened a second time" in CHANGELOG.md for why that check exists at
all), land at **53** of 455 total mutants -- 133 of 455 survive at all. One
of the twelve per-target figures drafted along the way to that number was
wrong: `aiofiles-temptypes` runs real async I/O against real temp files,
and concurrent mutant scoring (the harness's default at the time) gave it a
different survivor set on almost every run, drafting its widened figure at
1 unreachable / 5 reachable-survivor / 20 killed -- a large apparent
improvement that was concurrency noise, not a widening effect. Re-scored
serially, three times, with an identical survivor set every time, it shows
*no* movement under widening at all (11/8/7, matching its own pre-widening
figure exactly). With that correction, ten of the twelve targets showed no
change at all -- their non-`tests/test_X.py` files simply exercise
different code, not more of the same module. Where widening did move the
needle (`dotenv-variables`, `boltons-typeutils`), it worked almost entirely
by converting `unreachable` mutants directly to `killed`, not into
`reachable survivor`: the same broader test run that newly executes a line
usually also happens to assert something about it. The middle state the
hypothesis was banking on -- covered, but too weakly asserted to kill --
turned out to be the rare case, not the common one.

**This is the finding, not a limitation to work around:** across 12 widely
used, well-maintained Python libraries, running the broadest test scope
their own maintainers run, only 53 of 455 total mutants (12%) sit on a line
that executes under this suite but goes unasserted -- 133 of 455 (29%)
survive at all, the rest unreachable by this suite. The majority of
undetected faults in these modules are undetected because nothing runs that
code, not because the assertions that do run are weak. A tool that only
ever writes tests for reachable survivors -- this one included -- is
addressing the minority of the undetected-fault problem in code that looks
like this. That is worth stating plainly rather than narrowing the eval set
until the number looks better: two considered ways to make the number look
bigger were rejected on that exact principle (see Rejected:
reachable-survivor workarounds below).

Two targets have exactly 0 reachable survivors and there was no attempt to
make that not true. 53 pooled, reported honestly with the table above, is
the number.

### Rejected: reachable-survivor workarounds

Two ways to grow the pooled-53 denominator were considered and rejected
before Task 3, both on principle rather than because they were tried and
failed:

**Swap the worst targets for denser ones.** `voluptuous-error` and
`dotenv-variables` contribute 0 reachable survivors each. Replacing them
with modules chosen for denser existing assertion coverage would raise the
pooled number. Rejected: choosing eval-set targets *after* seeing which ones
produced thin denominators is case selection on the outcome -- it would
mean the eval set was tuned to flatter the metric it's supposed to be
measured against. The two weak targets stay.

**Push widening further.** Try harder specifically on the weak targets --
work around the `dotenv` `printenv` incompatibility instead of excluding
`test_cli.py`, pull in whole-repo suites beyond `tests/` for the others.
Rejected on the evidence just gathered, not on principle: 10 of 12 targets
showed *zero* movement even at full within-repo test scope, which means 53
is not an artifact of narrow test commands -- it is the real reachability
of these modules under the suites their maintainers actually run. There is
no reason to expect chasing scope further changes that.

### Abandoned: holdout transfer control

Two designs for an anti-circularity control were built and then abandoned,
both for the same root cause: an insufficient denominator. Recorded here
rather than deleted quietly, per CLAUDE.md's own style rule that a removed
experiment with a stated reason is evidence of judgment, not a gap.

**Design 1: operator-based holdout.** Exclude `boolop`/`unary_not` survivors
from every arm's work queue, score them anyway, report transfer rate =
held-out reachable survivors killed / held-out reachable survivors
available. Killed by the numbers: across all 12 targets, only 24 mutants
total are `boolop`/`unary_not` (of any outcome), 5 of the 12 targets have
*zero* such mutants at all, and only 1 of those 24 was ever both a survivor
and reachable. A rate needs a denominator; 1 pooled and 0 for most
individual targets is not one. This is not a reachability-widening problem
— `and`/`or`/`not` are just rare constructs relative to comparisons,
arithmetic, and returns in this eval set's source, so no amount of test-suite
widening changes how many of these mutants engine.py generates in the first
place.

**Design 2: positional holdout.** Instead of holding out by operator type,
hold out a fixed fraction of reachable survivors by position/index,
independent of operator. This gives a denominator proportional to each
target's total, which is a real improvement on paper — but per-target
reachable-survivor counts are themselves single digits to low teens even
after widening (see Denominator above), so a fixed fraction of a small
number is still a small number: most targets would still hold out 0 or 1
mutant. It also measures a different, weaker claim than intended: a test
that transfers to a same-operator mutant a few lines away is much less
informative about generalization than a test that transfers to an
undisclosed *type* of mutation, which was the actual point of a holdout.
Abandoned for both reasons.

**Decision:** no holdout control ships. Anti-circularity rests on the
assertion taxonomy instead: an arm cannot inflate its apparent
effectiveness with vacuous tests, because the taxonomy reports exactly how
many of its gate-passing tests are vacuous. This is a narrower guarantee
than a true held-out-mutant transfer signal would have been, and the
Limitations section says so plainly rather than implying the taxonomy is a
full substitute.

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

**Limitation, binding on how arm A/B taxonomy numbers can be read: the
classification unit and the scoring unit are not the same for arms A and
B.** Arm C targets one mutant per call, so one generated test maps to one
kill outcome — its taxonomy category can be cross-referenced against
whether that specific test killed anything. Arms A and B are scored per
*batch* (§Clean-pass failures, above): a whole target's generated tests are
appended once and the batch is scored once against every reachable
survivor. There is no record of which individual test inside a
multi-test batch caused which mutant to die. For arms A and B this means:
report the taxonomy distribution (what shape of test the arm wrote) and
report the kill count (how many mutants died), both fine on their own — but
do not report "which class of test did the killing" for A or B, because
that mapping does not exist and attributing a batch's outcome to every test
in it would silently overcount. `scripts/classify_tests.py` enforces this:
its `gate_would_keep` breakdown is only computed for a batch that split into
exactly one test (true by construction for arm C, true incidentally for any
A/B target whose whole batch happened to be a single test), and is reported
as explicitly not computable otherwise.

### If the result is positive

Everything written above anticipates a null, which was the honest bet given
where the evidence pointed going in. But if Arm C's gate-passing taxonomy
comes back mostly `value` class, with real kills against reachable
survivors, that is a genuine positive and it must not get hedged into mush
by the caution accumulated everywhere else in this document. State it now,
before the numbers exist, with the same discipline used to lock the metric:
if that is what the data shows, the honest claim is that the gate selects
for specification, not merely sensitivity, in this corpus, at this scale,
with the residual-equivalence caveat (see Limitations) still intact.

## Architecture

```
targets.json               eval set: 12 (project, module, test command) cases
killcheck/engine.py        AST mutation operators -> deterministic Mutant list   [FROZEN]
killcheck/runner.py        isolated execution, kill/survive ground truth         [FROZEN]
killcheck/logs.py          shared JSONL append helpers (HELD_OUT_OPERATORS: abandoned, unused)
killcheck/agent.py         the loop: survivor -> context -> test -> gate -> keep
killcheck/baseline.py      arm A (single prompt) and arm B (budget-matched)
killcheck/report.py        results table, markdown output
scripts/verify_targets.py  canary + widened baseline scoring + reachability
scripts/ablate.py          arm C's log -> what gate/retry each individually bought
scripts/classify_tests.py  deterministic AST assertion taxonomy per arm
trajectories/              one JSONL per run, every turn appended live
results/generated_tests.jsonl   every generated test, every arm, every attempt
results/target_verification.json denominator manifest: per-mutant reachability
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

### Clean-pass failures: never repaired, never dropped

A generated test that fails on clean source is the arm producing a wrong
test. This rule binds on all three arms, not just the one with a gate:

- **It is never dropped.** A test that can't be parsed or fails clean isn't
  quietly excluded from what the arm "really" produced -- it's what the arm
  produced.
- **It is never repaired.** Nobody hand-fixes a broken assertion, corrects a
  wrong literal, or patches a bad import before scoring. That would erase
  the exact difference the comparison exists to measure.
- **It is retried according to each arm's own design, and no further.** Arm
  C gets exactly one retry, with the real pytest failure fed back, per the
  agent loop contract above. Arms A and B get none -- that absence is the
  point of comparing them to C, not an oversight to smooth over.

**The consequence is asymmetric and must be stated wherever an arm's numbers
appear, not left for a reader to find in the JSON.** Clean-pass is evaluated
per batch: arm A's whole test set is appended once and scored once; arm B's
accumulated set the same way (see Metrics' note on batch vs incremental
scoring). One broken test in that batch fails the whole suite's collection,
which fails clean-pass for every other test generated alongside it, which
scores the target zero regardless of how many of the other tests in that
same batch were good. A single bad literal can erase an otherwise-strong
showing. This is real, not a scoring artifact to correct for -- an arm that
produces one unparseable or wrong test per target has a real reliability
problem, and burying that inside a silent zero would hide it rather than
report it. Table 1 carries a `clean_pass` column and a per-arm count of
targets that failed it for exactly this reason.

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
