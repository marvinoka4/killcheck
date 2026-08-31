# Tripwires for arm C

Written before arm C's first call, per the discipline this project has used
throughout: define the failure conditions before you have a result that
could tempt you to explain one away. Every item below is an ABORT condition,
not a caveat to note in the report. If one fires, stop the run, fix the
actual cause, and re-run from scratch -- do not patch around it, do not
finish the run "to see how it comes out," do not keep partial results from
before the fire as if they were unaffected by whatever caused it.

This document is checked during the run, not after. `killcheck/agent.py`
should check what it can check inline (canary, prompt contents, retry
behavior) as each target completes, not defer everything to a final audit.
Whatever can't be checked inline (batch-rescore-vs-per-call agreement,
duplicate rate across the whole run) gets checked the moment enough data
exists to check it -- after each target, not only at the very end.

## HARNESS

These mean the measuring instrument itself is not doing what it claims to.
A result produced while one of these is true is not evidence about arm C;
it's evidence about a broken harness, indistinguishable from real signal
unless you check for it separately, which is the entire point of the
canary/determinism-check pattern this project has already needed twice.

- **Canary fails to fail.** Before scoring any target, the standing canary
  check (`scripts/verify_targets.py`'s `canary_check`, or the equivalent run
  live against arm C's own copy) must report the suite correctly detects
  unimportable source as a kill. If it doesn't, mutations aren't reaching
  the interpreter for this target -- abort before generating a single test.

- **Mutant source is not what the test imported.** After writing
  `mutant.source` into the scored tempdir copy, read the file back and
  compare it byte-for-byte against `mutant.source` before running anything.
  A mismatch (stale bytecode, wrong path resolved via an editable install,
  a copy that silently fell back to the real checkout) means the test that
  "killed" the mutant never saw it -- this is the exact src-layout bug this
  project already hit once, recurring in a different call path.

- **Plugin rows missing, or `when` never equals `"call"`.** Whatever
  pytest-hook mechanism `agent.py` uses to capture the exact per-test
  outcome must log at least one row per test execution, and at least one of
  those rows per test must have `when == "call"` (the actual test body
  executing, as opposed to `setup`/`teardown`, or collection). Zero rows
  means the hook isn't firing at all; `when` never reaching `"call"` means
  every test is erroring out before its body ever runs, and the run's kills
  are `error`-outcome by default, not `killed`-via-assertion -- silently.

- **Batch rescore of the kept set disagrees with per-call gate outcomes.**
  After all of a target's kept tests are appended once and rescored as one
  batch (the official-number mechanism, matching arms A/B), the per-mutant
  kill/survive result for each test must match what the individual gate
  call already found. A disagreement means the kept tests interact with
  each other once combined -- this is not hypothetical: `tenacity-stop`'s
  arm A batch broke twelve pre-existing tests through nothing more than a
  same-named helper function, found only when the whole batch ran together.
  Arm C is smaller-batch by design (one test targets one mutant) but the
  existing suite is still present at scoring time every time, so this
  exposure is narrowed, not closed.

- **The kept set fails on clean source when run together.** Same root
  cause as above, sharper trigger: every kept test passed clean
  individually at gate time, by construction -- if the batch of all of them
  together does not pass clean, something in the set collides (shared
  names, shared mutable fixtures, order-dependent state). Do not attribute
  this to "one bad test slipping through the gate" without checking; the
  gate cannot see it by design (see the tenacity-stop writeup in
  CHANGELOG.md) and single-test bisection can fail to find it the same way
  it did there.

- **A call emits more than one test function.** The agent loop contract
  asks for exactly one test per call. If a generate call's parsed output
  contains more than one top-level `def test_*`/`async def test_*`, that's
  a contract violation, not a bonus -- it breaks the one-test-per-mutant
  scoring unit this arm's whole design depends on, and reintroduces the
  batch-attribution problem arms A and B have (see CLAUDE.md's assertion
  taxonomy limitation).

- **The prompt lacks the mutant diff.** Check the actual prompt text sent
  for each generate call, not just that a variable named `mutant_diff` was
  passed somewhere -- confirm the original-line/mutated-line pair is
  present verbatim. A prompt missing this is asking the model to write a
  test for a mutation it was never shown.

- **Attempt-2 prompt is identical to attempt-1, or retry never fires after
  a discard.** The agent loop contract's retry step exists specifically to
  feed back the real pytest failure output; if attempt 2's prompt doesn't
  differ from attempt 1's (modulo the appended failure text), the retry
  isn't doing what it's supposed to, and any measured lift from retrying is
  not real. Equally: if a test is discarded and no retry is logged
  afterward, the retry step silently didn't run at all.

- **pytest collected 0.** A clean-pass or kill-gate check that reports
  success because pytest found zero tests to run is not a pass -- it's a
  vacuous result, and every "kept" decision downstream of it is invalid.

## GENERATOR COLLAPSE

These mean the model is producing output, and it's passing whatever checks
run first, but the output itself has stopped doing the job -- the numbers
would look fine and mean nothing.

- **High AST-normalised duplicate rate.** Reuse the same canonicalisation
  used for the arm-B duplicate check (rename the function to a placeholder,
  strip decorators, `ast.unparse`, compare for exact equality) across all of
  arm C's generated tests. A high rate means the model is looping on a
  handful of shapes rather than genuinely responding to each mutant's diff.

- **High share of kept tests that never call the mutated function.** For
  each kept test, check whether the mutated function/attribute name
  (available from the mutant record) appears anywhere in the test's AST. A
  test that kills its mutant without ever referencing what was mutated is
  very likely killing by accident -- an unrelated side effect, an import-time
  crash, a collection error -- not by exercising the intended behaviour.

- **Retry body is a rename of attempt 1.** Canonicalise both attempts the
  same way as the duplicate check above and compare. If they're identical
  once names are normalised, the model reacted to nothing in the fed-back
  failure output -- the retry step ran, but bought nothing, and any
  "attempt 2 helped" claim is false.

- **Keep rate at 0% or 100% with no target-level variation.** Twelve
  targets with genuinely different mutation surfaces should not produce a
  uniform keep rate. Either extreme, flat across every target, means the
  gate isn't discriminating (or isn't running) rather than that the model's
  performance is genuinely uniform.

## METRIC COLLAPSE

These mean the primary number is being computed in a way that would make it
look better than it is, independent of anything the model actually did.

- **Nearly all kills are call-phase non-`AssertionError` on
  `return_none`/`constant` mutants.** Per CLAUDE.md's kill-outcome-breakdown
  rule, `error`-outcome kills are real but weaker evidence than an assertion
  actually firing. If the overwhelming majority of arm C's kills are
  exceptions other than `AssertionError` concentrated on exactly the two
  operators most prone to trivial crashes, the primary metric is being
  carried by import/collection breakage, not by tests that specify
  behaviour -- state this explicitly rather than let a healthy-looking
  pooled SKR hide it, per the existing kill-outcome-breakdown rule.

- **Official SKR computed incrementally rather than by one-pass batch
  rescore.** This is the exact confound CLAUDE.md already forbids for arms
  A and B, and it applies just as much to arm C's final number: the kept
  set must be appended once and scored once. Scoring incrementally as tests
  are kept gives compounding credit to whichever tests happen to be kept
  first.

- **A 0-reachable-survivor target back in the pool.** `voluptuous-error` and
  `dotenv-variables` contribute 0/0 and must stay excluded from the pooled
  denominator (see CLAUDE.md's Denominator section). Either reappearing in
  a nonzero-denominator pooled calculation is a regression in the pooling
  code, not a real result.
