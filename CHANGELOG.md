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
| Arm B (budget-matched) | One-test-per-call, call count = target's reachable-survivor count, same model/tokens, no mutation info, no gate, no retry | 53 tests from 53 calls; official pooled SKR 2/53 = 0.0377; 3 of 10 targets failed clean-pass | Isolates what budget alone buys, holding the one-test-per-call regime fixed against arm C |
| Batch-zero repair diagnostic | Re-scored arm A's 6 failing targets with only the individually-bad content removed, to separate "can't kill" from "batch invalidation hid a kill" | Capability diagnostic 0/26 -> 7/26; instrument correction (`natsort-ns-enum`) 0/2 -> 2/2; `tenacity-stop` resolved to a naming collision (`make_retry_state`), true repaired 1/14 | Diagnostic only, reported alongside -- never instead of -- the official 9/53; its own reconstruction bug (dropped shared imports) was caught by a predicted-outcome sanity check before any repaired number was trusted |
| Arm C partial | One mutant per call, mutant diff in context, execution gate, exactly one retry on failure; ran until the API budget was exhausted | 2 of 10 scoring targets completed (15 of 53 reachable survivors); 9/15 killed, keep rate 60% pooled (57.1% on `tenacity-stop`); 8 of 9 kills call-phase `AssertionError` | Stopped and reported as partial, with the 8 unrun targets and 38 unrun survivors named explicitly; no substitute generator used -- that would not be an identified comparison |
| Arm C complete | Same design, resumed under a topped-up API budget; pre-run instrument re-verified unchanged, `aiofiles-temptypes` re-checked and unquarantined; remaining 8 targets run, largest denominator first | All 10 scoring targets, 53/53 reachable survivors; 44/53 killed, SKR 0.830, zero clean-pass failures; keep rate 83.0% pooled (6 of 10 targets at 100%, 92.3% excluding `tenacity-stop`); 21 retries fired, 12 succeeded; all 9 discards passed clean and failed to kill -- zero broken drafts; zero cross-function collateral across 44 kept tests; `cachetools-func` 10/11 against arm A's 0/11 from 69 clean tests | Official 44/53 stands; B vs C is the identified comparison (2/53 vs 44/53); the gate filtered almost nothing outside the eval set's designated hard case and never once rejected a broken test -- full writeup below |

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

## Two contradictions between CLAUDE.md and README, caught by cross-doc consistency checking, both fixed

The README (rewritten to the final submission version) had already
corrected two claims CLAUDE.md still made. Both are now fixed in CLAUDE.md
to match.

**1. Anti-circularity did not rest on the assertion taxonomy.** CLAUDE.md's
Metrics section said "anti-circularity rests on the assertion taxonomy: an
arm cannot inflate its apparent effectiveness by writing vacuous tests,
because the taxonomy reports exactly how many of its gate-passing tests are
vacuous." That conflates three separate questions: (a) what the generator
emits, which the taxonomy does answer; (b) whether the gate selects on
anything measurable, which the pre-registered mechanical features answer;
and (c) whether a kept test specifies behaviour independent of the specific
mutation it was shown, which is the actual anti-circularity question and is
**not answered by anything in this submission** -- no holdout survived (see
Abandoned: holdout transfer control). Collateral kills and breadth are weak
probes computed from arm C's own run, not controls, and do not substitute
for (c). Replaced with the three-way split, stated as a correction rather
than silently rewritten.

**2. The pre-registered `none`/`existence` hypothesis was tested and not
supported -- CLAUDE.md never said so.** The hypothesis (a meaningful share
of gate-passing tests would be `none`/`existence` class, meaning the gate
selects differential probes rather than specifications) was written before
arm C existed. Arm C's completed sample (2 of 10 targets, 15 of 53
reachable survivors, stopped by an exhausted API budget -- not random)
contradicts it: kept 7 `value`/2 `existence`/0 `none`; discarded 6
`value`/0 `existence`/0 `none`; 8 of 9 kills call-phase `AssertionError`.
CLAUDE.md's hypothesis section still ended at "if the data contradicts
this, say so plainly" with no record that it had. This is exactly the
failure mode pre-registration exists to prevent -- a prediction stated in
public, then quietly left unconfronted once the data came in. Fixed by
recording the outcome directly beneath the original hypothesis text, which
is left completely unedited: a pre-registered hypothesis that was not
supported is worth more than one revised to match the data, and editing the
prediction itself would have destroyed the thing pre-registration was for.

Both corrections were prompted by the same discipline: checking one
document's claims against another's, not just against `results/` on disk.
The numbers audit two entries ago checked README against `results/`; this
pass checked CLAUDE.md against README and found it hadn't been updated
when README was.

## The determinism gate fired on a fresh clone, against our own committed numbers, three hours before submission

A clean-clone reproduction run of `scripts/verify_targets.py`, done to
confirm the reproducibility claim rather than assume it, reproduced 11 of
12 targets exactly against the committed `results/target_verification.json`:
all 12 pass the canary, 11 produce byte-identical survivor sets across
three serial runs. The twelfth, `aiofiles-temptypes`, did not: survivor set
sizes 19, 18, 19 across three runs, one mutant flipping from survived to
killed in a single run. The gate quarantined it rather than accepting the
2-of-3 majority -- exactly what it exists to do, and exactly what it did
the first time it caught this same target's much larger, concurrency-driven
non-determinism (see "Frozen core reopened a second time" above).

**Not investigated or fixed, on purpose, three hours before the deadline.**
This is the discipline this document has followed throughout: report a
contradiction, don't chase it down or re-run until it agrees, when doing so
would mean shipping a number produced under time pressure with no space
left to verify it properly. Recorded in README's Reproducing section as
what a reader should actually expect (11 of 12 exact, one quarantine), not
smoothed over.

**Why this is worth a full entry and not a footnote:** a determinism check
that has never once fired is indistinguishable, from the outside, between
two very different explanations -- "the harness is reliable" and "the check
doesn't actually catch anything." This one has now fired twice, on the same
target, for two different reasons (concurrent execution the first time,
something else -- unexamined -- the second), against two different sets of
committed numbers, months apart, on two different machines. That is what
distinguishes a check that works from a check that would look identical if
it didn't.

**Effect on reported results: none.** `aiofiles-temptypes` is one of the
eight targets arm C did not run -- it contributes 0 of the 15 reachable
survivors in the head-to-head and 0 of the 9 kills. It contributes 8
reachable survivors to the pooled 53 arms A and B were scored against, so
those pooled figures now carry a one-target reproducibility caveat, stated
plainly in README rather than left for a reader to discover by re-running
this themselves.

## aiofiles-temptypes re-checked before resuming arm C: passed this time

Before spending any of the topped-up API budget, the same three-serial
determinism check that quarantined `aiofiles-temptypes` above was re-run
against it alone, in isolation from the other 11 targets. This time it
passed: survivor set identical across all three runs, 19 survivors each
run -- the same size the third of the three earlier varying runs (19, 18,
19) landed on, not re-run until it matched anything.

No code changed between the quarantine and this re-check -- `engine.py`,
`runner.py`, and the frozen scoring path are byte-identical to the commit
that produced the 19/18/19 result. The prior non-determinism was real (the
gate is not flaky; see "the determinism gate fired..." above for why a
check firing twice for two different reasons is trusted, not dismissed),
and this re-check does not explain or retract it -- it establishes that
whatever caused it is not reproducing right now, on this run, on this
machine. `aiofiles-temptypes` is unquarantined and scored as a normal
target for the remainder of arm C on that basis: one clean pass after one
quarantine, not three, because the instructed decision rule was "passes
three times cleanly," which this run satisfied on its own three serial
runs, not by combining with the earlier quarantine's runs.

## Arm C complete: the remaining 8 targets, all 53 reachable survivors

Before any call: `scripts_demo.py` reconfirmed `kill_score=0.0952`, the
working tree matched the last commit, and `engine.py`/`runner.py`/`agent.py`
(including the pytest plugin string) were confirmed unchanged since the
original 2-target run via `git log` on each file -- `runner.py`'s last
change predates `agent.py`'s creation, `agent.py` has exactly one commit
total. Nothing to reopen, nothing to re-run for comparability reasons.

Ran in the specified order, one target per process, serially:
`cachetools-func` (11), `shortuuid-main` (7), `toolz-dicttoolz` (4),
`validators-card` (2), `natsort-ns-enum` (2), `dictdiffer-resolve` (2),
`boltons-typeutils` (2), `aiofiles-temptypes` (8). A per-target check ran
after every target (batch-vs-per-call agreement, clean-pass-together,
truncation correctness, retry sequencing, absence of "collected 0 items",
the pytest plugin actually firing, retry-body-rename) against
tripwires.md's HARNESS conditions; none fired anywhere. Keep rate varied
50%-100% across the 8, ruling out the GENERATOR COLLAPSE flat-rate
condition without waiting for the full pool.

**Result: all 10 scoring targets, 53/53 reachable survivors, 44/53 official
kills, SKR 0.830, zero clean-pass failures.** Arm A official 9/53 (0.170),
arm A repaired (diagnostic only) 18/53 (0.340), arm B official 2/53
(0.038) -- all three read directly from the original committed JSON,
unmodified and unrecomputed this session; denominators cross-checked
per-target against arm C's and found identical across all 10 (53=53=53
pooled). `cachetools-func`, the target arm A's 69 clean-passing tests
killed 0/11 on, went 10/11 under arm C.

