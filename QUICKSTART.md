# killcheck quickstart

killcheck finds bugs your tests wouldn't catch. It changes one line of your
code at a time -- `<` to `<=`, `True` to `False`, a return value to `None` --
and re-runs your test suite. If the suite still passes, that change is a
**survivor**: something in your code could break in that specific way and
nothing would tell you.

This page is about the `killcheck` command against one file of your own. If
you're here for the research write-up (a 12-library study of whether an LLM
agent can close these gaps, with a frozen measurement core and a pre-committed
metric), that's [README.md](README.md) instead -- this page has none of that.

## Install

```bash
git clone <this repo>
cd killcheck
pip install -e .
```

That's it -- `pytest` and `coverage` come with it. Run it from inside the
same virtualenv your target project's own tests run in, exactly like you'd
run `pytest` yourself; killcheck needs your project's dependencies importable
to run your suite at all.

## Get a score

```bash
cd /path/to/your/project
killcheck score yourpackage/somemodule.py
```

killcheck looks for a test file named after the module (`test_somemodule.py`
or one under a `tests/`/`test/` directory) and guesses a project root from the
nearest `.git`/`pyproject.toml`/`setup.py` above it. If either guess is wrong,
say so yourself:

```bash
killcheck score yourpackage/somemodule.py --tests "pytest tests/test_somemodule.py -q"
```

Output looks like this:

```
somemodule.py  (somemodule)
  8/17 killed  (kill score 0.4706)
  killed=8 (47%)  timeout=0 (0%)  error=0 (0%)  survived=9 (53%)

  4 of 9 survivors sit on a line the suite actually executes -- those are the
  ones worth writing a test for. 5 sit on a line the suite never runs at all
  (no test could kill them without first covering that line).

  Survivors by function:

  clamp()
    line 7     [reachable ] compare        M-f36da4f7
      before: if x < lo:
      after:  if x <= lo:
    line 9     [reachable ] compare        M-f9aa60b2
      before: if x > hi:
      after:  if x >= hi:
  ...
```

Read it as: **reachable** survivors are the ones worth a look -- your suite
runs that line and still didn't notice the change. **Unreachable** ones are a
coverage gap, not an assertion gap -- no test could catch that mutation
without first executing the line at all, so writing a sharper assertion
wouldn't help; you'd need a test that exercises that code path in the first
place. A full JSON report (every mutant, every line) is written to
`.killcheck/<name>_score.json` for anything this summary doesn't show.

No API key needed for this. Nothing here calls a model.

## Check whether you can trust that score

```bash
killcheck verify yourpackage/somemodule.py
```

Runs a few sanity checks before trusting any of the above: that your suite
actually passes on unmutated code, that a deliberately broken version of the
module is actually detected (not silently passing because the mutation never
reached the interpreter), that a same-size behavioral change is detected too
(rules out stale compiled bytecode masking the mutation), and that scoring the
same module three times in a row gives the same answer (rules out flaky or
order-dependent tests giving you a different survivor list every run). Also
no API key needed.

## Get tests written for you

```bash
export ANTHROPIC_API_KEY=sk-...
pip install -e ".[harden]"
killcheck harden yourpackage/somemodule.py
```

For each reachable survivor, this asks a model for one test targeting that
specific mutation, then actually runs it: the test only gets kept if it passes
on your real code AND fails on the mutated version. Nothing is kept on a
model's say-so -- a subprocess exit code decides, same as `score` above.
Anything that fails both tries is discarded, not silently patched to pass.

You get:
- a `.killcheck/<name>_killcheck_tests.py` file with every kept test, ready to
  read before you merge it into your real suite -- passing the gate proves a
  test is sensitive to one specific mutation, not that it's a good
  specification of your function in general, so read them rather than
  rubber-stamp them in
- a `.killcheck/<name>_harden.json` with the full record: every draft, every
  retry, why anything was discarded, and the token cost

By default this skips survivors on unreachable lines (see above -- there's
usually nothing a test can do about those) and caps itself at 25 survivors per
run (`--max-survivors` to change it).

## Everything writes to `.killcheck/`

All three commands write into a `.killcheck/` directory under wherever you ran
them from -- never into this repo's own checkout, regardless of where
`killcheck` itself is installed. Delete it any time; nothing here is load-
bearing for a later run.

## Options common to all three commands

| flag | what it does |
|---|---|
| `--tests "..."` | the test command to run, instead of guessing |
| `--out DIR` | write results here instead of `./.killcheck` |
| `--timeout N` | per-mutant subprocess timeout in seconds (default 60) |
| `--debug` | show the full traceback instead of a one-line error |

`killcheck <command> --help` lists the rest.

## If something looks wrong

killcheck refuses to guess past a few specific problems rather than return a
silent zero: a test suite that doesn't pass on unmutated code, a module that
doesn't parse, no test file it can find with confidence, or a test command
that isn't installed on PATH. Each prints a one-line reason and what to pass
instead of guessing wrong. If you hit something that isn't one of these and
looks like a bug, `--debug` gets you the traceback to report it with.
