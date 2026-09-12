"""`killcheck` the command-line tool: mutation testing against a single
arbitrary module, not just the 12 hardcoded eval-set targets in
targets.json.

Three subcommands, in increasing order of what they need:

  killcheck score <module.py> --tests "pytest tests/test_x.py -q"
    -> mutation score for that module, with the survivor list. No API key.

  killcheck verify <module.py> --tests "..."
    -> canary, byte-size canary, determinism gate, reachability -- the
       instrument-soundness checks, on this one module. No API key.

  killcheck harden <module.py> --tests "..."
    -> the full agent loop: reachable survivors, generated tests, gate,
       retry, a batch rescore, and a ready-to-review test file. Needs
       ANTHROPIC_API_KEY.

targets.json/scripts/*.py (the 12-target eval-set batch mode this
submission's own results/ and README are built on) are untouched by this
file and keep working exactly as they did -- see CLAUDE.md's Architecture
section. This is a second, independent way in, not a replacement.

Everything here calls into killcheck/runner.py, killcheck/engine.py,
killcheck/verify_core.py, killcheck/agent.py and killcheck/baseline.py's
existing functions as-is. The measurement core (engine.py, runner.py) is
untouched -- this file only ever calls its public functions
(score_target, verify_clean, generate_mutants), never reimplements or
patches their logic.

Output never goes into this repo's own results/ or trajectories/ -- those
are the frozen eval-set artifacts the primary metric is built on, and a
`pip install -e .` keeps this file living inside that checkout, so getting
this wrong would mean an ad-hoc `killcheck score` run on someone's unrelated
project could silently corrupt the submission's own committed data. Every
command writes into --out instead (default: ./.killcheck/ under wherever
the command was actually run from).

`score` and `harden` both run the unparseable-source canary before doing
any mutation work, and ABORT on failure rather than return a number --
added after a field test against a real src-layout target (pytest testing
itself) showed `score` returning a confident, unflagged kill_score=0.0000
while `verify`, run against the exact same target, correctly caught and
refused. A confident wrong number is worse than a refusal; see
CHANGELOG.md's "score and harden must not run without a canary" entry.
`--skip-canary` overrides this for a user who has already confirmed the
canary separately (or is deliberately investigating a known-bad target);
it says so loudly in the output, not just in a flag name nobody re-reads.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import shlex
import sys
import time
from collections import defaultdict
from pathlib import Path

from killcheck.agent import draft_and_gate, enclosing_function, official_batch_rescore
from killcheck.baseline import CALL_CAP, existing_test_source
from killcheck.engine import generate_mutants
from killcheck.runner import Target, score_target, verify_clean
from killcheck.verify_core import (
    build_mutant_records,
    byte_size_canary_check,
    canary_check,
    determinism_check,
    measure_reachable_lines,
    outcome_breakdown,
    summarize_reachability,
)

_ROOT_MARKERS = (".git", "pyproject.toml", "setup.py", "setup.cfg", "tox.ini")

# Shared between verify/score/harden's canary-failure paths -- a canary
# failure used to just say "mutations are not reaching this target's test
# process," which is true but tells a user nothing to try next. The most
# common real-world cause, confirmed on two independently-chosen real repos
# during a field test (attrs, pytest), is a src-layout package: the
# editable-install re-imports the ORIGINAL checkout instead of the mutated
# tempdir copy. `-o pythonpath=src` fixes it for a normal src-layout target
# (attrs) but not for a target that imports itself while its OWN test
# runner is still starting up, before that option's collection-time path
# logic ever runs (pytest testing pytest -- confirmed by direct
# reproduction, not assumed). --tests-env sets a real environment variable
# before the interpreter starts, which is not too late either way. See
# CHANGELOG.md's "canary failure messages must name the likely cause" entry
# for the full mechanism on both counterexamples.
_CANARY_FAILURE_HELP = (
    "canary failed -- mutations are not reaching this target's test process, so any kill "
    "score from it would be meaningless.\n"
    "\n"
    "The most common cause is a src-layout package (the module lives under src/<pkg>/) "
    "installed editable. Try adding `-o pythonpath=src` to --tests, e.g.\n"
    '  --tests "... -o pythonpath=src"\n'
    "\n"
    "That does not always work: a project that imports itself while its OWN test runner is "
    "still starting up (pytest testing pytest is the known case) has already re-imported the "
    "original before that option's collection-time path logic ever runs. If `-o "
    "pythonpath=src` doesn't fix it, set the path before the interpreter starts instead:\n"
    "  --tests-env PYTHONPATH=src\n"
    "\n"
    "See CHANGELOG.md's src-layout entries for the full mechanism on both cases."
)

_SKIP_CANARY_WARNING = (
    "WARNING: --skip-canary passed -- the canary (which confirms a mutation actually reaches "
    "the interpreter) was NOT run. If this target has the src-layout resolution problem the "
    "canary exists to catch, every result below is meaningless, not just optimistic -- it is "
    "not measuring what it looks like it is measuring. Run `killcheck verify` on this module "
    "to check properly before trusting anything below."
)


class CLIError(Exception):
    """A problem with the user's input or environment, not a bug in this
    tool -- printed as `error: ...` and exits 1, no traceback, unless
    --debug is passed. Every place this project's own eval-set assumptions
    (a discoverable test file, a clean suite, an importable module) don't
    hold for an arbitrary repo should raise this with a specific, actionable
    message rather than let an unrelated exception surface as a stack
    trace or -- worse -- get silently swallowed into a 0."""


# ---------------------------------------------------------------------------
# Target discovery -- the thing targets.json used to do for the 12 eval-set
# cases, done here from a bare module path plus an optional --tests string.
# ---------------------------------------------------------------------------


def _find_project_root(start: Path) -> tuple[Path, bool]:
    """Walk upward from `start` looking for a project marker. Returns
    (root, guessed) -- guessed=True means no marker was found and `start`
    itself was used as a fallback. Correct for a free-standing single-file
    script; wrong (but not silently wrong -- the caller prints a note) if
    the module is actually nested in a larger, just-unmarked project."""
    cur = start.resolve()
    for _ in range(64):  # bounded walk, not a bet on the filesystem terminating
        if any((cur / marker).exists() for marker in _ROOT_MARKERS):
            return cur, False
        if cur.parent == cur:
            break
        cur = cur.parent
    return start.resolve(), True


_EXCLUDED_DIR_NAMES = {".venv", "venv", "__pycache__", ".git", "build", "dist", "node_modules", ".tox", ".mypy_cache"}


def _candidate_test_files(project_root: Path) -> list[Path]:
    files: set[Path] = set()
    for pattern in ("test_*.py", "*_test.py"):
        files.update(project_root.rglob(pattern))
    return sorted(f for f in files if not (_EXCLUDED_DIR_NAMES & set(f.parts)))


def _discover_test_command(project_root: Path, module_file: Path) -> list[str]:
    """Best-effort guess at a test command for `module_file`, using the same
    stem/parent-dir tiering as killcheck/baseline.py's existing_test_source
    -- but erroring out instead of silently falling back to "largest file in
    the repo" when nothing matches. That silent guess already produced one
    real wrong pick in this project's own eval set (aiofiles-temptypes
    picking test_os.py; see CHANGELOG.md), on a project this codebase's
    authors hand-audited. On an arbitrary stranger's repo there is no such
    audit, so ambiguity here becomes an error with a copy-pasteable example,
    not a guess.
    """
    candidates = _candidate_test_files(project_root)
    if not candidates:
        raise CLIError(
            f"no test files found under {project_root} (looked for test_*.py "
            f"and *_test.py). Pass --tests explicitly, e.g.\n"
            f'  --tests "pytest path/to/tests -q"'
        )
    stem = module_file.stem
    parent = module_file.parent.name
    matched = [c for c in candidates if stem in c.name]
    if not matched and parent:
        matched = [c for c in candidates if parent in c.name]
    if not matched:
        example = candidates[0].relative_to(project_root)
        raise CLIError(
            f"found {len(candidates)} test file(s) under {project_root} but none named after "
            f"{stem!r} or {parent!r} -- won't guess which one(s) exercise this module. "
            f'Pass --tests explicitly, e.g.\n  --tests "pytest {example} -q"'
        )
    chosen = sorted(matched, key=lambda p: -p.stat().st_size)[0]
    return [sys.executable, "-m", "pytest", str(chosen.relative_to(project_root)), "-q"]


def discover_target(module_arg: str, tests_arg: str | None) -> Target:
    module_file = Path(module_arg)
    if not module_file.exists():
        raise CLIError(f"no such file: {module_arg}")
    if module_file.is_dir():
        raise CLIError(f"{module_arg} is a directory -- give a single .py module, not a package.")
    module_file = module_file.resolve()
    if module_file.suffix != ".py":
        raise CLIError(f"{module_arg} is not a .py file")

    try:
        source = module_file.read_text()
    except UnicodeDecodeError as e:
        raise CLIError(f"{module_arg} could not be read as text: {e}")
    try:
        ast.parse(source)
    except SyntaxError as e:
        raise CLIError(f"{module_arg} is not valid Python: {e}")

    project_root, guessed = _find_project_root(module_file.parent)
    if guessed:
        print(
            f"note: no project marker ({'/'.join(_ROOT_MARKERS)}) found above {module_file} "
            f"-- using {project_root} as the project root. If this module actually lives inside "
            f"a larger project, run killcheck from that project's root, or move up a directory.",
            file=sys.stderr,
        )

    module_rel = module_file.relative_to(project_root)

    if tests_arg:
        test_command = shlex.split(tests_arg)
        if not test_command:
            raise CLIError("--tests was empty")
    else:
        test_command = _discover_test_command(project_root, module_file)
        print(f"note: no --tests given -- discovered: {' '.join(test_command)}", file=sys.stderr)

    name = str(module_rel.with_suffix("")).replace(os.sep, "-")
    return Target(name=name, project_root=project_root, module_path=module_rel, test_command=test_command)


def _apply_tests_env(pairs: list[str]) -> None:
    """Set each --tests-env KEY=VALUE pair as a real process environment
    variable, before any subprocess this CLI runs.

    This works with zero change to the frozen core: runner.py's `_run()`
    (and every other subprocess.run call in this codebase) never passes
    `env=` explicitly, so it always inherits whatever THIS process's
    os.environ is at call time. Setting a var here, once, early, is enough
    for every later subprocess call in the same run to see it. This is the
    exact mechanism this project's own PYTHONPYCACHEPREFIX tests already
    rely on (see scripts/test_pyc_exclusion.py) -- --tests-env is that same
    mechanism, exposed to a user instead of hardcoded to one test.

    Why this belongs at the CLI layer and not as a runner.py change: an
    option baked into --tests itself (e.g. pytest's own `-o
    pythonpath=src`) only ever affects pytest's own collection-time path
    logic -- too late for a target that imports itself during its OWN test
    runner's startup, before collection begins (pytest testing pytest is
    the confirmed case; see CHANGELOG.md). A real environment variable, set
    before the interpreter even starts, is visible to the very first
    import, which is what actually fixes that case -- and inheriting
    os.environ is already how every subprocess call here behaves, so
    reaching this by mutating this process's own environment needs no
    change to runner.py's frozen _run() at all.
    """
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise CLIError(f"--tests-env {pair!r} is not in KEY=VALUE form")
        if not key:
            raise CLIError(f"--tests-env {pair!r} has an empty key")
        os.environ[key] = value


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _out_dir(args: argparse.Namespace) -> Path:
    d = Path(args.out) if args.out else Path.cwd() / ".killcheck"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _reachability_for(target: Target, report: dict) -> tuple[dict, list[dict], bool]:
    reachable_lines = measure_reachable_lines(target)
    mutants = build_mutant_records(target, report, reachable_lines)
    summary = summarize_reachability(mutants)
    return summary, mutants, reachable_lines is None


def _client():
    """Separate from killcheck.baseline._client(): that one loads .env from
    this SUBMISSION's own repo root (REPO / ".env"), which is right for the
    12-target batch mode but wrong here -- a stranger running `killcheck
    harden` in their own project has no reason to have this repo checked
    out at all, let alone a .env inside it. This loads from the CURRENT
    DIRECTORY explicitly (not python-dotenv's default find_dotenv(), which
    walks the call stack to the *calling file's own path* rather than the
    user's cwd -- the exact mechanism behind the .env leak fixed in
    runner.py's verify_clean(); see CHANGELOG.md. Being explicit about the
    path here avoids that footgun instead of re-triggering it.)
    """
    try:
        from anthropic import Anthropic
        from dotenv import load_dotenv
    except ImportError as e:
        raise CLIError(
            f"missing dependency for `killcheck harden` ({e.name}). `score` and `verify` don't "
            f'need it, but `harden` calls a model. Install it with: pip install "killcheck[harden]"'
        )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        load_dotenv(Path.cwd() / ".env")
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise CLIError(
            "ANTHROPIC_API_KEY not set. Export it, or put it in a .env file in the "
            "current directory."
        )
    return Anthropic(api_key=key)


# ---------------------------------------------------------------------------
# Human-readable formatting (score/verify share the survivor-by-function view)
# ---------------------------------------------------------------------------


def _reachability_tag(reachable: bool | None) -> str:
    if reachable is True:
        return "reachable"
    if reachable is False:
        return "unreachable"
    return "unknown"


def format_survivors_by_function(target: Target, survivor_records: list[dict]) -> str:
    if not survivor_records:
        return "  No survivors -- every mutant was killed."

    source = (target.project_root / target.module_path).read_text()
    tree = ast.parse(source)
    mutant_by_id = {m.id: m for m in generate_mutants(source, str(target.module_path))}

    by_function: dict[str, list[tuple[dict, object]]] = defaultdict(list)
    for rec in survivor_records:
        m = mutant_by_id.get(rec["mutant_id"])
        fn = enclosing_function(tree, rec["lineno"]) or "<module level>"
        by_function[fn].append((rec, m))

    lines = ["  Survivors by function:"]
    for fn in sorted(by_function, key=lambda k: by_function[k][0][0]["lineno"]):
        entries = sorted(by_function[fn], key=lambda pair: pair[0]["lineno"])
        lines.append(f"\n  {fn}()")
        for rec, m in entries:
            tag = _reachability_tag(rec["reachable"])
            lines.append(f"    line {rec['lineno']:<5} [{tag:10s}] {rec['operator']:<14} {rec['mutant_id']}")
            if m is not None:
                lines.append(f"      before: {m.original_line.strip()}")
                lines.append(f"      after:  {m.mutated_line.strip()}")
    return "\n".join(lines)


def format_reachability_line(reach_summary: dict, reach_unknown: bool, n_survivors: int) -> str:
    if reach_unknown:
        return (
            "  NOTE: could not measure line coverage for this module (test command has no "
            "'pytest' step, or coverage failed) -- reachability is UNKNOWN for every survivor "
            "below, not the same as unreachable."
        )
    return (
        f"  {reach_summary['reachable_survivor']} of {n_survivors} survivors sit on a line the "
        f"suite actually executes -- those are the ones worth writing a test for. "
        f"{reach_summary['unreachable']} sit on a line the suite never runs at all (no test "
        f"could kill them without first covering that line)."
    )


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------


def cmd_score(args: argparse.Namespace) -> int:
    target = discover_target(args.module_path, args.tests)
    out_dir = _out_dir(args)

    try:
        verify_clean(target, timeout=max(args.timeout, 30))
    except RuntimeError as e:
        raise CLIError(str(e))

    canary_verified = False
    if args.skip_canary:
        print(_SKIP_CANARY_WARNING, file=sys.stderr)
    else:
        passed, _detail = canary_check(target, timeout=args.timeout)
        if not passed:
            raise CLIError(_CANARY_FAILURE_HELP)
        canary_verified = True

    report = score_target(target, timeout=args.timeout, workers=1, check_clean=False)
    if report["total_mutants"] == 0:
        print(f"{target.module_path}: no mutable sites found -- nothing to score.")
        return 0

    breakdown = outcome_breakdown(report)
    reach_summary, mutant_records, reach_unknown = _reachability_for(target, report)
    survivor_records = [m for m in mutant_records if m["outcome"] == "survived"]

    result = {
        "target": target.name,
        "module": str(target.module_path),
        "project_root": str(target.project_root),
        "test_command": target.test_command,
        "canary_verified": canary_verified,
        **report,
        "outcome_breakdown": breakdown,
        "reachability": reach_summary,
        "reachability_unknown": reach_unknown,
        "mutants_detail": mutant_records,
    }
    path = out_dir / f"{target.name}_score.json"
    path.write_text(json.dumps(result, indent=2))

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    if not canary_verified:
        print(_SKIP_CANARY_WARNING)
        print()
    c, f = breakdown["counts"], breakdown["fractions"]
    print(f"{target.module_path}  ({target.name})")
    print(f"  {report['killed']}/{report['total_mutants']} killed  (kill score {report['kill_score']:.4f})")
    print(
        f"  killed={c['killed']} ({f['killed']:.0%})  timeout={c['timeout']} ({f['timeout']:.0%})  "
        f"error={c['error']} ({f['error']:.0%})  survived={c['survived']} ({f['survived']:.0%})"
    )
    if breakdown["error_dominant"]:
        print(
            f"  NOTE: {breakdown['error_share_of_kills']:.0%} of kills are import-time errors, "
            f"not assertions firing -- treat the kill score with that in mind."
        )
    if survivor_records:
        print()
        print(format_reachability_line(reach_summary, reach_unknown, len(survivor_records)))
        print()
        print(format_survivors_by_function(target, survivor_records))
    else:
        print("\n  No survivors -- every mutant was killed.")
    print(f"\nwrote {path}")
    if not canary_verified:
        print()
        print(_SKIP_CANARY_WARNING)
    return 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def cmd_verify(args: argparse.Namespace) -> int:
    target = discover_target(args.module_path, args.tests)
    out_dir = _out_dir(args)
    print(f"=== verify: {target.module_path} ===")
    checks: list[dict] = []

    def record(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})
        print(f"  {name:20s} {status:5s}  {detail}")

    try:
        verify_clean(target, timeout=max(args.timeout, 30))
    except RuntimeError as e:
        record("clean suite", "FAIL", str(e).splitlines()[0])
        raise CLIError("clean suite does not pass -- fix that before measuring anything.")
    record("clean suite", "PASS", "suite passes on unmutated source")

    passed, detail = canary_check(target, timeout=args.timeout)
    record("canary", "PASS" if passed else "FAIL", detail)
    if not passed:
        raise CLIError(_CANARY_FAILURE_HELP)

    status, detail = byte_size_canary_check(target, timeout=args.timeout)
    record("byte-size canary", status, detail)
    if status == "FAIL":
        raise CLIError(
            "byte-size canary failed -- a same-byte-length mutation on a line the clean suite "
            "actually executes (reachability confirmed first, not assumed) went undetected. "
            "Unreached-line false positives are ruled out by construction, so this is a "
            "genuine possible stale-bytecode execution. See CHANGELOG.md's byte_size_canary_check "
            "entries for the mechanism."
        )

    if args.skip_determinism:
        record("determinism", "SKIPPED", "--skip-determinism passed; the run below is a single pass, not confirmed stable")
        report = score_target(target, timeout=args.timeout, workers=1, check_clean=False)
    else:
        deterministic, detail, reports = determinism_check(target, timeout=args.timeout)
        record("determinism", "PASS" if deterministic else "FAIL", detail)
        if not deterministic:
            raise CLIError(
                "survivor set is not deterministic across 3 serial runs -- do not trust a kill "
                "score from this target as-is. Often caused by real concurrency, real I/O, or "
                "wall-clock-dependent tests inside the suite."
            )
        report = reports[0]

    breakdown = outcome_breakdown(report)
    reach_summary, mutant_records, reach_unknown = _reachability_for(target, report)
    record(
        "reachability",
        "UNKNOWN" if reach_unknown else "OK",
        "could not measure coverage for this module" if reach_unknown else
        f"{reach_summary['reachable_survivor']} reachable-survivor, "
        f"{reach_summary['unreachable']} unreachable, {reach_summary['killed']} killed",
    )

    result = {
        "target": target.name,
        "module": str(target.module_path),
        "project_root": str(target.project_root),
        "test_command": target.test_command,
        "checks": checks,
        "report": report,
        "outcome_breakdown": breakdown,
        "reachability": reach_summary,
        "reachability_unknown": reach_unknown,
        "mutants_detail": mutant_records,
    }
    path = out_dir / f"{target.name}_verify.json"
    path.write_text(json.dumps(result, indent=2))
    print(f"\n  {report['total_mutants']} mutants, kill score {report['kill_score']:.4f}")
    if args.json:
        print(json.dumps(result, indent=2))
    print(f"\nwrote {path}")
    return 0


# ---------------------------------------------------------------------------
# harden
# ---------------------------------------------------------------------------


def cmd_harden(args: argparse.Namespace) -> int:
    target = discover_target(args.module_path, args.tests)
    out_dir = _out_dir(args)
    (out_dir / "trajectories").mkdir(parents=True, exist_ok=True)

    try:
        verify_clean(target, timeout=max(args.timeout, 30))
    except RuntimeError as e:
        raise CLIError(str(e))

    canary_verified = False
    if args.skip_canary:
        print(_SKIP_CANARY_WARNING, file=sys.stderr)
    else:
        passed, _detail = canary_check(target, timeout=args.timeout)
        if not passed:
            raise CLIError(
                _CANARY_FAILURE_HELP + "\n\nRefusing to run harden: with the canary failing, "
                "every survivor below is an artifact of mutations never reaching the "
                "interpreter, not a real gap -- no test could ever kill them, so harden would "
                "spend real model calls chasing something that can't be fixed by writing a "
                "better test. Fix the canary failure first, or pass --skip-canary if you "
                "understand the risk."
            )
        canary_verified = True

    report = score_target(target, timeout=args.timeout, workers=1, check_clean=False)
    if report["total_mutants"] == 0:
        print(f"{target.module_path}: no mutable sites found -- nothing to harden.")
        return 0

    _reach_summary, mutant_records, reach_unknown = _reachability_for(target, report)
    survivor_records = [m for m in mutant_records if m["outcome"] == "survived"]
    if not survivor_records:
        print(f"{target.module_path}: no survivors -- every mutant is already killed. Nothing to harden.")
        return 0

    if args.include_unreachable or reach_unknown:
        wanted = survivor_records
    else:
        wanted = [m for m in survivor_records if m["reachable"] is True]
        skipped = len(survivor_records) - len(wanted)
        if skipped:
            print(
                f"note: skipping {skipped} unreachable survivor(s) -- no test can kill a "
                f"mutation on a line the suite never executes. Use --include-unreachable to "
                f"attempt them anyway (almost always wasted calls)."
            )
        if not wanted:
            print("no reachable survivors -- nothing to harden. (Re-run with --include-unreachable "
                  "to attempt the unreachable ones anyway, though they are close to unkillable.)")
            return 0

    if len(wanted) > args.max_survivors:
        print(
            f"note: capping at --max-survivors {args.max_survivors} of {len(wanted)} eligible "
            f"survivors (lowest line number first). Raise the cap to cover the rest."
        )
        wanted = sorted(wanted, key=lambda m: m["lineno"])[: args.max_survivors]
    else:
        wanted = sorted(wanted, key=lambda m: m["lineno"])

    source = (target.project_root / target.module_path).read_text()
    module_tree = ast.parse(source)
    mutant_by_id = {m.id: m for m in generate_mutants(source, str(target.module_path))}

    spec = {"test_command": target.test_command}
    try:
        test_file, test_source = existing_test_source(spec, target)
    except RuntimeError as e:
        raise CLIError(str(e))
    print(f"appending generated tests against: {test_file.relative_to(target.project_root)}")

    client = _client()
    run_id = f"harden-{target.name}-{time.strftime('%Y%m%d-%H%M%S')}"

    kept_sources: list[tuple[str, str]] = []
    already_names: list[str] = []
    drafts = []
    for rec in wanted:
        mutant = mutant_by_id[rec["mutant_id"]]
        mfunc = enclosing_function(module_tree, mutant.lineno)
        result = draft_and_gate(
            client, target, test_file, str(target.module_path), source, test_source,
            mutant, already_names, run_id, out_dir / "trajectories", out_dir, mfunc,
        )
        drafts.append({"mutant_id": mutant.id, "function": mfunc, "lineno": mutant.lineno, **result})
        status = "kept" if result["kept"] else "discarded"
        print(
            f"  {mutant.id}  line {mutant.lineno:<5} {(mfunc or '<module level>'):<20} "
            f"{status} ({len(result['attempts'])} attempt(s))"
        )
        if result["kept"]:
            kept = result["attempts"][-1]
            kept_sources.append((kept["test_name"], kept["test_source"]))
            already_names.append(kept["test_name"])

    reachable_mutants_for_rescore = [mutant_by_id[r["mutant_id"]] for r in wanted]
    official = official_batch_rescore(
        target, test_file, kept_sources, reachable_mutants_for_rescore, timeout=args.timeout
    )
    official_killed = sum(1 for r in official["per_mutant"].values() if r["outcome"] != "survived")

    tokens_in = sum(a["tokens_in"] for d in drafts for a in d["attempts"])
    tokens_out = sum(a["tokens_out"] for d in drafts for a in d["attempts"])

    print(
        f"\n{len(kept_sources)} of {len(wanted)} attempted survivors got a kept test "
        f"({len(wanted) - len(kept_sources)} discarded -- failed the gate on both attempts)."
    )
    if not official["clean_pass"]:
        print(
            "WARNING: the kept tests together do NOT pass on clean source in the official batch "
            "rescore -- something about combining them broke the suite. See the JSON output's "
            "official_clean_output for the failure; nothing was written to the generated-tests file."
        )
    else:
        print(f"official rescore: {official_killed} of {len(wanted)} attempted survivors now killed.")
    print(f"tokens: {tokens_in} in / {tokens_out} out")

    tests_path = Path(args.out_tests) if args.out_tests else out_dir / f"{target.name}_killcheck_tests.py"
    generated_tests_path = None
    if kept_sources and official["clean_pass"]:
        header = (
            f"# Generated by `killcheck harden` for {target.module_path}.\n"
            f"# Each test below passed on clean source and failed on the specific mutation it\n"
            f"# targets. Review before merging into your real suite -- the gate proves a test is\n"
            f"# sensitive to this one mutation, not that it specifies correct behaviour in general.\n"
        )
        tests_path.write_text(header + "\n\n" + "\n\n".join(src for _, src in kept_sources) + "\n")
        generated_tests_path = str(tests_path)
        print(f"\nwrote {len(kept_sources)} test(s) to {tests_path}")
    elif not kept_sources:
        print("\nno tests kept -- nothing written.")

    result = {
        "target": target.name,
        "module": str(target.module_path),
        "canary_verified": canary_verified,
        "attempted": len(wanted),
        "kept": len(kept_sources),
        "discarded": len(wanted) - len(kept_sources),
        "official_clean_pass": official["clean_pass"],
        "official_clean_output": official["clean_output"],
        "official_killed": official_killed,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "drafts": drafts,
        "test_file": str(test_file.relative_to(target.project_root)),
        "generated_tests_path": generated_tests_path,
    }
    path = out_dir / f"{target.name}_harden.json"
    path.write_text(json.dumps(result, indent=2))
    print(f"wrote {path}")
    if not canary_verified:
        print()
        print(_SKIP_CANARY_WARNING)
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def _add_common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("module_path", help="path to the .py module to mutate")
    sp.add_argument(
        "--tests",
        help='test command to run, e.g. "pytest tests/test_foo.py -q". '
        "Auto-discovered from the module's name if omitted.",
    )
    sp.add_argument("--out", help="directory to write results into (default: ./.killcheck)")
    sp.add_argument(
        "--timeout", type=int, default=60,
        help="per-mutant subprocess timeout in seconds (default: 60)",
    )
    sp.add_argument(
        "--tests-env", action="append", default=[], metavar="KEY=VALUE",
        help="set an environment variable before the interpreter starts (repeatable), e.g. "
        "--tests-env PYTHONPATH=src. Needed when -o pythonpath=src (baked into --tests) isn't "
        "enough -- a target that imports itself during its own test runner's startup, before "
        "collection-time path options apply, needs the path set before the interpreter starts.",
    )
    sp.add_argument("--debug", action="store_true", help="show full tracebacks on unexpected errors")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="killcheck",
        description="Mutation testing that tells you which surviving mutants are worth writing a test for.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp_score = sub.add_parser("score", help="mutation score for a module, with the survivor list")
    _add_common(sp_score)
    sp_score.add_argument("--json", action="store_true", help="print machine-readable JSON only")
    sp_score.add_argument(
        "--skip-canary", action="store_true",
        help="skip the pre-flight canary check (faster, unverified -- the output says so loudly)",
    )

    sp_verify = sub.add_parser("verify", help="canary, byte-size canary, determinism gate, reachability")
    _add_common(sp_verify)
    sp_verify.add_argument("--json", action="store_true", help="also print machine-readable JSON")
    sp_verify.add_argument(
        "--skip-determinism", action="store_true",
        help="skip the 3x serial re-run that confirms the survivor set is stable (faster, less certain)",
    )

    sp_harden = sub.add_parser("harden", help="generate and gate tests for reachable survivors (needs ANTHROPIC_API_KEY)")
    _add_common(sp_harden)
    sp_harden.add_argument(
        "--max-survivors", type=int, default=CALL_CAP,
        help=f"cap on survivors to attempt, lowest line number first (default: {CALL_CAP})",
    )
    sp_harden.add_argument(
        "--include-unreachable", action="store_true",
        help="also attempt survivors on lines the suite never executes (almost never killable)",
    )
    sp_harden.add_argument(
        "--out-tests",
        help="where to write the kept generated tests (default: <out>/<name>_killcheck_tests.py)",
    )
    sp_harden.add_argument(
        "--skip-canary", action="store_true",
        help="skip the pre-flight canary check (unverified -- risks spending model calls on "
        "survivors that can never be killed; the output says so loudly)",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    # Line-buffer stdout regardless of whether it's a TTY -- without this,
    # a command piped or redirected (logged in CI, captured by another
    # tool) can interleave progress prints after a later stderr error
    # message, which is confusing even though execution order was correct.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass  # stdout isn't a real TextIOWrapper (e.g. captured in a test) -- harmless to skip

    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {"score": cmd_score, "verify": cmd_verify, "harden": cmd_harden}
    try:
        _apply_tests_env(args.tests_env)
        return handlers[args.command](args)
    except CLIError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(
            f"error: could not run the test command -- {e}. Is it installed and on PATH in "
            f"this environment? (Run killcheck from inside the same virtualenv you run your "
            f"tests from.)",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception:
        if getattr(args, "debug", False):
            raise
        exc = sys.exc_info()[1]
        print(
            f"error: unexpected failure ({type(exc).__name__}: {exc}). Re-run with --debug for "
            f"the full traceback.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
