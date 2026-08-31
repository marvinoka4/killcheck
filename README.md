# killcheck

A pre-committed instrument for testing whether agentic test generation
produces tests that specify behaviour, or probes that detect only the edit
they were shown.

The measurement came first. The mutation engine and runner were frozen and
committed before any agent code existed. The metric was defined and committed
before any arm ran. One framing claim was retracted when the data contradicted
it, and the question narrowed three times under evidence. Both the retraction
and the narrowings are in [CHANGELOG.md](CHANGELOG.md) with the numbers that
forced them.

Concretely: for a Python module, the harness generates one mutant per mutable
site (comparison flips, arithmetic swaps, constant changes, dropped negations,
`raise`→`pass`, return-value→`None`), runs the existing suite against each, and
records which mutations the suite fails to notice. Those survivors are the work
queue. A generated test is kept only if it both passes on clean source and
fails on the specific mutant it targets. Ground truth throughout is a
subprocess exit code — no model judges any outcome.

**What this measures, stated up front so it need not be reconstructed:**
whether a mutant hint, an execution gate, and a single retry beat none of
those, at one test per call, over reachable survivors in modules from
widely-used Python libraries. It is not a measure of whether agents write good
tests in general, and the one-test-per-call cap makes "the agent beats
single-prompt generation" false by construction. Each narrowing from the
broader original question was forced by data or by an identification problem.

**No holdout control survived.** We do not measure whether the generated tests
specify behaviour independent of the shown operator. We measure assertion
syntax, whether the gate filters on pre-registered features of that syntax, and
collateral kills and breadth as weak transfer probes. Two holdout designs were
built and abandoned before any arm ran; see CHANGELOG for the denominators that
killed them.

Submission for the micro1 Frontier Engineering Challenge 2026. See
[CLAUDE.md](CLAUDE.md) for the full experimental design — invariants, metrics,
the agent loop contract, and the eval set. See [CHANGELOG.md](CHANGELOG.md) for
findings and decisions in the order they happened, and
[tripwires.md](tripwires.md) for the abort conditions checked during the agent
run.

---

## A finding, not a caveat: most undetected faults are unreached code, not weak assertions

Before building the agent, the eval set's 12 targets were checked for how many
of their surviving mutants sit on a line their own test suite actually executes
at all (`scripts/verify_targets.py`, full method in CLAUDE.md's Denominator
section). Every target's test command was widened to the broadest scope that
still runs clean and fast — full `tests/` directories, not single files —
specifically to rule out narrow test scoping as the cause of a thin number.

It wasn't the cause. Pooled across all 12 widely used, well-maintained Python
libraries and all 455 mutants the harness generated, only 53 sit on a line
that is reachable-by-this-suite yet goes unasserted. 133 of 455 survive at all;
the rest are killed or genuinely unreachable. Widening test scope 6×–40× per
target moved that number from 54 to 53 — lower, not higher. Ten of the twelve
targets showed no movement at all: their non-`test_X.py` files simply exercise
different code, not more of the same module. Where widening did move something
(`dotenv-variables`, `boltons-typeutils`), it worked almost entirely by
converting `unreachable` mutants straight to `killed`, not into `reachable
survivor`.

| target | mutants | unreach / reach / kill (before) | unreach / reach / kill (after) |
| --- | --- | --- | --- |
| cachetools-func | 51 | 0 / 11 / 40 | 0 / 11 / 40 |
| validators-card | 60 | 3 / 2 / 55 | 3 / 2 / 55 |
| natsort-ns-enum | 20 | 0 / 2 / 18 | 0 / 2 / 18 |
| dictdiffer-resolve | 19 | 2 / 2 / 15 | 2 / 2 / 15 |
| toolz-dicttoolz | 50 | 3 / 4 / 43 | 3 / 4 / 43 |
| voluptuous-error | 51 | 23 / 0 / 28 | 23 / 0 / 28 |
| slugify-special | 39 | 0 / 1 / 38 | 0 / 1 / 38 |
| dotenv-variables | 30 | 19 / 0 / 11 | 15 / 0 / 15 |
| shortuuid-main | 48 | 3 / 7 / 38 | 3 / 7 / 38 |
| boltons-typeutils | 26 | 14 / 3 / 9 | 14 / 2 / 10 |
| aiofiles-temptypes | 26 | 11 / 8 / 7 | 11 / 8 / 7 |
| tenacity-stop | 35 | 6 / 14 / 15 | 6 / 14 / 15 |
| **pooled** | **455** | **84 / 54 / 317** | **80 / 53 / 322** |

