# Changelog

Findings and decisions, in the order they happened. Not a commit log --
`git log` covers that. This is for "what did we try, why, what did the number
do, what did we decide."

## src-layout editable installs silently defeat mutation testing

**What happened:** while assembling `targets.json`, 3 of the first 12
candidate targets (`cachetools-func`, `dotenv-variables`, `aiofiles-temptypes`)
scored kill_score = 0.0000 -- every single mutant "survived," including
mutants that flipped literal constants read directly in the test assertions.
That is not a plausible test-suite gap; it is a broken harness.

**Root cause:** `runner.py` mutates a module by copying the whole project
into a tempdir and overwriting the module file there, then running the
target's test command with that tempdir as cwd. That is correct for a
flat-layout package (the package directory sits right under the project
root, so pytest's own rootdir-insertion puts the tempdir copy on `sys.path`
ahead of anything else). It is silently wrong for a `src/`-layout package
installed via `pip install -e .`: the editable install points an absolute
path back at the *original* checkout, and nothing about running pytest from
a different cwd changes that. The test process imports the pristine
original every time. The mutation is real, on disk, and never executed.

**How it was found:** kill_score = 0.0 for otherwise well-tested libraries
was the tell. Confirmed by hand: copied a target to `/tmp`, mutated one
constant, ran pytest from the copy, and printed `module.__file__` -- it
resolved to the *original* checkout path, not the copy, for the three
affected targets. For the other nine (flat layout), the same experiment
showed the copy's own path, confirming the mechanism.

**Fix:** added `-o pythonpath=src` to those three targets' `test_command`,
which makes pytest prefer the local (tempdir) `src/` over the installed
original. Verified by hand-mutating a constant read directly in a `validators`
assertion and confirming the test now fails on the expected value.

**Generalized into a permanent check:** `scripts/verify_targets.py` now runs
a canary before trusting any kill score from a target: overwrite the module
under mutation with unparseable garbage, run it through the exact same
`_evaluate_one` path the frozen runner uses, and assert the suite does not
report "survived." A canary that survives means mutations aren't reaching
the interpreter for that target, for any reason -- not just this one. All 12
targets pass the canary as of this entry.

**Decision:** keep the canary as a standing gate, not a one-time fix. Any
target added later must pass it before its kill score is trusted. Cost is
one extra subprocess run per target (~0.2s each) -- negligible next to the
mutation runs themselves.

## Kill outcome breakdown: no target's baseline is error-propped

**What:** added killed/timeout/error disaggregation to
`scripts/verify_targets.py` (see CLAUDE.md's Kill outcome breakdown section
for why `error` is weaker evidence than `killed`) and wrote
`results/target_verification.json` with the same breakdown per target.

**Result:** across all 12 targets, pre-agent kill outcomes are 99.8% `killed`
and 0.2% `timeout` (one mutant in slugify-special), and exactly 0 `error`
outcomes anywhere. None of the 12 pre-agent kill scores are error-dominant.
This means the baseline numbers recorded so far reflect real test assertions
catching real behavioral changes, not mutants that happened to break at
import time -- worth stating plainly rather than assuming, since it bears on
whether the eval set's headroom (0.27-0.97) is a fair reflection of test
quality or an artifact of how mutants happen to fail.

**Decision:** no action needed now. Re-run this check after the agent and
both baseline arms produce their own mutant sets (the agent writes new
tests, but the *mutants* are unchanged and still frozen-runner-generated,
so this is mainly a re-confirmation, not expected to change).

## Adversarial review of the metric, before any arm runs

An adversarial pass over the (already-committed) primary metric found three
more threats, all fixed in the same commit that adds this entry:

**1. Unreachable mutants were being scored as test-quality failures.**
Added coverage-based reachability to `scripts/verify_targets.py`: run the
clean suite under `coverage`, cross-reference each surviving mutant's line
against executed lines. Result: pooled across all 12 targets, 54 of the 138
surviving mutants are reachable survivors -- a healthy primary-metric
denominator overall -- but two individual targets, `voluptuous-error` and
`dotenv-variables`, came back with **0 reachable survivors each**, meaning
their contribution to the pooled denominator is 0/0, not 0%. Investigated
both by hand rather than accepting the number:

- `dotenv-variables`: genuine. `tests/test_variables.py` only exercises
  parsing/equality on `Literal`/`Variable`; it never calls `.resolve()`,
  `__repr__`, or `__hash__`, which is where all 19 survivors live. Confirmed
  by `grep` for `!=` and manual read of the module -- not assumed.
- `voluptuous-error`: an artifact, not a real finding. `coverage`'s line
  tracer does not mark a bare docstring (the sole statement in a class or
  function body) as executed, even when the class is defined and imported --
  confirmed directly: `import voluptuous.error` executes the `class Foo
  (Invalid):` lines but not the docstring lines immediately below them, per
  `executed_lines`. All 23 of `voluptuous-error`'s survivors are exactly
  this: one-line docstrings on exception subclasses. Decision: accept this
  as a known, documented limitation rather than special-case docstring
  AST nodes to route around it -- the practical effect is conservative
  (excludes mutants no test would plausibly assert on anyway) rather than
  distorting the metric in the tool's favor. Recorded in CLAUDE.md's
  Denominator section so nobody mistakes "0 reachable survivors" for "this
  target is unusually well-tested."