**The pooled 83.0% keep rate is not uniform and should not be read as
one.** Six of ten targets kept 100% (`slugify-special`, `natsort-ns-enum`,
`dictdiffer-resolve`, `toolz-dicttoolz`, `boltons-typeutils`,
`aiofiles-temptypes`); `cachetools-func` 90.9%, `shortuuid-main` 85.7%,
`tenacity-stop` 57.1%, `validators-card` 50.0%. Excluding `tenacity-stop`
the pooled rate is 92.3% (36/39); `tenacity-stop` alone accounts for 6 of
the 9 discards, `cachetools-func`/`validators-card`/`shortuuid-main` one
each -- real discards, not zero, so "the gate rejected almost nothing
outside the hard case" is directionally right but not literally nothing.

**Every one of the 9 not-killed mutants was drafted and discarded by the
gate -- none was a work-queue gap.** Checked explicitly: each target's
drafted-mutant-ID set matches its reachable-survivor-ID set from
`target_verification.json` exactly, all 10 targets. All 9 exhausted both
attempts; every final draft passed clean and simply failed to kill its
mutant (`passed_on_clean=True`, `killed_target=False`) -- not one was a
broken test. 6 on `tenacity-stop` (all `constant`), 1 each on
`cachetools-func` (`constant`), `validators-card` (`return_none`),
`shortuuid-main` (`compare`).

Full T1-T6 tables (per-target, assertion class, mechanical features,
operator family, collateral kills, head-to-head), the breadth distribution,
and the pooled keep rate are in README's Results section, replacing the
2-target section entirely -- see the commit that follows this one.

## Ninth and tenth instances of the same discipline: two figures in a delivered draft that did not match disk, both caught before paste

The Results section for the completed run arrived as a separate drafted
file (`results-section.md`), not written by the same process that produced
the tables above, and it was audited against `results/` before being
pasted into README rather than after -- the same discipline as the eight
instrument bugs, applied to prose instead of code.

**Ninth: the resource-vector table's arm B wall clock read "~12 min".**
Summing `wall_clock_s` across all 12 targets in `results/baseline_arm_b.json`
gives 399.0s = 6.65 min; `baseline.py`'s `run_arm()` starts the timer before
the model-call loop and stops it after scoring, so this is the true
end-to-end figure, not a partial measurement being rounded generously.
Corrected to "~7 min", matching the round-to-nearest-minute convention the
other two rows (A ~10 min, C ~20 min) already used correctly.

**Tenth: "the `none` bin is empty across all 74 drafts" overstated a true
claim about a smaller population.** The 53-mutant final disposition (44
kept, 9 discarded) genuinely has zero `none`-class drafts on either side --
but 2 of the 74 raw attempts were `none`-class on a first try
(`tenacity-stop`'s M-22369c8f, `toolz-dicttoolz`'s M-917ca6a1), both
superseded when a retry, given the real pytest output, produced a
`value`-class kept test instead. Caught by checking assertion class across
every attempt, not just the final one per mutant, after the "n=53 vs n=74"
distinction from the ninth catch prompted checking whether the same
draft-supplied text conflated the two populations elsewhere. It did. The
same wrong "empty across all 74" framing had already been drafted into
CLAUDE.md's hypothesis-outcome update before this check ran, and was
corrected there too before being committed -- the error did not make it
into two files by being caught in one and missed in the other.

Both are the same failure mode this project has now caught nine and ten
times: a plausible-sounding figure, arrived at by estimation or a
population mix-up rather than a direct read of `results/`, checked before
it could become the record rather than after.

## Eleventh: a whole README subsection left describing the 2-target state after arm C completed

`scripts/verify_targets.py`'s "one target does not reproduce
deterministically" writeup, and the reproduction-cost estimate above it,
were not on the step-2 grep list for superseded numbers (they don't contain
the literal strings searched for) and were missed in the first pass. They
said `aiofiles-temptypes` was "one of the eight targets arm C did not run"
and contributed "0 of the 9 kills" -- both false once arm C's completed run
covered it (8/8 official kills, folded into the pooled 44/53). Caught by
reading the full README top to bottom for the numbers audit rather than
trusting the step-2 grep list to be exhaustive; a literal string search
finds only claims that repeat known superseded phrases, not claims that
describe the same stale state in different words. Rewritten to report the
quarantine as real history, the pre-arm-C-resume re-check that passed, and
the standing fact that the gate may fire again on a future clone -- not
smoothed over, but no longer describing a target as unrun that ran.

**Twelfth, same pass:** the eight-instrument-bugs list's bug 8 said
`calls_mutated_function` "would have read as a real near-zero rate on the
target carrying 14 of 15 survivors" -- `15` was the 2-target partial-run
denominator; `tenacity-stop`'s 14 mutants sit inside the pooled 53 now.
Corrected to "14 of the 53 reachable survivors."

**Thirteenth, and the odd one out: not an instrument bug.** This
CHANGELOG's own summary table has claimed arm B had "0 clean-pass
failures" since the table was written (`27f4269`, "CHANGELOG: add summary
table, arm C results entry, two staleness fixes"); `results/baseline_arm_b.json`
has always shown 3 (`natsort-ns-enum`, `shortuuid-main`,
`aiofiles-temptypes`). The underlying data was never wrong -- arm B's real
clean-pass failures were correctly available in `results/` the entire time.
The summary row itself was just never checked against it when written, and
the wrong "0" survived multiple review passes and a prior numbers audit
before this session's grep-and-audit pass caught it. Nothing in `engine.py`,
`runner.py`, `baseline.py`, or `agent.py` was ever wrong here, so this is
not a fourteenth instrument bug -- but it belongs in the same record for the
same reason the other thirteen do: a wrong number that reads as plausible
inside a summary table is exactly the shape of error that reading a
document catches least reliably, and auditing it against disk catches
directly. Fixed in the summary table; see the "Arm C complete" row entry
above.

## A reader-reported "tenth instrument bug" (stale .pyc execution): verified empirically, confirmed real, confirmed not currently exploitable, closed with a second canary

Reported by Vinh Nguyen (dev.to/vinhnguyenthanhdn) -- the pyc staleness
mechanism and the byte-size-preserving canary gap.

A reader raised a specific, mechanistic claim: CPython's default
timestamp-based `.pyc` invalidation keys on source mtime (whole seconds) +
source size, so a mutation that happens to land on the same file size and
the same whole-second mtime as an existing `.pyc`'s header could execute the
stale bytecode instead of the mutated source -- invisible to the existing
canary (unparseable garbage has a different size, which invalidates any
`.pyc` regardless) and invisible to the determinism gate (staleness is
itself deterministic). Investigated per instruction: verify empirically, do
not reason about it. Every step below actually ran.

**The underlying CPython behavior is real.** A minimal synthetic
reproducer -- compile a `.pyc` from `a == b`, overwrite the source with
byte-size-identical `a != b`, force the mtime back to the exact whole
second the `.pyc` was compiled at, import fresh -- executed the *original*
`a == b` semantics from stale bytecode, not the `a != b` on disk. This
reproduced on the first attempt. The threat model is not hypothetical.

**It does not currently reach any scored result in this codebase.** Four
distinct places build a scored tempdir copy -- `runner.py`'s
`_evaluate_one` (frozen), `agent.py`'s `gate_check` and
`official_batch_rescore` (two call sites, including a nested per-mutant
copy taken from an *already-executed* outer copy, which does generate a
real `.pyc` in that outer copy before the nested copytree runs), and
`baseline.py`'s `score_with_added_tests` (which delegates its actual
per-mutant scoring back to `_evaluate_one`, inheriting its protection
rather than needing its own). All four exclude `"__pycache__"` and
`"*.pyc"` via `shutil.ignore_patterns`, read directly from source, not
inferred. Proven, not just read: copying a real target's checkout (19
committed `.pyc` files) through the exact `ignore_patterns` call produced
0 `.pyc` in the destination and 47 real source files, confirming the copy
itself ran rather than silently no-op'ing. Every mutant additionally gets
its own brand-new `tempfile.TemporaryDirectory()` -- never reused across
mutants -- so there is no window for a `.pyc` compiled for one mutant's
source to persist into another's. Checked separately for the three
src-layout targets (`cachetools-func`, `dotenv-variables`,
`aiofiles-temptypes`, all built with `-o pythonpath=src`): a probe import
resolved to the tempdir copy's own path, not the original checkout's, and
zero `.pyc` were reachable there either. `PYTHONPYCACHEPREFIX` /
`sys.pycache_prefix` are unset anywhere in this environment or repo, so
there is no fixed external cache location redirecting compiled bytecode
outside the per-mutant tempdir either.

**An accidental second layer, not one to rely on.** `engine.py`'s mutants
are generated via full-file `ast.unparse()`, which renormalises formatting
throughout the whole file, not just the mutated line -- so `mutant.source`
never has the same total byte length as the raw checked-out original
(confirmed: 0 of all 455 mutants across the eval set do). Even if the
`ignore_patterns` exclusion were somehow bypassed, an accidental size match
between a fresh mutant and a stale `.pyc` header would be unlikely by
construction of this specific engine. This is incidental, not designed,
and a different mutation engine (a surgical single-line text edit instead
of full-file regeneration) would not have this property -- it is not
something to lean on in place of the actual exclusion.

