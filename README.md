# killcheck

An experiment in whether agentic test generation produces tests that
specify behaviour, or probes that detect only the edit they were shown.
Built around a deterministic mutation harness: it finds mutations the
existing tests fail to detect, has an agent write tests for them, and gates
each test on whether it actually fails when the mutation is applied.

The measurement came first. The instrument was frozen and committed before
any agent code existed, the metric was defined before any arm ran, and one
of the framing claims below was retracted when the data contradicted it.

Concretely: for a Python module, the harness generates one mutant per
mutable site (comparison flips, arithmetic swaps, constant changes, dropped
negations, raise->pass, return-value->None), runs the existing suite against
each, and records which mutations the suite fails to notice. Those survivors
are the work queue. A generated test is kept only if it both passes on clean
source and fails on the specific mutant it targets. Ground truth throughout
is a subprocess exit code — no model judges any outcome.

Submission for the micro1 Frontier Engineering Challenge 2026. See
[CLAUDE.md](CLAUDE.md) for the full experimental design — invariants,
metrics, the agent loop contract, and the eval set. See
[CHANGELOG.md](CHANGELOG.md) for findings and decisions in the order they
happened.

## A finding, not a caveat: most undetected faults are unreached code, not weak assertions

Before building the agent, the eval set's 12 targets were checked for how
many of their surviving mutants sit on a line their own test suite actually
executes at all (`scripts/verify_targets.py`, full method in CLAUDE.md's
Denominator section). Every target's test command was widened to the
broadest scope that still runs clean and fast — full `tests/` directories,
not single files — specifically to rule out narrow test scoping as the
cause of a thin number.

It wasn't the cause. Pooled across all 12 widely used, well-maintained
Python libraries and all 455 mutants the harness generated, only 53 sit on
a line that's reachable-by-this-suite yet goes unasserted — 133 of 455
survive at all, the rest killed or genuinely unreachable. Widening test
scope 6x-40x per target moved that number from 54 to 53 — lower, not
higher (see "Two harness bugs" below for how one of the twelve per-target
figures behind that number was first drafted wrong, then caught and
corrected before it shipped). Ten of the twelve targets showed no movement
at all — their non-`tests/test_X.py` files simply exercise different code,
not more of the same module. Where widening did move something
(`dotenv-variables`, `boltons-typeutils`), it worked almost entirely by
converting `unreachable` mutants straight to `killed`, not into `reachable
survivor`.

| target | mutants | unreach→reach→kill (before) | unreach→reach→kill (after) |
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
undetected because nothing runs that code, not because the tests that do
run assert too little. A tool that only ever writes tests for reachable
survivors — this one included — is addressing the minority of the
undetected-fault problem in code shaped like this. Two ways to make the
pooled number look bigger (swap out the two 0-reachable-survivor targets
for denser ones; keep chasing wider test scope) were considered and
rejected before Task 3 — the first is case selection on the outcome, the
second is contradicted by the data just described. See CLAUDE.md's
"Rejected: reachable-survivor workarounds" for both, and CHANGELOG.md for
the numbers.

## Two harness bugs, both of which would have produced confident wrong answers

**src-layout imports.** `pip install -e` on src-layout packages resolves
imports back to the original checkout, so the runner's tempdir mutations
never executed. Three targets silently scored 0.000. Without the catch this
would have read as "the agent fails on src-layout packages" rather than
"the harness was broken." Fixed, then generalised into a standing canary
check: overwrite each module with unparseable source and assert the suite
does not report survived.

**Concurrent execution.** Parallel mutant evaluation corrupted results for
the one target running real async I/O against real temp files: four
repeated runs gave three different survivor sets. Serial execution is
stable and reproducible. The artifact biased toward the intervention —
spurious failures are read as kills, and kills are what every arm is trying
to increase. A widening improvement already drafted into three files as
0.27→0.77 was concurrency noise. Now `workers=1` everywhere, with a
standing determinism gate: three serial runs, survivor sets must be
byte-identical.

The canary did not catch the second bug, because the two checks prove
different properties. The canary proves mutations reach the interpreter.
The determinism check proves execution is isolated. An eval harness needs
both, and neither is findable by reading the code — only by running a check
that would fail if the instrument were lying.

## Results

Pending — arms A and B have not run yet.

## Reproducing this

**Prerequisites:** Python 3.11+, tested on macOS. `pip install -r
requirements.txt` (pytest, coverage, the Anthropic SDK, and each target's own
test dependencies). Budget roughly 2 GB disk for the 12 cloned target repos
and, at present, tens of minutes of wall clock for the slow steps below —
arm runtimes are not yet known and will be filled in once they exist.