Read plainly: the majority of undetected faults in these modules are
undetected because nothing runs that code, not because the tests that do run
assert too little. The "coverage is high but assertions are vacuous" story is
the industry account of AI-generated tests. It is largely not what mature
human-written suites do. A tool that only ever writes tests for reachable
survivors — this one included — is addressing the minority of the
undetected-fault problem in code shaped like this.

This reframing was forced by data collected before any arm ran, and the
original premise was partially wrong. Two ways to make the pooled number look
bigger — swapping the two zero-reachable-survivor targets for denser ones, and
chasing wider test scope — were considered and rejected before the arms: the
first is case selection on the outcome, the second is contradicted by the data
above. See CLAUDE.md's "Rejected: reachable-survivor workarounds", and
CHANGELOG.md for the numbers.

---

## Results

Arm C ran on 2 of 10 scoring targets before the API budget was exhausted. The
remaining 8 targets, holding 38 reachable survivors, were not run. We did not
substitute a different generator to fill the gap, because the comparison would
no longer be identified. Everything below is restricted to the 15 reachable
survivors arm C actually covered, with arms A and B recomputed over that
identical mutant set rather than over all 53.

### The three arms

- **Arm A** — one direct prompt with basic instructions, one call per target,
  unbounded test count. The baseline the challenge brief names. No mutation
  information, no gate, no retry.
- **Arm B** — one-test-per-call policy, call count aligned to the target's
  reachable survivor count. Same model, same token ceiling. Still no mutation
  information, no gate, no retry.
- **Arm C** — the agent. One mutant per call, one test per call, the mutant
  diff in context, an execution gate, and exactly one retry on gate failure.

Arm B exists so that a C result cannot be attributed to compute alone. It is
*not* budget-matched in any general sense — see the resource vector below.

### Head to head, the 15 survivors arm C covered

| target | reachable | A official | A repaired | B | C |
| --- | --- | --- | --- | --- | --- |
| slugify-special | 1 | 0 | 1 | 0 | 1 |
| tenacity-stop | 14 | 0 | 1 | 1 | 8 |
| **pooled** | **15** | **0** | **2** | **1** | **9** |

Arm B needed no repair: its batches passed clean on both targets.

The identified comparison is **B vs C** — same one-test-per-call regime, same
model, same token ceiling, differing only in the mutant hint, the gate, and one
retry. On these 15 survivors that is **1 kill against 9**. Arm A is shown for
description only: it writes an unbounded batch per call, so a C-vs-A gap moves
two mechanisms at once and identifies neither.

**The sample is not random, and this is the caveat to reach for first.** These
two targets are where arm A did worst. `tenacity-stop` was its 0/14 official
and 1/14 repaired — a module requiring time mocking, designated a hard case
before any arm ran. The 38 unrun survivors include `cachetools-func`, where arm
A wrote 69 tests that all passed clean and killed 0 of 11. Whether arm C would
have done better there is unknown. A 9/15 on the hardest module in the set is
not evidence of 9/15 on the easiest.

### What the gate did

| target | reachable | drafts | kept | discarded | keep rate | retries fired | retries succeeded | official killed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| slugify-special | 1 | 1 | 1 | 0 | 100% | 0 | 0 | 1/1 |
| tenacity-stop | 14 | 23 | 8 | 6 | 57.1% | 9 | 3 | 8/14 |
| **pooled** | **15** | **24** | **9** | **6** | **60.0%** | **9** | **3** | **9/15** |

Per-call gate outcomes and the one-pass batch rescore agree on every mutant, on
both targets. That matters here specifically: arm A broke `tenacity-stop`'s
existing suite through a naming collision — its own `make_retry_state()` helper
shadowed the pre-existing one and broke 12 tests that had nothing to do with
the module under test. Arm C wrote 14 tests into the same file, one at a time,
and its kept set runs clean together. The per-test gate rejects before damage
compounds, which is a mechanical property of the design rather than luck.

### The gate rejects valid tests that miss, not broken tests