**Closed with a second canary, per instruction, regardless of the
(negative) outcome above.** The existing canary's `GARBAGE_SOURCE` cannot
detect stale-bytecode substitution by construction: unparseable, differently
sized, and would invalidate any `.pyc` on that basis alone even if one were
present. Added `scripts/verify_targets.py`'s `byte_size_canary_check`:
locate a same-length comparison-operator flip (`==`/`!=`, `<`/`>`, `<=`/`>=`)
via `tokenize` (so an occurrence inside a string or comment is never
touched), splice it in directly against the raw source (not through
`ast.unparse`, so total file byte length is trivially preserved), and
assert the suite reports it as not-survived. Ran against all 12 targets:
8 have an eligible operator and all 8 pass (correctly detected); 4
(`cachetools-func`, `natsort-ns-enum`, `voluptuous-error`,
`boltons-typeutils`) have no same-length comparison operator anywhere in
their module and are reported `N/A`, not silently skipped or forced to a
pass. Wired into `main()`'s canary section as a standing check: a future
`FAIL` here aborts before scoring, exactly like the existing canary.

**A comment-only edit to the frozen `runner.py`, not a behavior change.**
Added a comment directly above the `ignore_patterns` call explaining it is
load-bearing for correctness, per the instruction that a line that turns
out to be load-bearing is exactly the kind of thing that gets "cleaned up"
later by someone who doesn't know. The `ignore_patterns(...)` call itself
is byte-for-byte unchanged -- CLAUDE.md invariant 5 requires re-running both
arms only when the measuring instrument's *behavior* changes, and this
changes none. Also added `scripts/test_pyc_exclusion.py`, a regression test
that exercises the real, unmodified `_evaluate_one` (via a `shutil.copytree`
spy from inside `runner.py`'s own namespace, not a reimplementation of its
copytree call) and asserts zero `.pyc` reach the tempdir across all 12
targets' real committed bytecode. Verified the test itself has teeth before
trusting it: a deliberately broken variant of `_evaluate_one` with no
`ignore_patterns` at all let 19 real `.pyc` files through on the same
target -- the test would have caught exactly this.

**A separate, unrelated finding surfaced by this investigation:** calling
`verify_clean()` directly against `dotenv-variables`' `project_root` (as
`scripts/verify_targets.py`'s canary loop does, before any tempdir copy
exists) currently fails in this exact working directory, because this
repo's own `.env` file sits two directories above
`targets/python-dotenv/`, and `find_dotenv()`'s upward search in
`test_is_interactive.py` walks straight into it. Confirmed this does *not*
affect any actual scored result: `_evaluate_one` and every other copytree
call run inside a `tempfile.TemporaryDirectory()` well outside this repo's
directory tree, where `find_dotenv()`'s upward search cannot reach our
`.env` -- the byte-size canary above scored `dotenv-variables` correctly
despite this. It does mean a literal re-run of
`python3 scripts/verify_targets.py` from this persistent working directory,
as opposed to a fresh clone elsewhere, would currently fail
`dotenv-variables`' canary check at the `verify_clean` step before ever
reaching scoring. Reported, not fixed -- out of scope for this
investigation, and not touched. **Fixed properly a few entries down** ("Frozen
core reopened a third time"), once it turned out this would also break a
literal reproduction following README's own documented steps in order, not
just this persistent directory.

## Reader-reported gap: no negative control, so a flattering bug in a case that never surprises us would never trigger debugging

Reported by Ahmet Özel (dev.to/ahmetozel) -- the negative control itself,
and the sharper framing underneath it: pre-registering a prediction does
not protect you when the instrument produces the number you predicted.
Pre-registration (used throughout this project -- the assertion-taxonomy
hypothesis, the metric lock, this very control's own "expected: near zero"
line above) guards against rationalizing a *surprising* result after the
fact. It does nothing for a bug that happens to produce the *expected*
result, because an expected result is exactly the one nobody goes back to
re-derive. Control A is the concrete instance of this in this project's own
run: had some bug made the vacuous suite's kill score land at a tidy 0.0
instead of 0.137, the pre-registered "expected: near zero" would have been
satisfied and the investigation below would never have happened.

Every check this project had built up to this point -- the canary, the
determinism gate, the byte-size canary -- fires on an unexpectedly *low* or
*inconsistent* score. None of them would catch a bug that made a bad score
look *good*: a flattering result never gets investigated, which is the
entire mechanism the "eight instrument bugs" section above already
identified for why bugs survive, applied one level up to the checks
themselves. Built `scripts/negative_controls.py`, two cases engineered so
the harness's answer should be extreme in the direction nothing else
checks.

**Control A -- a suite that cannot kill anything.** `cachetools-func`'s real
suite replaced with one that imports the module, calls all five decorators
once, and asserts nothing beyond existence. Scored through the unmodified
frozen `score_target()`: 7 of 51 killed (0.137), not zero. Investigated
rather than accepted, per the control's own design constraint (a near-zero
score is also what a broken suite produces -- see bug 1). Three
preconditions checked first: clean-pass (true), `coverage`-confirmed actual
execution (37 lines), both canaries still fire correctly on this target
(unparseable: yes; byte-size: `N/A`, `cachetools-func` has no same-length
comparison operator, consistent with the earlier byte-size-canary
investigation). All three held, so the 7 kills were investigated rather
than dismissed: all 7 are `return_none` mutants on lines this suite's exact
call pattern (`maxsize=2`, non-`None`, non-callable) actually executes --
`assert x is not None` is a real, narrow detector for exactly that operator
class, a property already documented for existence-class assertions
elsewhere in this project, not a new mechanism. `kills_outside_return_none
== 0`: zero `constant` or `compare` mutants, 34 of the 51, were counted as
detected by a suite that structurally cannot distinguish them. Not a
harness bug -- the test design was less vacuous than "near zero" implied,
reported as found rather than quietly tightened until the number looked
cleaner.

**Control B -- a mutant that does not exist.** The real arm C
`draft_and_gate` loop run against 5 fabricated original/mutated line pairs
on `cachetools-func`, verified to match the real source, with
`Mutant.source` left as the genuine unmodified clean file throughout. 0 of
5 kept, `killed_target=False` on all 10 attempts (every fabricated mutant
used its retry). One fabricated mutant's attempt 1 failed clean-pass for a
real, unrelated reason, retried, and attempt 2 still correctly scored
`killed_target=False` -- the ordinary retry machinery engaged normally
inside the control rather than needing to be special-cased around it.

Both written to `results/negative_controls.json` with full per-attempt
records, not just the aggregate. Control A is free (no model calls, ~20s)
and is documented as an additional manual step in README rather than wired
into `scripts/verify_targets.py`'s automatic run, alongside control B (5-10
real model calls, which could never be wired into a step this project
promises is free regardless of speed). See README's new "Negative
controls" section, inserted between the instrument-bugs section and
Reproducing this.

## The deferred equivalence audit, done: all 9 survivors hand-labeled, not just 20 sampled

The 20-item stratified manual audit (Limitations, planned before arm C ran,
deferred because labeling all reachable survivors up front wasn't
affordable) turned out cheaper and more targeted once arm C had actually
run: labeling the 9 *specific* mutants arm C could not kill, by hand, with
a concrete argument each -- not a model's unsupported judgement, and never
EQUIVALENT without an argument for why no input distinguishes it.

**7 EQUIVALENT.** All 6 `tenacity-stop` misses are the same thing in 6
different `stop_*` classes: `__call__`'s `retry_state` parameter's type
annotation, `"RetryCallState"` -> `''`. Verified, not assumed: `grep`'d the
whole `tenacity` package and its test suite for
`get_type_hints`/`__annotations__`/`inspect.signature` -- zero matches --
and confirmed `RetryCallState` is imported only inside `if
typing.TYPE_CHECKING:` in `tenacity/stop.py`, so the name does not exist at
runtime at all; even `typing.get_type_hints()` would raise `NameError` on
the *original* annotation. The `@override` decorator only sets
`__override__ = True`, confirmed by reading `tenacity/_utils.py` directly.
No call to `__call__(retry_state)` -- the method's only entry point -- can
observe a difference, because its body is byte-for-byte identical either
way. `validators-card`'s `M-8e984385` (`return False` -> `return None`) is
equivalent for a different, separately verified reason: `card_number` is
wrapped by `@validator` (`validators/utils.py`), whose own logic tests the
inner return with plain truthiness -- confirmed by reading both branches of
`wrapper()` -- so `False` and `None` both launder into the identical
`ValidationError` object; the caller never sees the raw value. Disclosed
rather than omitted: `card_number.__wrapped__('')` *would* distinguish
them, reaching through `@wraps`' internals -- excluded from counting
against equivalence because this project's own agent loop contract already
excludes "asserting on implementation internals" as legitimate test
content, and no legitimate caller of the public API can observe it.

**2 KILLABLE, both with an identified, specific cause -- not a capability
gap.** `cachetools-func`'s `M-d92d6ba3` (default `maxsize` 128->129):
verified empirically in a fresh subprocess that `lfu_cache()(fn)` (empty
parens first) reads 128 on clean source and 129 on the mutant via
`cache_parameters()['maxsize']`. The agent's actual test called
`lfu_cache(lambda n: n)` -- passing the function directly, which hits a
branch hardcoding `LFUCache(128)` literally in the source, unrelated to the
mutated default parameter -- so the model's own test reads 128 under both
clean and mutant regardless of the mutation; verified this too, both
patterns against clean source. `shortuuid-main`'s `M-2537e138` (`uuid()`'s
`pad_length is None` -> `is not None`): verified empirically that
`len(su.uuid(pad_length=30))` is 30 on clean source and 22 (silently
discarding the explicit argument, falling back to `self._length`) on the
mutant -- first attempt at this check used in-process module reload and
gave a false negative (both showed 30) before being caught and redone with
a fresh subprocess per version, the same reload-unreliability lesson this
project has hit before. The agent's actual test called `su.encode(...)`
directly -- a different method with its own separate, unmutated
`None`-check -- and its own code comments describe *`encode()`'s* logic,
not `uuid()`'s; the model appears to have tested the wrong function
entirely.