**Setup:**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add ANTHROPIC_API_KEY — see below
```

**In order:**

1. `scripts/fetch_targets.sh` — clones all 12 target repos at the pinned
   commit SHAs recorded in `targets.json` into a local working directory.
   Nothing is vendored; this step requires network access. A few minutes.
2. `python scripts_demo.py` — smoke test against the local fixture
   (`fixture/bank.py`), independent of the cloned targets. Confirms the
   mutation engine and runner work at all before spending time on the full
   eval set. Expected output: `kill_score=0.0952`. Seconds.
3. `python scripts/verify_targets.py` — runs, per target: a canary check
   (mutations reach the interpreter), a determinism check (3 serial full
   scoring runs, survivor sets must be byte-identical — see "Two harness
   bugs" above for why this exists), then coverage-based reachability
   bucketing on top of whichever of those 3 runs is reused. This is the slow
   step: every target is scored 3 times, serially, specifically because
   `workers=1` is required for reproducibility (concurrent execution
   produced non-reproducible survivor sets on real async-I/O targets — see
   above). Writes `results/target_verification.json`. Expect on the order of
   tens of minutes for all 12 targets combined.
4. Each arm's run command — **placeholder, filled in once `killcheck/baseline.py`
   and the agent loop exist and have been run.**

**Where the API key goes:** `ANTHROPIC_API_KEY` in `.env` (see
`.env.example`); loaded via `python-dotenv`, never read from anywhere else.
Token and cost figures per arm are **placeholder — filled in once the arms
have run**, per this project's own rule against drafting numbers before
they're verified.

## Limitations

Stated now, before any arm has run, so scope is fixed before it could be
narrowed to fit whatever the numbers turn out to be.

**Out of scope for this submission:**

- **No per-test kill attribution for arms A and B.** Arm C targets one
  mutant per call, so one generated test maps to one kill outcome. Arms A
  and B are scored per batch — a whole target's generated tests are
  appended once and scored once against every reachable survivor — so there
  is no record of which individual test in a multi-test batch caused which
  mutant to die. For A and B this submission reports the taxonomy
  distribution (what shape of test the arm wrote) and the kill count (how
  many mutants died) side by side, but not "which class of test did the
  killing" — that mapping doesn't exist for these two arms, and attributing
  a batch's outcome to every test in it would silently overcount.
- **No held-out-mutant transfer control.** Two designs (by mutation
  operator, by position) were built and abandoned before Task 3 — neither
  had a large enough denominator to support a rate; see CLAUDE.md's
  "Abandoned: holdout transfer control" for the numbers. Anti-circularity
  in this submission rests on the assertion taxonomy instead, which makes
  vacuous tests visible in the results rather than preventing them from
  counting toward SKR. That is a narrower guarantee than a true
  held-out-mutant transfer signal would have been, and this line states
  that plainly rather than treating the taxonomy as a full substitute.
- **Search-based test generation baselines** (Pynguin, CodaMosa). The
  comparison here is single-prompt vs. budget-matched vs. the mutation-gated
  agent, all LLM-based. Whether a non-LLM search-based generator would beat
  either baseline arm is a real and interesting question; it is not this
  question.
- **Real-fault benchmarks** (Defects4J or similar). This eval set measures
  detection of synthetic AST mutations, not reproduction of historical real
  bugs. Mutation testing is a proxy for fault-detection ability, not a
  substitute for it — a known limitation of mutation testing generally, not
  something specific to this tool.
- **Higher-order mutants** (two or more simultaneous mutations). Every
  mutant in this eval set is a single AST-level change. Higher-order
  mutants are known to sometimes survive when their constituent first-order
  mutants are individually killed; that interaction is not measured here.
- **Maintenance cost and flake rate, beyond what the assertion taxonomy
  measures.** A test that clears the kill gate today is not guaranteed to
  stay meaningful as the source evolves, and this project does not run
  generated tests repeatedly over time or across dependency upgrades to
  check for flakiness beyond what's caught during the gate itself (see
  CLAUDE.md's note on recording flaky/non-deterministic gate results rather
  than silently rerunning them).
- **Full semantic equivalence detection.** `engine.py` already drops
  mutants whose unparsed AST is textually identical to the original
  (trivially equivalent), and `scripts/verify_targets.py`'s reachability
  check removes mutants no test could structurally reach — but neither
  catches a mutant that is reachable, executed, and still behaviourally
  equivalent to the original for every input any test happens to construct
  (a classic undecidable problem in general). This project does not attempt
  automated equivalence detection beyond those two mechanical checks. A
  20-item stratified manual audit of surviving mutants for equivalence is
  planned if time permits, but is not committed to — it is not part of any
  reported metric either way. If it runs, the language here tightens to
  describe what was found.

None of the above are treated as blocking issues to work around under time
pressure. They're the honest edges of what this submission measures.
