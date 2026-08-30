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