**The materially stronger claim this buys, stated in CLAUDE.md's own
`X of Y` convention:** not "44 of 53, with an unbounded residue of
maybe-equivalent misses," but 44 of 53 overall, and of the 9 misses, 7 are
provably unkillable -- 44 of the 46 reachable survivors this suite could
possibly kill (95.7%). The primary metric stays 44/53, locked before any
arm ran, per the same discipline that rejected swapping targets to raise
the pooled-53 denominator earlier in this project -- this is reported
alongside it, not instead of it. Written to `results/equivalence_audit.json`
with full reasoning per mutant; new README subsection "Hand-labeled: 7 of
the 9 are provably equivalent"; Limitations' "20-item stratified audit...
did not run" line updated to describe what actually ran instead.

## Frozen core reopened a third time: verify_clean() was sensitive to its caller's on-disk location, not just dotenv-variables' own suite

The `.env`-leak finding surfaced during the pyc investigation, above, turned
out bigger than first scoped. Investigated properly rather than fixed on
the spot, per instruction to say so before touching the frozen core.

**Root cause was not `cwd`.** `find_dotenv()` (default `usecwd=False`)
never calls `os.getcwd()` in the code path this hits -- confirmed by
reading `dotenv/main.py` directly, not guessed. It walks the Python call
stack to the first real (non-`dotenv/main.py`) calling frame and searches
upward from *that frame's file's own path on disk*. For
`test_is_interactive.py`, that frame is the test file itself, physically
located at `targets/python-dotenv/tests/test_is_interactive.py` -- three
directories below this repo's own `.env`. This is also why the test's own
`monkeypatch.chdir(tmp_path)` doesn't help it: cwd is never consulted on
this path. "Running the check from a neutral cwd" -- one of the two fixes
named when this was assigned -- would not actually have worked; the file
itself has to physically live somewhere with no `.env` in its ancestry, not
just the process's working directory.

**Bigger scope than first reported.** The originally-named symptom was
`scripts/verify_targets.py`'s canary loop calling `verify_clean()` directly
against `project_root`. Checked whether `score_target()` -- frozen,
`runner.py`, called by `determinism_check()` and everywhere else scoring
happens -- has the same exposure via its own internal `verify_clean()`
call. It does: confirmed empirically that `score_target()` called directly
against `dotenv-variables` fails with the identical error, independent of
`scripts/verify_targets.py`'s own separate call. A fix scoped to only the
named call site would have left `determinism_check()` failing one step
later on the same target, in the same run. Also confirmed this isn't
specific to this one persistent working directory: README's own documented
reproduction steps create `.env` (step 0, before any numbered step) before
step 4 runs `verify_targets.py`, so a literal reproduction from a genuinely
fresh clone, followed exactly in order, would hit this too -- not just this
session's leftover state.

**The fix: isolate `verify_clean()` itself, once, in `runner.py`.**
Copies `project_root` into a `tempfile.TemporaryDirectory()` first (the
same `ignore_patterns` used by every other copytree call in this codebase)
and runs the existing test command against the copy instead of
`project_root` directly. Fixing `verify_clean()` itself, rather than each
caller separately, means `scripts/verify_targets.py`'s canary loop,
`scripts/negative_controls.py`'s `control_a`, and `score_target()`'s own
internal call all inherit the fix from one change, with no other file
touched. This is not a mutation-isolation change -- nothing here is
mutated -- it is a location-isolation fix, closing a gap in copytree
coverage that existed only because "nothing is mutated here" made a copy
seem unnecessary when the check was first written.

**Verified the fix changes no ground-truth verdict, before trusting it.**
`scripts_demo.py` reconfirmed `kill_score=0.0952` unchanged. Ran the full
`scripts/verify_targets.py` across all 12 targets against the fixed
`runner.py` and diffed every per-mutant record (`mutant_id`, `operator`,
`lineno`, `outcome`, `reachable`) plus every aggregate figure against the
already-committed `results/target_verification.json`: zero mismatches,
`git diff --stat` on the rewritten file shows no changes at all -- byte-for-
byte identical, `dotenv-variables` included, which previously could not
even be checked this way. `dotenv-variables`' canary, byte-size canary, and
determinism check all now pass where they previously either crashed
(`verify_clean` direct) or were never reachable (blocked by the crash
upstream).

**On invariant 5.** This changes `runner.py`, the frozen core, which the
letter of invariant 5 says requires re-running both arms. Reopened
deliberately rather than silently: this specific change alters no kill or
survive verdict for any of the 455 mutants, on any target -- verified
above, not assumed -- so arm A and arm B's official numbers, which depend
only on `target_verification.json`'s content, are unaffected by
construction. Both arms were not formally re-run. Recorded here plainly so
the decision is visible, not smoothed over: the same discipline this
project has applied to every other frozen-core reopening (workers=4->1,
`__future__`-import hoisting) is applied here too, including writing down
what would have needed re-running and why the verification step above
substitutes for it rather than skips it.

## Vinh Nguyen's second finding, sharper than the first: the pyc-exclusion test asserted a proxy, not the property

Reported by Vinh Nguyen (dev.to/vinhnguyenthanhdn) again -- a follow-up on
the stale-.pyc finding above. Verified per instruction, not reasoned about.

**The claim:** `ignore_patterns` protects the copy only while bytecode lands
beside the source. With `PYTHONPYCACHEPREFIX` set, CPython writes compiled
`.pyc` to a separate tree keyed on the copy's absolute path instead -- so
zero `.pyc` arrive in the copy *and* a stale read can still occur, if the
work directory's path is reused across calls. The actual protection depends
on the work directory not being reused, not on `ignore_patterns` alone.

**Reproduced exactly, three branches, Python 3.14.7 (matching the report's
3.14.6):** same `copytree` + `ignore_patterns` call runner.py itself makes,
a byte-size-identical mutant with mtime forced back to match a primed
`.pyc`'s header. No prefix + fixed work path: fresh (`.pyc` lives inside the
copy, gets deleted with it regardless of path reuse). Prefix + the *same*
fixed work path, reused across two calls: **STALE -- 0 `.pyc` in the copy,
wrong behaviour observed anyway.** Prefix + a fresh unique path per call:
fresh. All three landed exactly where the report said they would.

**Checked every temp-copy call site directly, not assumed:** `runner.py`'s
`_evaluate_one` and `verify_clean`, `agent.py`'s `gate_check` and
`official_batch_rescore` (including its nested per-mutant copy, which
copies from an outer copy that has *already* generated real bytecode via
its own clean-pass run before the nested copytree runs), `baseline.py`'s
`score_with_added_tests`, `scripts/verify_targets.py`, and
`scripts/negative_controls.py` -- every one uses
`tempfile.TemporaryDirectory()` or `tempfile.mkdtemp()`, none reuses a
fixed path. No call site found vulnerable under the current design.

**Ran the full harness once with `PYTHONPYCACHEPREFIX` set** to confirm the
empirical answer for our actual configuration, not just the mechanism in
isolation: all 12 targets' canary, byte-size canary, and determinism checks
passed; `results/target_verification.json` diffed against the committed
version -- zero mismatches, `git diff` shows no change to the file at all.

**The important change: `scripts/test_pyc_exclusion.py` rewritten to assert
the property, not the proxy.** The original test asserted `pyc_count == 0`
in the copy -- exactly the assertion that holds in the "prefix + fixed
path" branch above, while the harness is stale. Kept as a secondary
diagnostic only, clearly labeled as insufficient alone. Added
`test_byte_size_mutation_correctly_observed_under_pycache_prefix`: runs a
real, same-byte-length behavioral mutation through the real, unmodified
`_evaluate_one`, with `PYTHONPYCACHEPREFIX` set for the duration, and
asserts the *observed outcome* is not `survived` -- the property that
actually matters. Verified this new test has teeth before trusting it, per
instruction: added
`test_property_check_catches_the_deliberately_stale_case`, which reproduces
the exact reused-fixed-path branch from the reproduction above and asserts
*that* variant comes back `survived` (stale) -- confirming the property
check is capable of catching the failure mode it exists for, not just
capable of passing.

**Extended the load-bearing comment in `runner.py`** to name both
protections explicitly and state that neither is sufficient alone:
`tempfile.TemporaryDirectory()` giving every call a unique, never-reused
path (the one that matters under `PYTHONPYCACHEPREFIX`), and
`ignore_patterns` excluding `__pycache__`/`*.pyc` from the copy itself (the
one that matters without it). Comment-only; `ignore_patterns(...)` and the
`tempfile.TemporaryDirectory(...)` call are byte-for-byte unchanged, so this
does not reopen the frozen core in the behavior-changing sense --
`scripts_demo.py` reconfirmed `kill_score=0.0952`.

**The general lesson, worth recording on its own:** the original test's
assertion (0 `.pyc` in the copy) held under our actual configuration and
would have failed to catch the exact failure mode it was written for, under
a supported, documented Python environment variable we simply hadn't
considered. A proxy assertion can be exactly right for the environment it
was written in and exactly wrong for one input away from it. Asserting the
property -- the actual behavior the harness depends on, not a stand-in for
it that happens to correlate in the cases tried -- costs the same to write
and does not have that failure mode. This is the same shape of lesson as
the predicted-outcome discipline that caught most of this project's
instrument bugs, applied one level up: to the tests that check the
instrument, not just the instrument itself.

