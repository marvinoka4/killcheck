# Changelog

Findings and decisions, in the order they happened. Not a commit log --
`git log` covers that. This is for "what did we try, why, what did the number
do, what did we decide."

## Improvement changelog

| STAGE | WHAT WE TRIED AND WHY | EVIDENCE | DECISION / LEARNING |
| --- | --- | --- | --- |
| Frozen core + fixture baseline | Built `engine.py` (AST mutation operators) and `runner.py` (kill/survive execution), verified against a local fixture before touching any real target | `fixture/bank.py`: 21 mutants, killed=2, survived=19, kill_score=0.0952 | Froze both files (CLAUDE.md invariant 5) before any agent code existed; any later change requires re-running both arms and a changelog note |
| Eval set construction | 12 targets from permissively licensed public repos, pinned commit SHAs, 15-60 mutants each | 455 total mutants across 12 targets | Locked in `targets.json` before any metric or arm code existed |
| src-layout bug + canary | 3 of 12 targets scored kill_score=0.0000 -- investigated rather than accepted as "the agent fails on src-layout" | mutated module's `__file__` resolved to the *original* checkout, not the tempdir copy, for the 3 src-layout targets | Fixed with `-o pythonpath=src`; added a standing canary check (unparseable-source mutant must report `survived`=false) before any kill score is trusted anywhere |
| Metric lock | Pooled reachable-survivor kill rate chosen as primary over mean kill score, before any arm ran | n/a -- a design decision, made and committed pre-data | Locked in CLAUDE.md with the "mean kill score is dominated by mutant count" rationale; per-target results as raw counts, never percentages |
| Reachability denominator | Coverage-based reachability partition (killed / reachable-survivor / unreachable) added after 2 targets came back with 0 reachable survivors | `voluptuous-error` 0/0 (coverage's docstring-tracing artifact, confirmed by hand); `dotenv-variables` 0/0 (`test_cli.py` excluded for an unrelated platform reason) | Both targets kept, contribute 0/0, excluded from the pooled denominator rather than swapped for denser targets |
| Widening experiment | Widened every target's `test_command` to the broadest clean-running scope, to test whether narrow scoping explained a thin (54/138) denominator | Pooled reachable survivors moved 54 -> 53 (lower, not higher); 10 of 12 targets showed zero movement | Accepted as a finding -- most undetected faults are unreached code, not weak assertions -- not a defect to engineer around; two ways to inflate the number (target-swap, further widening) rejected on principle |
| Both abandoned holdout designs | (1) operator-based holdout: exclude `boolop`/`unary_not` from the work queue, score anyway. (2) positional holdout: hold out a fixed fraction by index | 24 total `boolop`/`unary_not` mutants across all 12 targets, 4 of 12 have zero, only 1 pooled survivor-and-reachable | Both abandoned before any arm ran -- denominator too thin under either design to support a rate |
| Concurrency bug + determinism gate | `score_target`'s default `workers=4` produced non-deterministic survivor sets on `aiofiles-temptypes` (real async I/O against real temp files) | 4 runs at `workers=4`: kill scores 0.7308/0.7308/0.7692/0.7308, different survivor sets each time; `workers=1`: 0.2692 identically, twice | `workers=1` as the new default; added a standing 3x-serial determinism check, quarantining (not averaging) any target that varies. All 12 targets pass, zero quarantined |
| Arm A (single prompt) | One call per target, unbounded test count, no mutation info, no gate, no retry -- the baseline the challenge brief names | 556 tests from 10 calls; official pooled SKR 9/53 = 0.1698; 6 of 10 targets failed clean-pass | Official 9/53 stands per the never-repaired rule; a repair diagnostic run separately (below) to quantify how much of that is a batch-invalidation floor |
| Arm B (budget-matched) | One-test-per-call, call count = target's reachable-survivor count, same model/tokens, no mutation info, no gate, no retry | 53 tests from 53 calls; official pooled SKR 2/53 = 0.0377; 0 clean-pass failures | Isolates what budget alone buys, holding the one-test-per-call regime fixed against arm C |
| Batch-zero repair diagnostic | Re-scored arm A's 6 failing targets with only the individually-bad content removed, to separate "can't kill" from "batch invalidation hid a kill" | Capability diagnostic 0/26 -> 7/26; instrument correction (`natsort-ns-enum`) 0/2 -> 2/2; `tenacity-stop` resolved to a naming collision (`make_retry_state`), true repaired 1/14 | Diagnostic only, reported alongside -- never instead of -- the official 9/53; its own reconstruction bug (dropped shared imports) was caught by a predicted-outcome sanity check before any repaired number was trusted |
| Arm C partial | One mutant per call, mutant diff in context, execution gate, exactly one retry on failure; ran until the API budget was exhausted | 2 of 10 scoring targets completed (15 of 53 reachable survivors); 9/15 killed, keep rate 60% pooled (57.1% on `tenacity-stop`); 8 of 9 kills call-phase `AssertionError` | Stopped and reported as partial, with the 8 unrun targets and 38 unrun survivors named explicitly; no substitute generator used -- that would not be an identified comparison |

## Full detail, in the order things happened

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

## Classifier was about to be run on batched rows -- caught before the first classification, not after

Before running `scripts/classify_tests.py` over the real arm A/B results,
checked the data it would actually read against the classifier's own
documented unit. `classify_test()`'s docstring says "for a single test
function's source" and returns exactly one category per row it's given,
by walking every `assert` in the row and taking the highest-priority match
(`value > mock > exception > existence > none`). But `killcheck/baseline.py`
logs one row **per target**, not per test -- `test_source` on that row is
the arm's entire generated batch, because clean-pass/kill scoring for arms A
and B happens once per batch, not once per test. Real numbers from this run:
arm A's `toolz-dicttoolz` row alone held 99 test functions in one row.

**The direction of the error matters: it would have flattered both arms, not
penalized them.** Running the classifier unmodified would have reported
`n=10` for arm A ("all generated") instead of the true 556, and each of
those 10 classifications would have been won by whichever single test in a
batch of up to 99 happened to contain a value-comparison assert -- a batch
of 1 strong test and 68 weak ones would classify identically to a batch of
69 strong tests. An error that inflates a result you're about to publish is
exactly the kind of thing that doesn't announce itself; this one was caught
by checking the classifier's documented input shape against the actual
logged data before running it once, not by noticing a suspicious number
after the fact.

**Fix:** `scripts/classify_tests.py` now splits each row's `test_source`
into its individual test functions/methods before classifying (`ast.walk`
over the whole tree, matching both `def` and `async def`, not just
top-level statements). Two more real shapes turned up doing this, also
checked rather than assumed: `tenacity-stop`'s arm A tests were organised as
methods on ~12 `TestXxx` classes (a top-level-only check would have missed
all 62), and every `aiofiles-temptypes` test was `async def` (a bare
`FunctionDef` check would have missed all 31). Verified the fix against
every row's raw `def test_`/`async def test_` count before trusting the
output: exact match on all 20 rows across both arms (arm A: 556 tests
pooled; arm B: 53).

**This fixes the classification unit, not the scoring unit, and cannot --
recorded as a limitation in CLAUDE.md and README rather than glossed over.**
Arms A and B are still scored per batch; there is no record of which
individual test in a multi-test batch caused which mutant to die. So
`gate_would_keep` -- the taxonomy of tests that would clear the kill gate --
is only computed for a batch that split into exactly one test (arm C by
construction; incidentally, any A/B target whose whole batch happened to be
a single test, like arm B's `slugify-special`). For every multi-test batch
it's reported as explicitly not computable, not guessed at by attributing
the batch's outcome to every test inside it.

## Arm A's 9/53 is a floor, not a capability measure -- quantified before designing arm C's prompt

Arm A wrote 556 tests from 10 calls against 53 reachable survivors; arm B
wrote 53 tests from 53 calls. Before treating the A-vs-B gap as evidence
about unguided generation's design, not just its volume, a diagnostic was
run to separate "arm A can't kill mutants" from "arm A's kills are hidden
behind a batch-invalidating test." Full method and numbers below; run as a
scratch script, not added to the pipeline -- it exists to inform arm C's
prompt design, not to be re-run as part of the official metric.

**Arm A's official pooled SKR is 9/53 = 0.1698, with 6 of 10 targets failing
clean-pass.** Per the standing clean-pass rule, that 9/53 stands as the
official result -- a failing test is the arm producing a wrong test, never
repaired for scoring. But 6 of 10 targets contributing zero kills purely
because one bad test invalidated their whole batch means 9/53 measures a
floor, not arm A's ceiling.

**The six failures are not one category, and pooling them would mislead.**
Split into two kinds, because they have different owners:

- **Capability diagnostic** -- `validators-card`, `slugify-special`,
  `shortuuid-main`, `boltons-typeutils`, `tenacity-stop`. In every one of
  these, the model wrote something wrong (a bad assertion, or -- see
  `tenacity-stop` below -- code that collides with the target's own test
  infrastructure). Removing exactly what's wrong and re-scoring answers "how
  much of arm A's capability is masked by batch invalidation."
- **Instrument correction** -- `natsort-ns-enum` only. Zero of its 40 tests
  were wrong. The batch was invalidated by a misplaced `from __future__
  import annotations`, a consequence of this harness's append-only
  augmentation, not of anything the model got wrong about the module under
  test. This does not belong in a table of model failures.

**Of arm A's six clean-pass failures, five were the model writing something
wrong and one was the harness.**

**Capability diagnostic -- per-target result:**

| target | batch size | bad content found | repaired kill / reachable |
| --- | --- | --- | --- |
| validators-card | 59 | 3 wrong assertions | 0/2 |
| slugify-special | 66 | 1 wrong assertion | 1/1 |
| shortuuid-main | 57 | 1 wrong assertion | 4/7 |
| boltons-typeutils | 37 | 1 wrong assertion | 1/2 |
| tenacity-stop | 62 | 1 wrong assertion + 1 colliding helper function (see dedicated entry below) | 1/14 |

Pooled, capability diagnostic: official 0/26 = 0.0000 -> repaired 7/26 =
0.2692.

**Instrument correction -- `natsort-ns-enum`:** official 0/2 -> repaired
(under the fixed harness, see below) 2/2.

**Combined, informational only:** official 9/53 = 0.1698 -> repaired
(capability diagnostic + instrument correction, all six previously-failing
targets resolved) 18/53 = 0.3396 -- roughly double. Neither the 18/53 total
nor the 7/26 capability-only figure is arm A's score. The official 9/53
stands. These exist to show arm C's per-mutant, retry-on-failure design
(never batching many tests behind one shared clean-pass gate) is a direct
structural fix for a failure mode just measured, not merely a different
strategy assumed to be better.

**Reconstruction method, and the bug in the first attempt at it, are
recorded in their own entry below** (the fifth instrument bug found this
session) -- surgical per-test removal on a copy of the batch's own AST,
`from __future__ import` dropped unconditionally from what gets attributed
to any test (see the augmentation fix below).

**Augmentation fix, and checking arm B for the same exposure.** Grepped
both arms' logged output for `from __future__`: **`natsort-ns-enum` under
arm B has the identical exposure** -- same misplaced
`from __future__ import annotations`, same `SyntaxError`, same target.
`killcheck/baseline.py`'s `score_with_added_tests()` appended generated
tests strictly after the existing file's content, which can never satisfy
Python's requirement that `__future__` imports be a file's first
statement, regardless of whether the appended tests are otherwise correct.
Fixed: `hoist_future_imports()` now moves any `__future__` import in the
appended batch to right after the existing file's own leading
docstring/`__future__` imports (found by AST, spliced by exact line span,
so nothing else gets reformatted), verified directly against both arms'
actual logged `natsort-ns-enum` batches -- arm A now scores clean at 2/2,
arm B scores clean at 0/2 (clean, but genuinely kills nothing). **Arm A and
arm B's official results were both produced before this fix exists** in
`killcheck/baseline.py`; their recorded JSON has not been regenerated, and
`natsort-ns-enum`'s official entry still reads clean_pass=False / 0 killed
for both arms.

**Decision, made rather than left open:** do not re-run. The bug is already
correctly attributed to the instrument, not the model (see the
capability-vs-instrument split above), and re-running now would change the
generator's environment after the fact -- a different `baseline.py` than
the one arm A and arm B's other 9 targets were scored under -- for the sake
of one target's number. The repair diagnostic already reports what the
fixed instrument would have shown (`natsort-ns-enum`: 2/2), alongside the
official 0/2, exactly as every other repaired figure in this document is
reported: as a diagnostic, not a silent substitution into the official
result.

## Tenacity-stop: generated tests broke the suite they were added to, through a plain naming collision -- not detectable by the kill gate or the taxonomy

This is a finding, not a loose end, and `tenacity-stop` is this eval set's
largest single denominator (14 of 53 pooled reachable survivors), so it
gets its own entry rather than a footnote.

**Symptom:** after removing arm A's one wrong assertion
(`test_mixed_and_or`), clean-pass still failed. Twelve *pre-existing* tests
in `tests/test_tenacity.py` -- `TestBase::test_callstate_repr`,
`TestWaitConditions::test_wait_exception`, ten under
`TestRetryConditions` -- started failing with `TypeError`s, none of them
part of any of the model's own eleven new `TestStop*` classes. Single-test
removal bisection over the model's other 60 tests found no one-test fix.

**Hypothesis going in:** `TestStopWhenEventSet`, one of the model's new
classes, uses a real `threading.Event()` -- the obvious suspect for
cross-test pollution. Spent under 20 minutes checking it directly:

- Removed all four of `TestStopWhenEventSet`'s tests (the only tests in the
  batch using `threading` at all -- confirmed by grep, 5 of 5 `Event(`
  occurrences are in this one class) and re-ran. **All twelve pre-existing
  failures persisted, identically.** Threading is not the cause.
  **Hypothesis rejected, not left unconfirmed.**

**Actual cause, found by reading what else the batch defines, not just its
tests:** the model's response includes its own top-level helper function,
`make_retry_state(previous_attempt_number, delay_since_first_attempt,
upcoming_sleep=0)` -- a plausible-looking reimplementation of a helper the
existing `tests/test_tenacity.py` *already defines*, with a different
signature (the real one: `make_retry_state(previous_attempt_number,
delay_since_first_attempt, last_result=None, upcoming_sleep=0)`). Appended
after the existing file's content, the model's definition executes second
and silently rebinds the module-level name -- every pre-existing test that
calls `make_retry_state(...)` for the rest of that test session is now
calling the model's incompatible version, not the original. Confirmed
directly: removing *only* the colliding function (zero test removals, all
62 of the model's tests kept, including the one wrong assertion) restores
clean-pass for all twelve pre-existing tests in one shot. Re-adding just
`test_mixed_and_or`'s removal on top of that gives `tenacity-stop`'s true
repaired score: **1/14** (folded into the capability-diagnostic pooled
figure above).

**Why this matters beyond one target:** the kill gate checks one test
against one mutant, in isolation, by design -- that's exactly right for
what it's built to measure, and exactly why it cannot see this. A test
that's individually well-formed, passes clean, and correctly targets its
intended mutant can still corrupt an unrelated part of the same test
session through a shared name, and nothing in this project's kill gate or
assertion taxonomy is positioned to catch that, because both operate on
one test in isolation from everything else already in the file. The only
reason this surfaced at all is that arm A batches many tests into one
append, so a collision has something to collide with; arm C, gating and
scoring one test per mutant, narrows but does not eliminate the exposure
(the existing suite is still present in every scoring run). Recorded here
as an open exposure for arm C's tripwires, not something this session's fix
closes.

## Fifth instrument bug, and the best-caught one: a predicted outcome that came back false

The repair diagnostic's first reconstruction (extract each test function
individually, rejoin them) was wrong: it dropped every batch-level shared
import/constant, spuriously breaking any test that referenced one and
logging it as `NameError` -- not a real defect -- and it broke class-based
tests by extracting a method as a bare function with an unfillable `self`
parameter.

**What actually caught it, worth generalising:** not inspection of the
reconstruction code, and not a suspicious-looking result. A sanity check
with a specific, falsifiable, *predicted* outcome, run before trusting
anything downstream of the new code: `slugify-special`'s one bad test
(`test_pre_translations_exact_value`) was already known, independently,
from the pytest output in the original run. The prediction: remove exactly
that one test from the reconstructed batch, and clean-pass must restore.
It didn't -- the first reconstruction attempt reported 62 of 66 tests still
needed removing, `clean_now=False`. That is a contradiction of a specific
prediction, not a vague feeling that a number looked off, and it was caught
*before* the diagnostic's numbers were used for anything, on the first
target checked, not discovered by auditing all six after the fact.

This is the same mechanism as the canary check and the determinism check,
generalised past the frozen core: don't just run new code and read off
whatever it reports -- run it against a case where you already independently
know the answer, predict the specific outcome first, and treat a false
prediction as a stop-and-investigate signal rather than noise to average
past. Four of the first five instrument bugs found this session
(src-layout imports, concurrent scoring, the classifier's batch-vs-test
unit, this reconstruction) were caught by exactly this shape of check --
run something new against a known answer before trusting it against an
unknown one. The fifth (the `natsort-ns-enum` `__future__` import) was
caught by the capability-vs-instrument split in the entry above, a related
but distinct discipline: asking "whose fault is this failure" before
pooling it with ones that have a different owner.

**Update, after arm C's run: eight instrument bugs total, not five.** The
remaining three did not all fit the predicted-outcome shape, and forcing
them into that bucket would misstate how they were actually found. Bug 6
(`_test_name()` missing class-based tests) was caught by an anomaly during
arm C's smoke test -- both attempts on a legitimate response were being
silently discarded, which prompted looking at what the extractor actually
returned, not a prediction stated in advance. Bug 7 (unittest-style
assertion detection) was caught by reading the actual classification of a
real kept test and noticing it read `none` when it plainly wasn't -- again,
an examined result, not a stated prediction. Bug 8
(`calls_mutated_function` undefined for dunder-dispatched code) was caught
by a suspicious near-zero rate during table-building, which is explicitly
the kind of signal this entry says is *less* reliable than a stated
prediction -- worth naming honestly rather than folding into the same
success story as the other four. The predicted-outcome check remains the
strongest tool of the ones used here; it is not the only one that worked.

## Arm C's smoke test found two more instrument bugs before spending the real budget

`killcheck/agent.py`'s smoke test (`slugify-special`, 1 reachable survivor)
surfaced two bugs, both fixed and re-verified before running any other
target.

**Bug 1: `_test_name()` only scanned top-level statements.** The model's
first real response was a `unittest.TestCase`-based test -- a class with a
`def test_x(self)` method, not a bare top-level function. A legitimate,
common pattern. The extractor found no top-level `def test_*`, returned
`None`, and both attempt 1 and attempt 2 were auto-discarded as "no test
function found" without ever actually being run -- neither had a chance to
pass or fail on its own merits. Confirmed by re-evaluating the captured
attempt-1 response directly after the fix: it does genuinely fail to kill
its mutant (a real construction flaw in the model's chosen test values,
unrelated to the bug), so no valid kill was thrown away here -- but the bug
still fed the retry loop a useless "no test function found" message instead
of the real pytest output, degrading the retry mechanism's actual chance of
correcting the problem. That mechanism -- real failure output feeding a
retry -- is one of the specific things arm C exists to measure, so a bug
that quietly disables it for an entire class of valid responses is not a
minor one. Fixed by recursing into class bodies to find the first
`test_*` def/method, matching `_enforce_one_test()`'s overflow-counting the
same way.

**This is the same recursion-failure shape as the `classify_tests.py` bug
from the arm A/B taxonomy pass, worth naming as a repeated pattern rather
than two unrelated incidents:** an extractor written and tested against the
common case (a bare top-level function) silently drops the valid uncommon
one (a class-based test) instead of erroring loudly. Both times, the
uncommon case wasn't rare in absolute terms -- it's ordinary, idiomatic
Python -- it was just the one shape the extractor's author didn't happen to
write a check for. Worth watching for a third instance of this exact shape
before assuming it's fully closed.

**Bug 2: `classify_test()` had no detection for unittest-style assertion
methods** (`self.assertEqual`, `assertIn`, etc.) -- only bare `assert`
statements and mock-specific `assert_called*` calls. A test using only
`self.assertEqual(...)` classified as `"none"` (zero assertions found)
despite asserting a concrete expected value. Checked arms A and B's
already-reported taxonomy for this exposure before fixing anything:
**zero instances of unittest-style assertions in either arm's logged
data** -- their reported numbers stand unaffected. Fixed in both
`scripts/classify_tests.py` and `killcheck/agent.py`'s own
`mechanical_features()` (which independently re-detects assertions for its
own mechanical-feature fields), sharing one method-name mapping
(`UNITTEST_VALUE_METHODS`/`UNITTEST_EXISTENCE_METHODS`) so the two can't
drift apart the way the bare-assert logic and this logic just did.

Both fixes verified against the actual captured model responses, then the
smoke test re-run clean before proceeding to the rest of the run.

## Arm C's results: 2 of 10 targets, then the API budget ran out

After both smoke-test bugs were fixed, arm C ran `slugify-special` (1
reachable survivor) and `tenacity-stop` (14) in full before the Anthropic
account's credit balance was exhausted on `cachetools-func`'s first call --
a billing failure, not a tripwire, confirmed to have left no partial or
corrupted data for that target (the crash occurred before any logging call
for it). This is the project's main result and it does not get to be a
paragraph buried after the bug list; it is recorded here in full.

**Pooled over the 15 survivors covered: 9 killed, keep rate 60.0% (9 of 15
drafts kept on the final attempt; 57.1% on `tenacity-stop` specifically,
100% on `slugify-special`'s single mutant, not meaningful at n=1).** 9
retries fired (all on `tenacity-stop`), 3 succeeded.

**The gate rejected valid tests that missed, not broken tests.** Of the 6
discarded drafts, 0 failed the clean-source check and all 6 passed clean
but failed to kill their target mutant. Every draft the model produced was
runnable and correct on unmutated code; six of them simply didn't detect
the fault they were shown.

**The pre-registered `none`/`existence` hypothesis is NOT supported on this
sample.** CLAUDE.md's assertion-taxonomy section predicted, before any test
existed to classify, that a meaningful share of gate-passing tests would be
`none` or `existence` class. Kept: 7 `value`, 2 `existence`, 0 `none`, 0
`mock`, 0 `exception`. Discarded: 6 `value`, 0 in every other category. The
`none` bin is empty on both sides of the keep decision, and every discarded
draft is `value` class while both `existence` drafts were kept -- the
opposite of what was predicted. Stated plainly rather than reframed after
the fact, per the discipline CLAUDE.md itself set for this hypothesis
before any data existed.

**8 of 9 kills are call-phase `AssertionError`,** the remaining one a
call-phase other exception. No assertion-free or crash-only kill in this
sample. All 9 kills are `constant` (8) or `return_none` (1) operator family
-- no `compare`, `binop`, `boolop`, `unary_not`, or `raise_removed` mutant
was killed, which is partly population shape (`constant` and `return_none`
are the largest operator families in the eval set) and partly a real limit
on what these 9 kills demonstrate.

**Zero cross-function collateral kills.** 10 total kills across 9 kept
tests (one test killed 2 mutants in its own function), 1 same-function
collateral kill, 0 cross-function. Cross-function collateral was the only
remaining transfer signal left after both holdout designs were abandoned
(see above); it is zero here.

**Per-call gate outcomes and the official one-pass batch rescore agree on
every mutant, on both targets** -- checked explicitly, not assumed, given
that arm A's batch broke `tenacity-stop`'s existing suite through exactly
this kind of interaction (see the naming-collision entry above). Arm C
writes into the same file one test at a time; its kept set ran clean
together with zero mismatches.

**What did not run:** 8 targets, 38 reachable survivors --
`cachetools-func` (11), `aiofiles-temptypes` (8), `shortuuid-main` (7),
`toolz-dicttoolz` (4), `validators-card` (2), `natsort-ns-enum` (2),
`dictdiffer-resolve` (2), `boltons-typeutils` (2). No substitute generator
was used to fill the gap: doing so would not have been an identified
comparison against arms A and B, which ran on the full 53-survivor set.

## Correction: "5 of 12 targets have zero boolop/unary_not mutants" should read 4

Caught auditing CLAUDE.md's numeric claims against `results/` before
reporting arm C's partial-run numbers. Recomputed directly from
`target_verification.json`: 24 total `boolop`/`unary_not` mutants across
all 12 targets (matches), 1 pooled survivor-and-reachable (matches), but
only 4 targets have zero such mutants (`cachetools-func`, `natsort-ns-enum`,
`dictdiffer-resolve`, `tenacity-stop`), not 5. The other two figures in that
sentence were already correct; only the target count was wrong. Corrected
in CLAUDE.md's "Design 1: operator-based holdout" entry.

## Eighth instrument finding: a pre-registered mechanical feature that isn't computable on every target

`calls_mutated_function` (does the draft's AST reference the mutated
function's name) is well-defined for an ordinary function or method: the
name appears as a literal `Name`/`Attribute` in a `Call` node whenever the
test actually invokes it. It is not well-defined for a dunder-dispatched
method. All 14 of `tenacity-stop`'s mutated functions are dunders
(`__call__` x9, `__or__` x2, `__and__` x2, `__init__` x1) -- Python's normal
call and operator syntax (`stop(state)`, `a | b`, `a & b`, `ClassName(...)`)
never spells the dunder name literally, so an AST name-match reads
near-zero on this target by construction, regardless of whether the test
actually exercises the mutated code. Confirmed independently: every kept
test's official-scoring kill was correctly attributed to it via the plugin
(see the per-target kill data), so the tests are doing their job; the
feature just can't see it.

**Fix, and what it does and doesn't mean:** `tenacity-stop` is now excluded
from this one column only (every other mechanical feature, and every other
target, is unaffected). Pre-registering a mechanical feature before seeing
any data protects against tuning the feature to flatter a result after the
fact -- that protection is exactly why this feature was locked in before
arm C ran. It does not, and cannot, guarantee the feature is measurable on
every target's code shape. The honest response to finding a feature is
undefined somewhere is to report it as not computable there, not to force a
number out of it and not to quietly drop the feature everywhere to avoid
the asterisk.