**2. The agent could be scored on exactly what it was told to fix.**
Added `boolop`/`unary_not` as held-out operators (`killcheck/logs.py`,
`HELD_OUT_OPERATORS`): removed from every arm's work queue, still scored.
Transfer rate (held-out survivors killed after an arm runs, despite never
being targeted) is the anti-circularity control -- see CLAUDE.md's Held-out
operators section. No data yet; infrastructure only, verified by unit
checks in `scripts/verify_targets.py`'s held-out partition counts.

**3. The kill gate proves sensitivity, not specification.** Built
`scripts/classify_tests.py`, a deterministic AST classifier (value / mock /
exception / existence / none) with no LLM involved, plus the pre-registered
hypothesis that a meaningful share of gate-passing tests will land in
`none`/`existence`. Verified against a synthetic 4-test fixture covering
all five categories (including a `pytest.raises` case reached only via a
retry) before deleting the fixture -- no real arm has produced data yet.

**Also added:** `results/generated_tests.jsonl` schema and
`killcheck/logs.py` as the single write path for it and for trajectories,
and `scripts/ablate.py`, which reconstructs what a no-gate/no-retry/both
design would have kept from one arm C run. Verified against the same
synthetic fixture: confirmed `skr(C) == skr(C_minus_gate)` holds as the
structural identity it should be (removing the gate cannot change which
survivors get killed, only how much non-killing material ships), and
confirmed `skr(C_minus_retry) != skr(C_minus_both)` CAN legitimately diverge
when a test fails on both clean and mutant source (a broken test, not a
real kill) -- the synthetic fixture deliberately included one such row to
prove `ablate.py` catches this rather than silently over-crediting it.

**Decision:** all of the above is infrastructure and metric definition, not
results. Committed as one changeset alongside the CLAUDE.md and README
updates, before `killcheck/baseline.py` (arms A and B) is written -- the
commit ordering is itself part of the evidence that none of this was shaped
by a result it needed to explain.

## Frozen core reopened a second time: parallel mutant execution was corrupting one target's ground truth

**What happened:** re-verifying the 2b table before committing it (per this
project's own "re-verify, then commit" discipline) found one cell that
didn't reproduce: `aiofiles-temptypes` came back with a different
reachable-survivor count than the run that had already been drafted into
CLAUDE.md, README, and CHANGELOG text. Investigated rather than re-run until
it matched.

**How it was found:** ran `aiofiles-temptypes`'s full mutation scoring four
times at the original `workers=4`: kill scores of 0.7308, 0.7308, 0.7692,
0.7308, with *different survivor sets*, not just different counts. Ran it
twice more at `workers=1`: 0.2692 both times, identical survivor set both
times -- and identical to the target's own pre-widening number. Checked
three other targets (`dotenv-variables`, `boltons-typeutils`,
`tenacity-stop`) the same way: `workers=4` and `workers=1` matched exactly
on all three. The problem is isolated to the one target running real async
I/O against real temp files under concurrent subprocess execution --
`_evaluate_one` already isolates each mutant in its own tempdir and its own
subprocess, so this is not a general isolation-strategy bug, but four
concurrent pytest-asyncio processes touching the real filesystem
simultaneously are enough to spuriously fail tests that would otherwise
pass, and `runner.py` cannot distinguish "the mutation broke it" from
"concurrent execution broke it."

**Why this matters more than one target:** the corruption is not neutral
noise. A spurious concurrent failure is read by the runner as a kill, and
kill count is the exact quantity every arm in this project exists to
increase. Non-determinism in this specific harness does not average out
across runs -- it systematically flatters whatever is being measured, in
the one direction that would make an intervention look better than it is.
Fixing only the one target caught behaving badly would have left every
other number carrying the same unquantified doubt, just without visible
symptoms.

**Fix:** `killcheck/runner.py`'s `score_target()` default changed from
`workers=4` to `workers=1`. This reopens the frozen measurement core for the
second time (first was the src-layout `pythonpath` fix during eval-set
construction). Per CLAUDE.md's invariant 5, changing the measuring
instrument after establishing a baseline requires re-running both arms and
noting it in the changelog -- this change lands here, before either arm has
run for the first time, so no re-run is owed yet, but the instrument is
different from what every number in this document up to this entry was
computed with.

**Also worth recording honestly:** the edit that added this explanation to
`runner.py`'s module docstring initially dropped the docstring's closing
`"""`, breaking the file outright (a `SyntaxError` on import). Caught
immediately by the first thing that imports `runner.py` -- both
`scripts_demo.py` (the fixture sanity check) and the verification run
itself failed loudly rather than silently. Fixed and re-verified
`scripts_demo.py` still reports kill_score=0.0952 on `fixture/bank.py`,
identical to the value recorded before any of this session's changes,
before re-running anything at scale. A frozen file being edited at all,
twice now, for correctness reasons, is exactly the situation this project's
own sanity checks exist for.

**Decision:** every reachability number and every "widening helped this
target" claim drafted since the 2b widening pass is provisional until
re-derived under `workers=1`. Retracted and replaced in CLAUDE.md, README,
and this file -- see the following entries, committed separately from this
one so the instrument fix and what it changed can be reviewed independently.