## From research artifact to usable tool: a `killcheck` CLI against an arbitrary module, not just the 12 eval-set targets

Requested directly: killcheck only ran against the 12 hardcoded cases in
`targets.json`. To be useful to anyone else it needs to run against an
arbitrary repo with one command. Six steps, in order; this entry covers all
six, reported together since the work was continuous.

**1. CLI entry point.** New `killcheck/cli.py`, three subcommands:
`killcheck score <module.py>` (mutation score + survivor list, grouped by
function, reachable vs unreachable), `killcheck verify <module.py>` (canary,
byte-size canary, determinism gate, reachability -- the instrument-soundness
checks, on one module), `killcheck harden <module.py>` (the full agent loop:
draft, gate, retry, batch rescore, a ready-to-review test file). Target
discovery works from a bare path: walks upward from the module for a project
marker (`.git`/`pyproject.toml`/`setup.py`/`setup.cfg`/`tox.ini`), and
guesses a test command from the module's stem/parent-directory name against
`test_*.py`/`*_test.py` files if `--tests` isn't given. `targets.json` and
every `scripts/*.py` batch tool are untouched -- batch mode is unchanged, this
is a second way in, not a replacement. `engine.py` and `runner.py` were not
touched; the CLI only calls their existing public functions
(`score_target`, `verify_clean`, `generate_mutants`).

**2. Packaging.** New `pyproject.toml`: `console_scripts` entry
(`killcheck = killcheck.cli:main`), dependencies pinned to what this was
built and tested against (`pytest==9.1.1`, `coverage==7.16.0`; `anthropic`
and `python-dotenv` moved to an optional `[harden]` extra, since `score` and
`verify` need neither an API key nor those packages importable at all).
Verified by creating a genuinely fresh venv, `pip install -e .` from a clean
clone, and running the installed `killcheck` against a repo outside the eval
set -- see "where it broke" below; the two real breakages found there were
fixed before this entry was written, not left for later.

No network access in this sandboxed environment, so "a repo outside the
eval set" is a small synthetic project built for this purpose
(`mathutils.py` -- `clamp`/`is_palindrome`/`safe_divide`/`running_total` --
plus a deliberately partial test suite), not a cloned GitHub repo. Stated
plainly since the brief asked for a real external repo and this substitutes
for one; the packaging and import-surface bugs this caught (below) are
about killcheck's own installed layout, not about anything specific to a
real repo's code, so the substitution doesn't weaken what it verified.

**3. QUICKSTART.md.** Install, point it at one module, get a score, read the
output -- no eval-set concepts, no arms, no SKR. README.md stays the research
write-up; both files now link to each other.

**4. Output for humans.** `score` and `verify`'s default output (not
`--json`) is a terminal summary: kill score, killed/timeout/error/survived
breakdown, a reachable-vs-unreachable count, then survivors grouped by
enclosing function with the original and mutated line shown for each,
tagged `[reachable]`/`[unreachable]`/`[unknown]`. Full JSON is still always
written to `--out` (default `./.killcheck/`) regardless of which mode is
used on stdout.

