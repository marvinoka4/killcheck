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

## Task 2b: widened test scope to fix the denominator -- it didn't work, and that's the finding

54 reachable survivors pooled across 12 targets (2 at zero) was flagged as
too thin before Task 3 could proceed. Working hypothesis: test commands
were narrowly scoped (often one test file per target), so most of each
module's code never ran and its mutants landed in `unreachable` rather than
`reachable survivor`. If true, widening scope should move mutants from the
former bucket to the latter.

**What was done:** every target's `test_command` in `targets.json` was
widened to the broadest scope that still runs clean and fast -- full
`tests/` directories in place of single files, where a `tests/` directory
existed at all. Three needed narrow exclusions, each confirmed by hand, not
assumed to be safe:

- `validators-card`: `--ignore=tests/crypto_addresses` -- 17 tests
  `ImportError` on `validators[crypto-eth-addresses]`, an optional extra
  unrelated to `card.py`.
- `natsort-ns-enum`: added `pytest-mock` to `extra_requirements` -- the
  wider suite uses the `mocker` fixture, which isn't a locale problem as
  the first error message suggested; read the traceback fully before
  guessing.
- `dotenv-variables`: `--ignore=tests/test_cli.py` -- one test shells out to
  the system `printenv --version`, which fails on macOS's BSD `printenv`
  ("illegal option"). A platform bug in the test, not in `variables.py`.

`voluptuous-error` and `slugify-special` were already at their repos'
widest possible scope; left unchanged. All widened suites re-verified clean
and re-passed the canary. Runtime per target (single suite run, not the
full mutation sweep): 0.4s-4.8s across all 12, comfortably under the 60s
ceiling -- none needed to revert on a runtime basis.

**Result:**

```
target                 before(unreach/reach/kill)   after(unreach/reach/kill)
cachetools-func          0/11/40                      0/11/40   (unchanged)
validators-card          3/ 2/55                      3/ 2/55   (unchanged)
natsort-ns-enum          0/ 2/18                      0/ 2/18   (unchanged)
dictdiffer-resolve       2/ 2/15                      2/ 2/15   (unchanged)
toolz-dicttoolz          3/ 4/43                      3/ 4/43   (unchanged)
voluptuous-error        23/ 0/28                     23/ 0/28   (unchanged)
slugify-special          0/ 1/38                      0/ 1/38   (unchanged)
dotenv-variables        19/ 0/11                     15/ 0/15   (4 unreachable->killed)
shortuuid-main           3/ 7/38                      3/ 7/38   (unchanged)
boltons-typeutils       14/ 3/ 9                     14/ 2/10   (1 reachable-survivor->killed)
aiofiles-temptypes      11/ 8/ 7                     11/ 8/ 7   (unchanged)
tenacity-stop            6/14/15                      6/14/15   (unchanged)

POOLED reachable-survivor: 54 -> 53
```

**Correction, made before this entry was ever committed:** the `after` row
for `aiofiles-temptypes` originally read `1/5/20`, with a claimed kill-score
jump of 0.27->0.77, and the pooled total below it originally read 50, not
53. Both were concurrency noise -- see "Frozen core reopened a second time"
below for how this was found. `aiofiles-temptypes` runs real async I/O
against real temp files, and concurrent mutant scoring (the default at the
time) produced a different survivor set on almost every run. Re-scored
serially, three times, with an identical survivor set every time,
`aiofiles-temptypes` shows *no* movement under widening at all -- its
"after" row equals its "before" row. That changes twelve of twelve targets'
verdicts to "unchanged or converted-to-killed, never contaminated," and
moves the pooled figure from the drafted-but-wrong 50 up to the
serially-verified 53. The table above is the corrected version; nothing
in this entry was drafted from the bad number.

The hypothesis was wrong about the *shape* of the fix, not just its scale.
Widening ran 6x-40x more tests per target and meaningfully improved one
kill score (`dotenv-variables` 0.37->0.50), but almost entirely by
converting `unreachable` mutants straight into `killed`, not into
`reachable survivor` -- the same broader test run that newly executes a
line usually also happens to assert something about it. The "covered but
too weakly asserted to kill" state the hypothesis was banking on turned out
to be the rare case, not the common one. Ten of twelve targets showed no
movement at all: their other test files simply exercise different code, not
more of the same module.

**Decision, per the reframe recorded in CLAUDE.md and README.md:** this is
a finding about the shape of undetected faults in well-tested open-source
Python, not a defect in the eval set to be engineered away. Pooled 53
reachable survivors, reported honestly with the table above, is the number
Task 3 proceeds with.