Of the 6 discarded drafts, **0 failed the clean-source check and 6 passed clean
but failed to kill their target mutant.**

This is the most informative number in the run and it was not the expected one.
The gate was designed with two jobs: screen out tests that do not work, and
screen out tests that work but do not detect the fault. In this sample it only
ever did the second. Every draft the model produced was a runnable, passing
test. Six of them simply did not go red when the code was wrong.

### Assertion class, kept versus discarded

| class | kept | discarded |
| --- | --- | --- |
| value | 7 | 6 |
| existence | 2 | 0 |
| exception | 0 | 0 |
| mock | 0 | 0 |
| none | 0 | 0 |

The pre-registered hypothesis, recorded in CLAUDE.md before any test existed,
was that a meaningful share of gate-passing tests would be `none` or
`existence` class — that the gate would select differential probes rather than
specifications. **On this sample that hypothesis is not supported, and the
result runs the other way.** The `none` bin is empty. Every discarded draft is
`value` class, while both `existence` drafts were kept.

The honest reading is not that the gate selects for quality. It is that the
gate does not select on assertion class at all in this sample: `value` tests
appear on both sides of the keep decision, and n=15 with 6 discards cannot
support a claim about class either way. What the numbers do rule out is the
specific failure mode we set out to find. There are no assertion-free tests
here and no crash-only oracles: 8 of 9 kills are call-phase `AssertionError`,
one is a call-phase other exception. Coverage gaming did not migrate to process
status in this sample.

### Kills by operator family

| operator | call-phase AssertionError | call-phase other | total |
| --- | --- | --- | --- |
| constant | 7 | 1 | 8 |
| return_none | 1 | 0 | 1 |

All 9 kills are `constant` or `return_none` — the cheap end of the fault model,
and the same two families that dominated both baseline arms. No `compare`,
`binop`, `boolop`, `unary_not` or `raise_removed` mutant was killed by any arm
on these targets. Given `constant` is 54% of the whole mutant population
(247/455) and `return_none` 23% (105/455), this is partly population shape. It
is also a real limit on what a kill demonstrates.

### Transfer probes

Collateral kills: 10 total kills across 9 kept tests. One same-function
collateral kill, **zero cross-function**. Since survivors cluster densely
within functions, same-function collateral is largely geometry. Cross-function
collateral was the only remaining transfer signal after both holdout designs
were abandoned, and it is zero here.

Breadth, kept tests, no threshold cut: 1/1, 1/9, 1/9, 1/9, 1/2, 2/2, 1/1, 1/2,
1/2. Seven of nine kept tests kill exactly the one mutant they were written for.

**Read together, these two lines are the closest thing to an answer we have on
the original question, and it is not favourable.** The gate produces tests that
detect the edit they were shown and, with one exception, nothing else. That is
consistent with the framing written before the run: the suite goes red when
this rewrite is applied, and need not go red when the behaviour is wrong in any
way the rewrite does not encode. It is not a control, and it cannot be — no
holdout survived. It is a weak probe pointing in one direction on 15 mutants.

### Resource vector, these 2 targets

| arm | calls | tokens in | tokens out | tests emitted | tests passing clean | wall clock |
| --- | --- | --- | --- | --- | --- | --- |
| A | 2 | 37,767 | 9,318 | 128 | 126 | 118s |
| B | 15 | 410,147 | 3,122 | 15 | 15 | 127s |
| C | 24 | 672,218 | 7,799 | 24 | 21 | 404s |

No two arms spent the same amount of anything except calls, and only B and C
are call-aligned. Arm B resends the full module and test file on every call,
which is why it consumes 11× arm A's input tokens to emit a third as many
output tokens. Arm C carries the same context tax plus the mutant diff and
retry feedback.

There is no neutral budget unit here: matching on output tokens would let B
emit 128 tests in 15 calls, which is arm A with extra steps; matching on input
tokens would fine C for showing the diff, which is information rather than
padding. The unit was fixed before any arm ran and is reported as a policy, not
as a fairness claim.

### One pre-registered feature was not measurable