**5. Graceful degradation.** Every assumption that held for the 12
hand-audited eval-set targets and doesn't hold for an arbitrary repo now
produces a specific, actionable error instead of a crash or a silent zero:
no such file, a directory instead of a `.py` file, a module that doesn't
parse, no discoverable test file (with a copy-pasteable `--tests` example),
an *ambiguous* set of test files where the old largest-file-fallback
heuristic (already flagged once in this project's own history --
aiofiles-temptypes picking the wrong file, see the entry above from that
session) would have silently guessed wrong with no hand-audit backstop this
time to catch it, a test suite that fails on clean code (the real pytest
failure is shown, not swallowed), a test command that isn't installed on
PATH (caught at the top of `main()`, not left as a raw `FileNotFoundError`
traceback from inside frozen `runner.py`'s `_run()`), a module with zero
mutable sites, and a target with zero survivors (or zero *reachable*
survivors for `harden`). All nine confirmed by deliberately constructing
that exact condition and running the installed CLI against it, not by
inspection. `--debug` gets the full traceback for anything not on this list.

Ad-hoc runs never touch this repo's own `results/`/`trajectories/` --
verified directly, not assumed: every `score`/`verify`/`harden` invocation
writes only under `--out` (default `./.killcheck/` under wherever the
command was run from), confirmed by running `harden` end-to-end against the
synthetic external repo and checking `git status` on this repo's `results/`
and `trajectories/` came back clean. This matters specifically because a
`pip install -e .` keeps `killcheck/cli.py` living inside this exact
checkout -- getting this wrong would have meant an unrelated project's ad-hoc
run could silently corrupt the eval set's own frozen results.

### Instrument finding, not a packaging chore: `killcheck/agent.py`'s import of `scripts.classify_tests` would have failed for every real install of this package

This is the same shape of bug as "src-layout editable installs silently
defeat mutation testing" above, not a lesser cousin of it -- worth stating
as its own finding rather than burying as item 1 of the list below, because
the mechanism is identical: **an environment assumption that holds where
the code is developed and breaks where it ships, invisible from inside the
checkout precisely because the checkout is where the assumption happens to
be true.** The src-layout bug held because a mutated copy's absolute path
happened to still resolve back to the original checkout; this one held
because `scripts/` happened to sit right next to `killcheck/` on disk in
every environment anyone had actually run this code in so far.

**What happened:** `killcheck/agent.py` (part of the installable
`killcheck` package, needed by `killcheck harden`) did
`from scripts.classify_tests import classify_test, ...` -- a top-level
import of a sibling directory that is not part of the package and was never
declared as one.

**Root cause:** this import resolves today only because `scripts/` is a
namespace package reachable via `sys.path` insertion that happens whenever
code runs from inside this exact git checkout (`agent.py`'s own
`sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` puts the
repo root, and therefore `scripts/`, on the path). A `pip install -e .`
keeps `killcheck/agent.py` living inside that same checkout, so the import
kept resolving in every environment this project had actually tested in --
including, initially, this session's own fresh-venv packaging test, run
from the killcheck repo directory. The failure was invisible until the
fresh-venv `killcheck` binary was run from a *different* directory
(`cd` into the synthetic external repo), which is the ordinary way anyone
would actually use an installed CLI tool.

**How it was found:** not by reading the import statement and reasoning
about it -- confirmed empirically, the same standard this project holds
every other claim to: `python3 -c "import scripts"`, run from outside this
repo's own checkout with `killcheck` pip-installed into that venv,
raises `ModuleNotFoundError`. The first attempt at this exact check gave a
false pass, because it was run with cwd still inside the killcheck
checkout -- Python's `-c` mode puts cwd on `sys.path[0]`, so `scripts/`
resolved locally even though nothing about the *installed package* made it
resolve. Re-run from a directory with no `scripts/` on disk at all gave the
real answer. A packaging check run from inside the project's own checkout
can pass for the same reason the bug exists in the first place; the check
only means something run from outside it.

**Fix:** extracted `classify_test` and its supporting decision procedure
into `killcheck/classify.py` -- part of the installable package, no
dependency on `scripts/` at all. Verified AST-identical to the pre-move
original, function by function, before trusting it (see below).
`scripts/classify_tests.py` now imports from there instead of defining
these itself; its own batch-analysis behavior (reading
`results/generated_tests.jsonl`, writing `results/assertion_taxonomy.json`)
is unchanged. The same problem, same fix, for `scripts/verify_targets.py`'s
checking primitives -- see item 2 below.

**Generalized into a permanent check, not just a one-time fix:** the fresh-
venv-plus-outside-the-checkout packaging test that caught this (see
"Packaging" above) is exactly the kind of check that would catch a future
regression of the same shape -- a new `killcheck/` module reaching for
anything under `scripts/`. Worth running again after any future change to
what `killcheck/*.py` imports, the same way the canary in the src-layout
entry above is re-run for every new target rather than trusted once and
forgotten.

**Decision:** no `scripts/` dependency belongs in `killcheck/`, ever --
that boundary is now what makes `pip install -e .` correct for `harden` at
all, not just a cleanliness preference.

**Where it broke, in full (the actual ask -- "report where it breaks on a
repo outside the eval set; that list is the real work"):**

1. The instrument finding above: `killcheck/agent.py` importing from
   `scripts.classify_tests`. Fixed via the `killcheck/classify.py`
   extraction described above.
2. Same problem, same shape, for `scripts/verify_targets.py`'s
   `canary_check`/`byte_size_canary_check`/`determinism_check`/
   `measure_reachable_lines`/`build_mutant_records`/`summarize_reachability`/
   `outcome_breakdown` -- the CLI's `verify` command needs exactly these, and
   they lived only in `scripts/`. Extracted into `killcheck/verify_core.py`,
   same AST-identical verification discipline, `scripts/verify_targets.py`
   now imports from there; its own `main()` (the eval-set batch entry point)
   is untouched and confirmed unchanged the same way.
3. Caught immediately by re-running the existing suite before calling
   either extraction done: forgot to re-export `byte_size_preserving_mutation`
   from `scripts/verify_targets.py` after moving it -- broke
   `scripts/test_pyc_exclusion.py`'s collection (it does
   `from verify_targets import byte_size_canary_check,
   byte_size_preserving_mutation`, i.e. imports by name off that module).
   Fixed by adding it back to the re-export list with a comment explaining
   why it's there despite nothing in `verify_targets.py` itself calling it
   directly, so a future "unused import" cleanup doesn't silently reintroduce
   this.
4. `pip install -e .` writes a `killcheck.egg-info/` build-metadata
   directory into the source tree itself, regardless of which venv's `pip`
   ran it -- not excluded by the existing `.gitignore`. Added `*.egg-info/`,
   `.killcheck/`, `build/`, and `dist/`.
5. Found while sanity-checking the `classify.py` extraction (its output
   should be byte-identical to what's committed, since neither
   `classify_test` nor `results/generated_tests.jsonl` changed): the
   *committed* `results/assertion_taxonomy.json` was itself stale, predating
   arm C's run entirely. Unrelated to this refactor -- fixed separately, see
   "The stale `results/assertion_taxonomy.json`" below.
6. Noted, not changed: `killcheck/verify_core.py`'s `measure_reachable_lines`
   (moved verbatim, so this predates this session) invokes the literal
   command name `"python3"` for its `coverage` subprocess rather than
   `sys.executable`. Degrades gracefully today -- coverage measurement
   returns `None`, the CLI reports reachability as `UNKNOWN` rather than
   crashing or reporting a silent zero -- but would misbehave on a system
   where only `python` is on PATH. Left alone rather than folded into a
   "pure code motion" refactor, since fixing it is an actual logic change
   that deserves its own verification pass, not a rider on this one's
   zero-behavior-change guarantee.

**Verification, not assertion, for the two extractions specifically:**
`ast.dump()`-compared every moved function's parsed source against the
pre-move original (`git show HEAD:...`) before trusting either move --
`outcome_breakdown`, `canary_check`, `byte_size_preserving_mutation`,
`byte_size_canary_check`, `determinism_check`, `measure_reachable_lines`,
`build_mutant_records`, `summarize_reachability`, and `scripts/
verify_targets.py`'s own `main()` for the first move; `classify_test`,
`_classify_expr`, `_touches_mock_attr`, `_is_mock_assert_call`,
`_is_pytest_raises`, `_is_unittest_assert_raises`, every constant, and
`scripts/classify_tests.py`'s own `_split_tests`/`main()` for the second --
all reported IDENTICAL. Then ran the full 12-target `scripts/
verify_targets.py` end to end (all canary, byte-size canary, and
determinism checks passing, all 12 targets scored, 53 pooled reachable
survivors) and diffed the resulting `results/target_verification.json`
against a backup taken before the refactor: **zero mismatches, byte for
byte**. `git diff --stat results/target_verification.json` against the
committed version shows no change to the file at all -- the same
empirical-diff discipline this project has used for both prior frozen-core
reopenings, applied here even though `verify_targets.py` was never frozen,
because its output feeds the primary metric's denominator. `scripts_demo.py`
reconfirmed `kill_score=0.0952` throughout. The extraction changed zero
ground-truth verdicts.

**End-to-end validation against the synthetic external repo, with a real
model call:** `killcheck score`/`killcheck verify` ran clean with no API key
needed. `killcheck harden --max-survivors 4` attempted all four reachable
survivors: two comparison-boundary mutants on `clamp()` (`<`/`<=` and
`>`/`>=`) turned out to be genuinely EQUIVALENT for that specific function
-- verified by hand, not assumed, since both branches return the same value
(`hi`/`lo`) at the boundary either way -- and were correctly discarded after
both attempts, never force-kept. The other two (`is_palindrome`'s
space-stripping constant, `safe_divide`'s `b == 0` boundary) were correctly
killed and kept on the first attempt. Official batch rescore: 2 of 4 now
killed, matching the kept count exactly. A ready-to-review test file was
written to `.killcheck/mathutils_killcheck_tests.py`.

## The stale `results/assertion_taxonomy.json`, found incidentally, fixed on its own

Surfaced as a side effect of the `killcheck/classify.py` extraction above,
not caused by it: the committed `results/assertion_taxonomy.json` was last
written in the "Arm A and B results, generated tests, trajectories" commit
(31 Aug), which predates arm C's run entirely. It had only `"A"` and `"B"`
keys. `results/generated_tests.jsonl` -- the file this report is computed
from -- has held arm C's full log since arm C finished; nothing about
`classify_test`'s decision procedure changed (confirmed AST-identical, see
above). The committed JSON simply never got regenerated after the input it's
derived from grew a third arm.

**Decision: regenerate, not delete.** The underlying data
(`results/generated_tests.jsonl`) is complete and correct, `classify_test`
is unchanged, and `scripts/classify_tests.py` is a pure, deterministic
function of that input -- there is no reason to remove a report that can be
correctly reproduced on demand. Ran `python3 scripts/classify_tests.py` and
committed the result.

**Checked before trusting it:** the `"A"` and `"B"` sections of the
regenerated file are byte-identical to the committed ones -- confirmed by
direct comparison, not assumed from "nothing should have changed." Only a
`"C"` section was added (51 lines, pure insertion, `git diff --stat` confirms
no lines removed or altered elsewhere in the file). Arm C's `gate_would_keep`
bucket -- 38 `value` / 6 `existence` / 0 `none` / 0 `mock` / 0 `exception`,
44 total -- matches CLAUDE.md's own stated "Final disposition" figure
(CLAUDE.md's Assertion taxonomy section, "kept 38 `value` / 6 `existence` /
0 `none`") exactly, and matches the `value`/`existence`/`exception` "kept"
column of README's Table 1 kept-vs-discarded breakdown exactly.

**Grepped README.md and CHANGELOG.md for every figure shaped like this
file's output** (`38 value`, `6 existence`, counts and percentages in the
`gate_would_keep` shape) before concluding nothing downstream needed a
correction: every match found was already the arm-C-inclusive number --
someone had computed it correctly by hand or via a one-off run at the time
arm C finished, and simply never re-saved the backing JSON artifact to
match. Same shape as the CHANGELOG arm-B "0 clean-pass failures" catch
earlier in this project: the prose was right, the derived-data file sitting
next to it was wrong. The one figure that looked adjacent but isn't sourced
from this file at all -- README's "discarded" column (8 `value` / 1
`existence` / 0 `none`) -- is a different computation entirely (this
script's `gate_would_keep` only reports the *kept* bucket; there is no
"discarded" breakdown in its schema), so it was left alone correctly, not
overlooked.

**The general point:** a derived-data artifact that isn't regenerated as
part of the same step that changes its input silently stops being ground
truth for anything that later cites it, even while every citation of it
elsewhere happens to still be correct by luck of having been computed
independently at the right time. Caught here only because an unrelated
refactor happened to re-run the generator; nothing about this project's own
workflow re-runs `scripts/classify_tests.py` automatically after an arm
finishes. Worth a standing habit, not just this one fix: regenerate derived
`results/*.json` artifacts as part of finishing a run, not on discovery
months later.

## `measure_reachable_lines`: hardcoded `python3` -> `sys.executable`

Noted but deliberately not changed in the extraction commit above, since it
was an existing behavior being moved verbatim, not something to fold into a
"pure code motion" change without its own verification pass. Fixed on its
own, as flagged.

**Why this one matters more than a typical hardcoded-binary-name nit:**
`measure_reachable_lines` doesn't crash or error out when `"python3"` isn't
on `PATH` -- it degrades. `subprocess.run` with a missing executable raises
`FileNotFoundError`, which the surrounding `try`/`except
(TimeoutExpired, FileNotFoundError, JSONDecodeError)` catches and turns
into a plain `None` return, which `killcheck score`/`verify`/`harden` all
already render as `reachability: UNKNOWN` -- not a crash, not a silent
zero. But UNKNOWN reads as "coverage measurement doesn't work for this kind
of project" (a harness limitation), when the real cause would have been
"the wrong interpreter name for this environment." A stranger with only
`python` (not `python3`) on `PATH` -- true of some Windows installs, some
minimal containers, and increasingly common `pyenv`/`uv`-managed
environments that don't always symlink both names -- would get every
single survivor reported UNKNOWN and no indication why, which is a worse
failure than an outright crash: a crash gets debugged, a plausible-looking
"can't measure this" doesn't.

**Fix:** `["python3", ...]` -> `[sys.executable, ...]` at both of this
function's two subprocess call sites (the `coverage run` and the
`coverage json` steps). `sys.executable` is always correct: it's the
literal path to the interpreter currently running this code, not a name
that has to be resolved on `PATH` at all, so it's a strict improvement with
no new failure mode.

**Verified the same way as the extraction itself, not assumed correct
from "it's just a rename":** ran the full 12-target `scripts/
verify_targets.py` a third time with the fix applied, and diffed the
resulting `results/target_verification.json` against the same pre-refactor
backup used for the extraction's own verification -- zero mismatches, byte
for byte. This eval set's own environment always had `python3` on `PATH`
(so the bug was never observable here), which is exactly why the fix needed
verifying against real output rather than trusted on inspection alone: a
change to code no failing test exercises needs the same empirical check a
passing test would have forced, not less.

## Field test: killcheck against three real repos outside the eval set

The synthetic external repo used to verify packaging (see "From research
artifact to usable tool" above) inherits this project's own assumptions by
construction -- it cannot tell us what breaks on a stranger's code. Also
worth correcting here: the "no network" conclusion behind building that
synthetic repo was itself wrong. The check that produced it
(`timeout 10 curl ...`) failed because `timeout` isn't installed in this
sandbox, not because of an actual network problem -- the compound command's
`||` fallback printed "NO NETWORK" for the wrong reason, uncaught because
stderr wasn't redirected. Network was reachable the whole time (confirmed
directly: `git ls-remote`, a plain `curl` to PyPI, and DNS resolution all
worked once `timeout` was removed from the check). Recorded here plainly
rather than left to stand uncorrected.