**Two ways to make the number bigger were considered and rejected, on
principle, before Task 3 -- an abandoned option with a stated reason is
evidence of judgment, logged the same as an abandoned design:**

- **Swap `voluptuous-error` and `dotenv-variables` for denser targets.**
  Rejected: choosing eval-set targets *after* seeing which ones produced
  thin denominators is case selection on the outcome. It would mean the
  eval set was tuned to flatter the metric it's supposed to be judged
  against. Both targets stay, contributing their honest 0/0.
- **Push widening further** (work around the `dotenv` `printenv`
  incompatibility instead of excluding `test_cli.py`; pull in whole-repo
  suites beyond `tests/` for the remaining weak targets). Rejected on the
  evidence just gathered, not on principle: 10 of 12 targets showed zero
  movement even at full within-repo test scope. 53 is not an artifact of
  narrow test commands -- it is the real reachability of these modules
  under the suites their own maintainers run. There is no reason to expect
  chasing scope further changes that.

## Task 2b: holdout transfer control abandoned outright

The operator-based holdout (`boolop`/`unary_not` excluded from the work
queue, scored anyway) built during the first adversarial-review pass was
re-examined once the denominator problem turned out to be general, not
specific to two targets. The numbers, unaffected by the widening above
(operator population is a source-code property, not a test-scope one):
across all 12 targets, exactly 24 mutants total are `boolop`/`unary_not` of
any outcome; 5 of the 12 targets have *zero*; and only 1 of those 24 was
ever both a survivor and reachable (`boltons-typeutils`). A rate needs a
denominator larger than 1.

A second design was considered as a replacement -- hold out a fixed
fraction of reachable survivors by position rather than by operator type,
which would scale with each target's own count instead of being capped by
how rare `and`/`or`/`not` are in the source. Also rejected: per-target
reachable-survivor counts are themselves single digits to low teens even
pooled at 53 (see the widening entry above), so a fixed fraction of a small
number is still a small number -- most targets would still hold out 0 or 1
mutant. It also answers a weaker question than intended: a test that
transfers to a same-operator mutant a few lines away says less about
generalization than a test that transfers to an undisclosed *type* of
mutation, which was the actual point.

**Decision:** no holdout control ships, in either form. Code reverted --
`HELD_OUT_OPERATORS` remains defined in `killcheck/logs.py` only as a
historical record with a comment pointing here; nothing reads it for
scoring. `scripts/verify_targets.py` no longer computes a held-out
partition; `scripts/ablate.py`'s work-queue denominator is now simply "all
reachable survivors." Anti-circularity rests on the assertion taxonomy
instead -- recorded as a real, narrower guarantee in CLAUDE.md and
README.md, not papered over as equivalent.

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

## Baseline arm picker guessed the wrong existing-test file for the eval set's hardest target

`killcheck/baseline.py`'s `existing_test_source()` picks the test file arms
A and B are shown as "existing tests," and where their generated tests get
appended. Before running either arm, its choice was audited by hand against
all 12 targets rather than trusted from reading the code.

Ten of twelve were fine: either the test command names exactly one file, or
a candidate's name contains the module's own stem (`card.py` ->
`test_card.py`). `aiofiles-temptypes` (module `tempfile/temptypes.py`, test
command runs the whole `tests/` directory, 9 candidate files) was not: no
filename contains `temptypes`, so the picker fell back to its last resort --
largest file in scope -- and chose `tests/test_os.py` (16.8 KB). That file
exercises `aiofiles.os`. The module actually being mutated is exercised by
`tests/test_tempfile.py` (5.0 KB, imports `from aiofiles import tempfile`,
tests `TemporaryDirectory` and `AsyncSpooledTemporaryFile` -- the classes
`temptypes.py` defines).

Consequence had this shipped uncaught: both baseline arms would have been
shown irrelevant existing tests on the eval set's designated hard case (the
one target required to be async/I/O-heavy per the eval-set rules), and
their generated tests would have landed in the wrong file. Scoring itself
would not have broken -- the test command runs the whole `tests/` directory
regardless of which file new tests land in -- but the fairness of what the
model saw would have been degraded silently, on exactly the target most
likely to need every advantage a fair prompt gives it.

**Found by auditing the picker's actual choice across all 12 targets before
running anything, not by reading the code.** The same failure class as the
two harness bugs above: a plausible-looking heuristic that is wrong on
exactly one case you have to go looking for.

**Fix:** added a second tier between exact-stem match and the largest-file
fallback -- match on the module's immediate parent directory name
(`tempfile/temptypes.py`'s parent is `tempfile`, which is in
`test_tempfile.py`). Verified this changes only `aiofiles-temptypes`'s pick
across all 12 targets; the ten already-correct picks are unaffected, and the
two single-candidate targets (`voluptuous-error`, `slugify-special`) have
nothing else to choose among either way.