`calls_mutated_function` — whether a generated test names the mutated function
— is undefined for `tenacity-stop`. All 14 mutated functions there are dunders
(`__call__` ×9, `__or__` ×2, `__and__` ×2, `__init__` ×1), and Python's call and
operator syntax never spells the dunder name, so an AST name match reads
near-zero by construction rather than because the tests miss the code. It is
reported as not computable rather than as a low rate. Pre-registration protects
against choosing a feature after seeing results; it does not guarantee the
feature is measurable on every target.

### What did not run

| target | reachable survivors |
| --- | --- |
| cachetools-func | 11 |
| aiofiles-temptypes | 8 |
| shortuuid-main | 7 |
| toolz-dicttoolz | 4 |
| validators-card | 2 |
| natsort-ns-enum | 2 |
| dictdiffer-resolve | 2 |
| boltons-typeutils | 2 |
| **total** | **38** |

---

## What actually broke: eight instrument bugs

The agent was never the hard part. The instrument was. Eight bugs were found in
the measurement apparatus during this build, every one of which would have
produced a confident, wrong, publishable number.

1. **src-layout imports.** `pip install -e` resolved imports back to the
   original checkout, so tempdir mutations never executed. Three targets
   silently scored 0.000. Without the catch this would have read as "the agent
   fails on src-layout packages" rather than "the harness was broken." Fixed,
   then generalised into a standing **canary check**: overwrite each module
   with unparseable source and assert the suite does not report `survived`.
2. **Concurrent execution.** Parallel mutant evaluation corrupted results for
   the one target running real async I/O against real temp files: four repeated
   runs gave three different survivor sets. A widening improvement already
   drafted into three files as 0.27→0.77 was concurrency noise. Now
   `workers=1` everywhere, with a standing **determinism gate**: three serial
   runs, survivor sets must be byte-identical.
3. **Test-file picker.** The largest-file fallback selected `tests/test_os.py`
   for a module it does not exercise, on the eval set's designated hard case —
   which would have shown both baseline arms irrelevant context on the target
   where it mattered most.
4. **Taxonomy classifier unit.** About to be run on batched rows, where one
   strong test in a batch of 69 would have classified all 69, hiding 68 weaker
   ones.
5. **Test reconstruction.** Extracting test functions individually dropped
   shared imports and broke class-based tests, manufacturing failures that were
   not real.
6. **Test-name extraction.** Only scanned top-level statements, so a valid
   `unittest.TestCase` response was discarded as "no test function found" — and
   the retry loop was fed a harness error instead of real pytest output,
   degrading the exact mechanism arm C exists to measure.
7. **Assertion detection.** `self.assertEqual(...)` classified as `none`, which
   would have reported asserting tests as assertion-free — manufacturing
   precisely the finding we were hypothesising.
8. **An unmeasurable pre-registered feature.** `calls_mutated_function` is
   undefined for dunder-dispatched code, and would have read as a real
   near-zero rate on the target carrying 14 of 15 survivors.

Two patterns matter more than the individual bugs.

**They are not randomly signed.** Every one biased toward a more flattering or
more publishable result: spurious failures read as kills, batch classification
inflating the strong class, a valid test discarded as a rejection, a classifier
gap that would have manufactured our own hypothesis. The reason is not
conspiracy but attention — a disappointing number gets investigated, a pleasing
one gets written up. An instrument bug that flatters you is therefore far more
likely to survive to publication than one that does not.

**None was found by reading the code.** Every one was found by running a check
whose outcome was predicted in advance and came back wrong. The clearest case
is bug 5: the prediction was that removing the one known-bad test would restore
clean-pass. It didn't. The check was looking for confirmation and returned a
contradiction, which is the only reason the reconstruction bug was caught
before its numbers were trusted.

The determinism gate fired again during the final clean-clone reproduction, on
the same target, and quarantined it. The check is not decorative.

**The practical lesson we would carry into the next agentic evaluation:** the
canary and the determinism check prove *different properties*, and a harness
needs both. The canary proves mutations reach the interpreter. The determinism
check proves execution is isolated. Neither is findable by inspection. Before
measuring an agent, write the checks that would fail if your instrument were
lying to you — and note which direction each possible lie would push your
result.

---

## Reproducing this