Three real repos were cloned and tested with network access: `pytest-dev/
pytest` (nested conftest.py hierarchy -- its own test suite is the feature
it tests), `python-attrs/attrs` (src-layout), `python-jsonschema/jsonschema`
(chosen for heavy parametrization; turned out to be unittest.TestCase-heavy
instead -- see below). All three MIT, none in targets.json. Killcheck was
pip-installed into a fresh venv per repo; `score`/`verify` only, no
`harden` (no model calls, no API credit spent).

**pytest** (`pytest-dev/pytest` @ `3fd8675d`, module `src/_pytest/scope.py`,
auto-discovered test `testing/test_scope.py` -- correct match, first try).
`killcheck score` completed and reported **kill_score = 0.0000 (0/35), with
no warning anything was wrong.** `killcheck verify` on the identical target
correctly caught this: canary FAIL, "mutations are not reaching this
target's test process." Confirmed by direct reproduction: the same
src-layout bug already in this CHANGELOG's own "src-layout editable
installs silently defeat mutation testing" entry -- the editable install
resolves back to the original checkout, not the tempdir copy. The standard
workaround (`-o pythonpath=src`) does NOT fix it here, unlike attrs below:
`_pytest.mark.structures` imports `_pytest.scope` while pytest is still
bootstrapping itself, before pytest's own ini-option-based path insertion
(which only applies at collection time) ever runs. A `PYTHONPATH`
environment variable set before the interpreter starts does fix it --
confirmed by direct reproduction (a garbage-source mutation is correctly
detected once `PYTHONPATH` is set as a real env var, not a pytest option).

Two further findings here are self-hosting-specific, not general: shallow
clone (`--depth 1`) broke setuptools-scm's version derivation, so pytest
saw itself as `0.1.dev1` and failed its own `minversion = "2.0"` check --
fixed by a full clone; `pip install -e ".[dev]"` fails on pytest's own
checkout with a dependency-resolution conflict, entirely independent of
killcheck, caused by `dependency-groups.dev` self-referentially declaring
`"pytest[dev]"` as one of its own requirements.

**attrs** (`python-attrs/attrs` @ `8f767776`, module `src/attr/
converters.py`, auto-discovered test `tests/test_converters.py` -- correct,
first try). Same src-layout canary FAIL as pytest, but here `-o
pythonpath=src` DOES fix it (attrs doesn't import itself to bootstrap its
own test runner the way pytest does). With the workaround: canary PASS,
byte-size canary PASS, determinism PASS (17 survivors, stable x3),
`killcheck score` in 23.6s -- 40/57 killed (0.702), 10 reachable / 7
unreachable. Two of those seven "unreachable" survivors are provably wrong
under a wider test scope, confirmed by grep, not assumed: `optional()`'s
annotation-setting lines never execute under `test_converters.py` alone,
but `tests/test_annotations.py` -- a different file, invisible to
single-file auto-discovery -- directly asserts on `.__annotations__` values
that depend on exactly those lines running. This is finding 3, left open
below.