**Also added:** the largest-file fallback now prints a warning naming the
target and the chosen file whenever it fires with more than one real
candidate to choose among -- a degraded pick should announce itself instead
of requiring a hand audit to find, same principle as the canary and
determinism checks. Single-candidate targets are exempted: when a test
command names exactly one file, there is nothing to guess among and nothing
to warn about.

## Smoke test caught a truncation bug, then surfaced a real Arm A failure that isn't one

The `killcheck/baseline.py --arm A --target slugify-special` smoke test
failed clean-pass twice in a row, for two unrelated reasons -- one a harness
bug, fixed before either arm ran; the other real Arm A data, not touched.

**First failure: a truncated response corrupted the whole batch.**
`MAX_TOKENS` was 2000, a placeholder never checked against what the prompt
actually asks for. Arm A's prompt says "write as many tests as you think are
warranted" -- open-ended -- then gave the model 2000 tokens to do it in. On
`slugify-special` the response was cut off mid-statement with no closing
fence (`completion_tokens` landed on exactly 2000). `extract_code()`'s fence
regex found no closed block and fell back to the raw text, which put a
literal ` ```python ` line into `test.py`, turning one incomplete test into
a `SyntaxError` that failed collection for all 20 complete tests generated
alongside it.

**Decision: raise the budget, not bound the prompt.** `MAX_TOKENS` is now
8000, recorded in CLAUDE.md as the per-call figure for **all three arms** --
invariant 3 requires the same budget across arms, so `killcheck/agent.py`
must use the same number when it's built. Weakening Arm A's "as many as
warranted" ask to fit an arbitrary ceiling would have made it a strawman,
which is exactly the failure mode this design is supposed to avoid. Set
before any arm produced a result.

**`extract_code()` fixed to salvage, not inject or discard.** A truncated
response's tail is now AST-parsed with trailing lines stripped one at a time
until it parses or nothing is left, instead of being kept raw. Verified
against the actual truncated `slugify-special` response: recovers all 20
complete test functions, drops only the one incomplete final statement.
Garbage input still returns `""` rather than being injected. One residual,
accepted edge case: a truncation that happens to land on a syntactically
complete but semantically wrong line (e.g. `assert some_name` where the
model meant to reference a different name) will be salvaged as-is -- it can
still fail at runtime, but it costs one test's outcome, not the whole
batch's collection, which is the actual goal.

**Added: a `truncated` flag per call**, true when `completion_tokens ==
MAX_TOKENS`, logged to the trajectory and rolled up into a per-arm count in
`results/baseline_arm_*.json` and Table 1. Truncation is expected to recur
in arm C across its ~53 generate calls; it needs to be visible as itself,
not discoverable only by noticing a mysterious clean-pass failure downstream.

**Second failure, after the fix: real, and left alone.** Re-run, zero
truncated calls, 20 clean parses -- and `clean_pass` was still `False`.
Cause: the model wrote

```python
def test_pre_translations_exact_value():
    expected = [('Ю', 'U'), ..., ('Ϋ́', 'Y'), ...]  # 30 hand-copied tuples
    assert PRE_TRANSLATIONS == expected
```

and got one entry wrong -- `'Ϋ́'` at index 18 is a different Unicode form
(precomposed vs. combining-character sequence) from the real value, visually
identical, byte-different. A snapshot assertion against a hand-transcribed
literal is exactly the kind of thing the `RULES` prompt's "assert on
observable behaviour, not implementation details" instruction was meant to
discourage, and didn't. This is not a harness defect -- nothing in
`baseline.py` needed fixing -- it's Arm A actually failing, which is real
data about single-call unguided generation, kept as-is.

**Decision, binding on all three arms (recorded in CLAUDE.md):** a
generated test that fails on clean source is the arm producing a wrong test.
It is never dropped and never repaired -- hand-fixing a broken assertion
before scoring would erase the exact difference the comparison exists to
measure. It is retried according to each arm's own design and no further:
arm C gets exactly one retry with real pytest output fed back; arms A and B
get none, by design, not oversight.

**The consequence is asymmetric and is not left for a reader to find in the
JSON:** clean-pass is evaluated per batch. One broken test in arm A's
(or arm B's) batch fails collection for every other test generated alongside
it, scoring the target zero no matter how many of the batch's other tests
were good. `slugify-special` is the concrete illustration: 19 of 20
generated tests were fine; one wrong Unicode literal zeroed the target.
Table 1 carries a `clean_pass` column per arm and a per-arm count of targets
that failed it, so this shows up as a count, not something inferred from a
zero.