**Prerequisites:** Python 3.11+ (developed on 3.14, macOS). Roughly 2 GB disk
for the 12 cloned target repos. An `ANTHROPIC_API_KEY` is required only for the
arm runs, not for the harness verification.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then add a real ANTHROPIC_API_KEY
```

**In order:**

1. **`bash scripts/fetch_targets.sh`** — clones all 12 target repos at the
   pinned commit SHAs in `targets.json`. Nothing is vendored; requires network.
   A few minutes.
2. **`python3 scripts_demo.py`** — smoke test against the local fixture
   (`fixture/bank.py`), independent of the cloned targets. Expected output:
   `mutants=21 killed=2 survived=19 kill_score=0.0952`. Seconds. If this number
   differs, stop — the measuring instrument has changed.
3. **`python3 scripts/verify_targets.py`** — per target: the canary check, the
   determinism check (3 serial full scoring runs, survivor sets must be
   byte-identical), then coverage-based reachability bucketing. This is the slow
   step, and it is slow deliberately: `workers=1` is required for
   reproducibility. Writes `results/target_verification.json`. Tens of minutes
   for all 12.
4. **`python3 killcheck/baseline.py --arm A`** and **`--arm B`** — the two
   baseline arms. Writes `results/baseline_arm_a.json` and
   `results/baseline_arm_b.json`. ~10 and ~53 model calls respectively.
5. **`python3 killcheck/agent.py`** — arm C. One call per reachable survivor,
   plus up to one retry each. Writes `results/agent_arm_c.json`. Add
   `--target <name>` to run a single target.
6. **`python3 scripts/classify_tests.py`** — assertion taxonomy over all
   generated tests. **`python3 scripts/ablate.py`** — reconstructs the weaker
   keep-rules from arm C's draft log without additional calls.

**Approximate cost of a full reproduction:** the harness steps (1–3) are free.
The three arms together ran roughly 90 model calls for this submission at
Sonnet rates; a complete arm C across all 10 scoring targets would add ~38 more
plus retries. Expect a few US dollars total.

### One target does not reproduce deterministically

A clean-clone reproduction run of `scripts/verify_targets.py` three hours
before submission reproduced 11 of 12 targets exactly against the committed
`results/target_verification.json`: all 12 pass the canary, and 11 produce
byte-identical survivor sets across three serial runs. The twelfth,
`aiofiles-temptypes`, varied — survivor set sizes 19, 18, 19 across three
runs, one mutant flipping from survived to killed in a single run. The
determinism gate quarantined it rather than accepting the 2-of-3 majority,
which is what the gate is for.

This is the same target that motivated the `workers=1` fix: it drives real
async I/O against real temporary files. Serial execution removed the large,
result-changing non-determinism documented above — its kill score moved from
a 0.27/0.77 spread under concurrency to a stable 0.2692 on the original
machine — but a residual single-mutant flake remains and is
environment-dependent. We are reporting it rather than fixing it three hours
before the deadline, and rather than re-running until it agreed.

It does not affect any reported result. `aiofiles-temptypes` is one of the
eight targets arm C did not run. It contributes 0 of the 15 reachable
survivors in the head-to-head, and 0 of the 9 kills. It does contribute 8
reachable survivors to the pooled 53 used in the arms A and B tables, so
those pooled figures carry a one-target reproducibility caveat.

A reader reproducing this work should expect 11 of 12 targets to match
exactly and `aiofiles-temptypes` to quarantine.

**Trajectories** for every arm are in `trajectories/`, one JSONL per run, one
row per model turn, written live rather than reconstructed. Every generated
test — kept and discarded — is in `results/generated_tests.jsonl` with its gate
outcomes.

---

## Limitations

Scope was fixed before the arms ran, so it could not be narrowed to fit the
numbers. Everything below was written in advance except where noted.

**No anti-circularity control.** This is the most significant limitation in the
submission and it is stated rather than dressed up. Three different questions
are easy to conflate here:

- *(a)* what does the generator emit — answered by the assertion taxonomy;
- *(b)* does the gate select on anything measurable — answered by the
  pre-registered mechanical features;
- *(c)* do the kept tests specify behaviour independent of the mutation they
  were shown — **not answered by anything in this submission.**

Only (c) is anti-circularity. Two holdout designs were built and abandoned
before any arm ran: by operator family, which had a pooled held-out
reachable-survivor denominator of 1, and by position, which rounds to 0–1 per
target at these population sizes. What remains are two weak probes: collateral
kills on reachable mutants a test was not written for, reported split by
whether the collateral mutant is in the same function as the target
(same-function collateral is largely geometry, since survivors cluster densely;
cross-function collateral is the only real transfer signal left), and breadth
within the function. Neither is a control. An earlier draft of this file
claimed anti-circularity rested on the assertion taxonomy. That claim was wrong
and is corrected here.

**Arm C is incomplete.** 2 of 10 scoring targets, 15 of 53 reachable survivors,
stopped by an exhausted API budget. The two completed targets are not a random
sample — they are among those where arm A performed worst. No general claim
about arm C's performance is available from them.

**No per-test kill attribution for arms A and B.** Arm C targets one mutant per
call, so one generated test maps to one kill outcome. Arms A and B are scored
per batch, so there is no record of which individual test caused which mutant to
die. For A and B this submission reports the taxonomy distribution and the kill
count side by side, but not which class of test did the killing.

**Batch-invalidation makes arm A's official score a floor.** A generated test
that fails on clean source counts as the arm producing a wrong test — never
dropped, never repaired, and clean-pass is evaluated per batch, so one broken
test invalidates every other test generated for that target. Arm A failed
clean-pass on 6 of 10 targets. Five were the model writing a wrong test; one
(`natsort-ns-enum`) was our own append-only augmentation misplacing a
`from __future__` import, with zero bad tests in a batch of 40. The repaired
diagnostic is reported alongside, never instead of, the official number.

**Generated tests can break the suite they join, and nothing here detects it.**
Arm A's `tenacity-stop` batch defined a `make_retry_state()` helper that shadowed
the pre-existing one with an incompatible signature, breaking 12 unrelated
tests through module-level rebinding. Both the kill gate and the assertion
taxonomy operate on one test in isolation and structurally cannot see this.

**Diagnostic-B is a rescoring, not a re-run.** It rescores tests generated under
the batch-zero prompt, so later calls could see a sibling that per-test
inclusion would have dropped. A native re-run was considered and rejected: it
would change the generator's context and produce a fourth arm rather than a
correction.

**No search-based baselines** (Pynguin, CodaMosa). Whether a non-LLM
search-based generator would beat either baseline arm is a real question; it is
not this one.

**No real-fault benchmarks** (Defects4J or similar). This eval set measures
detection of synthetic AST mutations, not reproduction of historical bugs.
Mutation testing is a proxy for fault-detection ability, not a substitute for
it — a limitation of mutation testing generally, not of this tool specifically.

**No higher-order mutants.** Every mutant here is a single AST-level change.

**No maintenance-cost or flake measurement** beyond what the taxonomy captures.
A test that clears the gate today is not guaranteed to stay meaningful as the
source evolves.

**No semantic equivalence detection.** `engine.py` drops mutants whose unparsed
AST is textually identical to the original, and reachability removes mutants no
test structurally reaches — but neither catches a mutant that is reachable,
executed, and still behaviourally equivalent for every input any test
constructs. Residual semantic equivalence among reachable survivors is
unbounded by our method, and the mutant population is 54% `constant`, the family
where such equivalents concentrate. Reachable-survivor kill rate is a process
statistic, not a killable-mutant rate. **A 20-item stratified manual audit was
planned and did not run** — the time went to the arm C run instead.

---

## Hot take

Everything in this repository that went wrong went wrong in the instrument, not
the agent. Eight measurement bugs, all of which would have produced a confident
number, none of which was found by reading code, and every single one biased
toward the more flattering answer.

That last property is the one worth carrying forward. Evaluation bugs are not
randomly signed, because attention is not randomly allocated: a result that
disappoints you gets investigated and a result that pleases you gets written
up. So the errors that survive to publication are disproportionately the ones
that helped. This is not a claim about anyone's integrity. It is a claim about
what a debugging process selects for when the only trigger for debugging is
surprise.

The practical consequence is that "I read the code and it looks right" is not
evidence about an eval harness. The checks that found things here — the canary,
the determinism gate, the predicted-outcome sanity check — all share one
property: each would *fail loudly* if a specific assumption were false. Before
measuring an agent, write down what your instrument would look like if it were
lying to you, then build the check that catches exactly that. And write down
which direction each possible lie would push your result, because that tells
you which checks you will be least motivated to run.