**jsonschema** (`python-jsonschema/jsonschema` @ `865c27fc`, module
`jsonschema/_utils.py`, auto-discovered test `jsonschema/tests/
test_utils.py` -- correct, first try). Chosen for "heavy fixtures or
parametrised tests"; turned out to have zero `pytest.mark.parametrize`
anywhere in its own test suite and zero `conftest.py` files in the whole
repo -- its fixtures are exclusively `unittest.TestCase`/`setUp`, not
pytest's. Recorded honestly rather than silently reframed: the prediction
was wrong, though the repo still qualifies under the "heavy fixtures" half
of the criterion, and killcheck needed no special handling for
`TestCase`-style suites (score/verify only run the whole test command as a
subprocess and check exit codes -- they don't care about internal test
structure). `killcheck verify`: clean suite PASS, canary PASS,
**byte-size canary FAIL -- "possible stale-bytecode execution" at
`_utils.py:97`.** Investigated rather than trusted: no `.pyc` for this file
existed before the run (nothing to be stale from), and a direct `coverage
run` against `test_utils.py` alone shows line 97 in the Missing set --
`extras_msg` (the flagged line's function) is used only by `_keywords.py`/
`_legacy_keywords.py`, never by `test_utils.py`. The FAIL was real (the
mutation did survive) but the stated cause was wrong -- an unreached-line
false attribution, not staleness. This is finding 2, fixed below.
`killcheck score` (score never calls the byte-size canary -- see finding 1)
completed in 67.3s: 40/146 killed (0.274), 92 unreachable / 14 reachable --
the same single-file-scope undercounting as attrs, more pronounced.

**Auto-discovery's file-matching was correct 3 for 3** -- no wrong guesses,
no ambiguity errors triggered, across three genuinely different repo
layouts. Wall clock was reasonable everywhere a real result came back
(20-78s, nowhere near "twenty minutes"). `.killcheck/` output stayed
correctly isolated to each target repo in all three cases -- confirmed
directly, not assumed, by checking this repo's own `results/`/
`trajectories/` came back clean via `git status` after each run.

**Prioritised findings, as requested, before any fix:**

*Genuinely broken:*
1. `killcheck score` runs no canary at all -- `verify` catches the
   src-layout failure every time, `score` (the command QUICKSTART.md puts
   first) does not, and returns a confident, wrong `0.0000` in silence.
   `harden` has the identical gap (confirmed by code inspection, not run,
   to avoid spending API credit on a target already known to be broken) --
   worse there, since it would spend real model calls chasing survivors
   that can never be killed.
2. `byte_size_canary_check` doesn't check reachability before asserting
   non-survival means staleness -- confirmed wrong on a real target
   (jsonschema, above). Lives in `killcheck/verify_core.py`, inherited
   unchanged from `scripts/verify_targets.py`'s original design, so this
   affects the eval set's own instrument too, not just the CLI -- never
   triggered there because the hand-curated eval set happened to avoid it.
3. Auto-discovery's single-file test scope materially undercounts
   reachability (attrs, jsonschema above) -- the exact effect CLAUDE.md's
   own "widening the suites" exercise corrected for in the eval set, with
   no equivalent in the CLI.

*Awkward-repo-specific (pytest self-hosting only -- see per-repo section
above for which):* the `[dev]` extras conflict, the shallow-clone version
issue, `-o pythonpath=src` not working for pytest specifically.

*Works but poor UX:*
4. Canary failure messages named that something failed but not why or what
   to try, despite this project's own CHANGELOG already documenting the
   src-layout fix.
5. QUICKSTART.md's own `--tests` example used a bare `pytest`, not
   `sys.executable -m pytest` (what auto-discovery itself uses). This bit
   the tester directly during this exact field test -- their shell's PATH
   resolved bare `pytest` to a different venv's binary than the one
   killcheck was installed into, silently.

Findings 1, 2, 4, 5 fixed in the entries immediately below, in that order,
per instruction. Finding 3 is a design decision, not a patch -- left open,
options reported separately, no code changed for it in this pass.

## score and harden must not run without a canary

**Finding 1 from the field test above, highest priority.** `killcheck
score` returned `kill_score = 0.0000` on pytest's `src/_pytest/scope.py`
with no warning at all -- the src-layout bug the canary exists to catch,
silently shipped to a user as a confident number. `verify`, run against the
exact same target, caught it correctly. The canary works; it simply wasn't
wired into the command people run first, and QUICKSTART.md itself puts
`score` ahead of `verify` in its own walkthrough.

**Fix:** `score` and `harden` both now run `canary_check` (the same
unparseable-garbage canary `verify` already ran) immediately after
`verify_clean`, before any mutation work -- and for `harden`, before the
API client is even constructed, so a canary failure never spends a model
call. On failure, both ABORT with an error (no number returned, no test
drafted) rather than proceed. `--skip-canary` overrides this for a user who
has already confirmed the canary separately, or is deliberately
investigating a known-bad target -- both the terminal output (top AND
bottom, so it's hard to scroll past) and the written JSON
(`"canary_verified": false`) say loudly that the result is unverified, not
just a flag name nobody re-reads.

**Verified against the exact case that exposed the bug:** `killcheck
score` on pytest's `scope.py` now aborts with an actionable error (see the
next entry) instead of returning `0.0000`. `--skip-canary` still produces
the same `0/35` as before, now with the warning printed twice, confirming
the skip path is a deliberate, visible override, not a silent behavior
change for anyone who was already passing it.

## Canary failure messages must name the likely cause and the fix, plus a --tests-env passthrough for the case that needs it

**Finding 4 from the field test above.** "canary failed -- mutations are
not reaching this target's test process" is true and tells a user nothing
to try next, despite this project's own CHANGELOG already documenting the
exact mechanism and fix for the most common cause.

**Fix:** every canary-failure path (`verify`, and the two new ones in
`score`/`harden` above) now raises a shared, detailed message: names
src-layout-plus-editable-install as the common cause, suggests adding `-o
pythonpath=src` to `--tests`, and states plainly that this does not always
work -- pytest testing itself is the confirmed counterexample, because
`_pytest` imports itself while pytest's own test runner is still
bootstrapping, before its collection-time path option ever applies (see
the field-test entry above for the direct reproduction). For that case, the
message now points at a new flag:

**`--tests-env KEY=VALUE`** (repeatable, on all three subcommands): sets a
real process environment variable before the interpreter starts, e.g.
`--tests-env PYTHONPATH=src`. This is the one thing that fixes pytest's
self-hosting case, confirmed directly -- an ini option only ever affects
pytest's own collection-time path logic, too late for a target that has
already imported itself before collection begins; a real environment
variable is visible to the very first import, which is not too late.

**Why this needed no change to the frozen core.** `runner.py`'s `_run()`
(and every other `subprocess.run` call in this codebase) never passes
`env=` explicitly, so it always inherits whatever the CALLING process's
`os.environ` is at the moment the subprocess starts. `--tests-env` just
sets `os.environ[key] = value` once, early, inside the CLI's own process,
before any of the frozen functions are called -- every subsequent
subprocess call in the same run inherits it automatically. This is the
exact mechanism this project's own `PYTHONPYCACHEPREFIX` tests already
rely on (`scripts/test_pyc_exclusion.py`, setting the env var before
calling `_evaluate_one` directly); `--tests-env` is that same mechanism,
exposed to a user instead of hardcoded to one test. No frozen-core change,
no new parameter threaded through `runner.py`'s signatures.

**Verified end to end against the exact case that motivated it:**
`killcheck verify src/_pytest/scope.py --tests-env PYTHONPATH=src` on
pytest -- canary PASS, byte-size canary PASS, determinism PASS (12
survivors, stable x3), reachability OK (9 reachable / 3 unreachable),
`kill_score = 0.6571`. Pytest went from a silently wrong `0.0000` (before
this pass's fixes) to a correctly-refused abort (finding 1's fix) to a
real, trustworthy number, entirely via `--tests-env` -- no code change
needed beyond exposing the mechanism.

## byte_size_canary_check must check reachability before asserting staleness

**Finding 2 from the field test above.** `byte_size_canary_check` picked
the first same-length comparison-operator flip found anywhere in the
module and asserted that its non-detection meant "possible stale-bytecode
execution." On jsonschema's `_utils.py`, this produced a confidently WRONG
diagnosis: the flagged line (`extras_msg`'s `==`, line 97) is never
executed by `test_utils.py` at all -- confirmed directly via `coverage run`
showing it in the Missing set, and by grep showing `extras_msg` is only
called from `_keywords.py`/`_legacy_keywords.py`, never from the test file
in scope. The mutation genuinely survived, but for the mundane reason that
its line never ran, not because of stale bytecode. No `.pyc` for this file
existed before the run either -- there was nothing to have gone stale from.

This lives in `killcheck/verify_core.py`, inherited unchanged from
`scripts/verify_targets.py`'s original design (see the earlier "reader-
reported... stale .pyc execution" entries) -- so it is an instrument
finding, not a CLI-only one: it affects the eval set's own published
harness too, just never triggered there, because the 12 eval-set targets
were hand-curated and widened specifically to maximize reachable coverage
(see CLAUDE.md's "Widening the suites"), which happened to always leave a
reachable same-length operator for this check to land on. **Same shape as
the findings this check itself was built to catch: an earlier version of
this exact project asserted non-survival as evidence of something specific
(staleness) without first ruling out the mundane explanation (the line
never ran) -- exactly the discipline `byte_size_canary_check` exists to
enforce on the mutation-scoring path, applied here one level up, to the
check itself.**

**Fix:** `byte_size_canary_check` now calls `measure_reachable_lines`
first and only selects a same-length mutation site on a line the clean
suite actually executes (coverage-confirmed), via a new `reachable_lines`
parameter on `byte_size_preserving_mutation` (optional, defaults to `None`
-- `scripts/test_pyc_exclusion.py`'s own reproduction still calls it
unfiltered, deliberately, since it needs any eligible mutation regardless
of what the suite covers). Three outcomes, no longer conflated into two:
a reachable site survives -> `FAIL`, staleness diagnosis now warranted,
not assumed; eligible operators exist but none are reachable -> `N/A`,
says so explicitly, no diagnosis made; reachability itself couldn't be
measured -> `N/A`, says that explicitly too, distinct from the other two
reasons in `detail`.

**Re-ran all 12 eval targets and diffed against the committed
`target_verification.json`: byte-identical, zero mismatches** -- same
empirical-diff discipline as every prior change to this shared checking
logic. Also diffed the byte-size-canary section's own PASS/FAIL/N/A output
line by line against the pre-fix run: identical -- same 8 `PASS` (same
lines, same columns, same operators), same 4 `N/A` ("no same-length
comparison operator found in this module"), zero `FAIL` either before or
after. The fix changed zero verdicts on the hand-curated eval set and
correctly changed jsonschema's verdict from a false `FAIL` to an honest
`N/A` on the one real, uncurated target that exposed the gap.

**The general lesson, worth recording on its own, same shape as the
pyc-staleness lesson above:** a check that asserts a specific diagnosis
(staleness) from a single observation (non-detection) without first
eliminating a more mundane explanation (unreached code) can be exactly
right for every case it happened to be tested against and exactly wrong
for the first uncurated one it meets. This is the same "assert the
property, not a proxy that merely correlates with it" discipline recorded
earlier in this project, applied to a different check: non-survival
correlates with staleness only once reachability is no longer a competing
explanation, and establishing that costs one extra coverage measurement,
not a fundamentally different design.

## QUICKSTART's --tests example must use `python3 -m pytest`, not bare `pytest`

**Finding 5 from the field test above.** QUICKSTART.md's own `--tests`
example used a bare `pytest`, resolved via `PATH` -- not `sys.executable -m
pytest`, what auto-discovery itself uses internally. This bit the tester
directly during the field test above: their shell's `PATH` resolved bare
`pytest` to a different venv's binary (this repo's own `.venv/bin/pytest`)
than the one killcheck was actually installed into, silently -- no error,
just a different environment's `pytest` running against the wrong
site-packages. That is the best possible evidence this will bite a real
user, not a hypothetical one.

**Fix:** QUICKSTART.md's example now reads `--tests "python3 -m pytest
tests/test_somemodule.py -q"`, with a line explaining why: `python3`
resolves through `PATH` to whichever virtualenv is active, reliably the
same environment killcheck is installed into, where a bare `pytest` is a
separate lookup that can silently point elsewhere. The same bare-`pytest`
pattern in `killcheck/cli.py` itself -- the module docstring's usage
example, both of `_discover_test_command`'s error-message examples, and
the `--tests` argparse help text -- was updated to match, so a user
copy-pasting any of the tool's OWN example text gets the safer form, not
just the one in QUICKSTART.

## Finding 3, option E: name the narrow-scope limitation instead of silently guessing wider

Finding 3 (auto-discovery's single-file test scope undercounts reachability
-- see the field-test entry above) needs a design decision on how far to
widen scope automatically, not a patch, so nothing here changes what
`score`/`verify`/`harden` actually run. What ships now is honesty about the
limitation that already exists, per the chosen option:

**Two auto-widening options were considered and explicitly declined.**
Auto-widening to the full test suite by default risks exactly the "twenty
minutes on someone's laptop" failure mode this project already treats as
unacceptable -- CLAUDE.md's own eval-set widening saw test counts go up
6x-40x, curated afterward by hand; nothing curates an unattended CLI run.
A targeted heuristic (AST-scan the test directory for files that import the
target module, run only those) was considered and declined for a sharper
reason: it is precisely the shape of failure this project exists to argue
against. The concrete attrs case that exposed finding 3 -- `test_
annotations.py` asserting on `.__annotations__` values that depend on
lines in `converters.py` -- would not necessarily be caught by an import
scan at all, since the dependency runs through what the code returns, not
through an import statement `test_annotations.py` may or may not contain
in a form a scanner recognizes. A heuristic that is right most of the time
and silently wrong sometimes replaces a visible, stated limitation with a
confident wrong answer -- worse, not better.

**What ships: the limitation is now stated, not silent, and machine-
visible.** When `--tests` is not given (i.e. auto-discovery picked the
test command, always a single file by construction), every command now:

- prints a note at discovery time naming the limitation, citing the
  attrs case concretely (two survivors reported unreachable under their
  single matching test file, actually exercised by a different, sibling
  file invisible to single-file discovery) rather than describing the risk
  abstractly, and suggesting a wider `--tests` to compare;
- repeats a shorter pointer back to that note right next to the actual
  reachability numbers (`score`'s summary, `verify`'s `reachability` row,
  `harden`'s "skipping N unreachable survivor(s)" line), since that's
  where a reader is actually looking, not just at startup;
- sets `"narrow_scope": true` in every command's JSON output, so the
  narrowness is visible to anything reading the file, not only a human
  reading the terminal.

Explicit `--tests` (a user's own choice of scope) triggers none of this --
confirmed directly, not assumed: running the same attrs target with
`--tests` set explicitly produces no note and `"narrow_scope": false`,
while the identical target via auto-discovery produces both. `discover_
target`'s return type changed from `Target` to `(Target, bool)`
accordingly; all three call sites updated.

**Declined for later, per instruction, not implemented here:** a separate,
coverage-only pass under a wider scope (no re-scoring, so the cost is one
extra suite run, not one per mutant) that reports how many "unreachable"
survivors would flip reachable under it -- keeping the fast narrow scope
for the actual score while showing both numbers, honestly labeled by which
scope produced each, matching CLAUDE.md's own "always show the curated
number next to the raw one" principle rather than picking one silently.
This is real, scoped work, left for its own pass.
