"""
Runs a test suite against each mutant in an isolated copy of the project.

Ground truth definition, fixed before any agent work begins:
  - killed    : the suite fails on the mutant (the mutation was detected)
  - survived  : the suite passes on the mutant (the mutation went unnoticed)
  - timeout   : the suite did not finish in time; counted as KILLED, because a
                mutation that hangs the suite is detected by the suite.
  - error     : the mutant did not import / collect; counted as KILLED for the
                same reason, but reported separately so it can be audited.

Kill score = killed / total_mutants.

score_target()'s default is workers=1 (serial). This was workers=4 originally;
changed after a real-world target (aiofiles-temptypes, which runs real async
I/O against real temp files) produced a different survivor SET on every
concurrent run -- confirmed by running it 4x at workers=4 (three different
kill scores) and 2x at workers=1 (identical survivor set both times). Three
other targets showed no difference between workers=4 and workers=1, so this
is not a general correctness bug in the isolation strategy (_evaluate_one
already runs each mutant in its own tempdir copy and subprocess) -- it is a
real target doing concurrent async I/O against a shared filesystem, and
runner.py has no way to tell "the mutation broke it" apart from "concurrent
execution broke it." The bias runs one direction only: a spurious concurrent
failure reads as a kill, and kill counts are the quantity every arm in this
project is trying to increase. Non-determinism here does not average out; it
flatters. See CHANGELOG.md for how this was found and what it changed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from pathlib import Path

from .engine import Mutant, generate_mutants


@dataclass
class MutantResult:
    mutant_id: str
    module: str
    lineno: int
    operator: str
    description: str
    original_line: str
    mutated_line: str
    outcome: str  # killed | survived | timeout | error
    duration_s: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Target:
    """One evaluation case."""

    name: str
    project_root: Path
    module_path: Path  # module under mutation, relative to project_root
    test_command: list[str]  # e.g. ["python", "-m", "pytest", "tests/test_x.py", "-x", "-q"]

    @classmethod
    def from_dict(cls, d: dict, base: Path) -> "Target":
        return cls(
            name=d["name"],
            project_root=(base / d["project_root"]).resolve(),
            module_path=Path(d["module_path"]),
            test_command=d["test_command"],
        )


def _run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout + proc.stderr)[-4000:]
    except subprocess.TimeoutExpired:
        return -9, "TIMEOUT"


def verify_clean(target: Target, timeout: int = 120) -> None:
    """The suite must pass on unmutated code, or the whole run is meaningless."""
    code, output = _run(target.test_command, target.project_root, timeout)
    if code != 0:
        raise RuntimeError(
            f"[{target.name}] test suite does not pass on clean code "
            f"(exit {code}). Fix this before measuring anything.\n{output[-1500:]}"
        )


def _evaluate_one(target: Target, mutant: Mutant, timeout: int) -> MutantResult:
    import time

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="killcheck-") as tmp:
        work = Path(tmp) / "project"
        shutil.copytree(
            target.project_root,
            work,
            ignore=shutil.ignore_patterns(
                "__pycache__", ".git", ".pytest_cache", "*.pyc", ".venv", "node_modules"
            ),
        )
        (work / target.module_path).write_text(mutant.source)
        code, output = _run(target.test_command, work, timeout)

    duration = time.monotonic() - started

    if code == -9:
        outcome = "timeout"
    elif code == 0:
        outcome = "survived"
    elif "ERROR" in output and "collected 0 items" in output:
        outcome = "error"
    else:
        outcome = "killed"

    return MutantResult(
        mutant_id=mutant.id,
        module=str(target.module_path),
        lineno=mutant.lineno,
        operator=mutant.operator,
        description=mutant.description,
        original_line=mutant.original_line,
        mutated_line=mutant.mutated_line,
        outcome=outcome,
        duration_s=round(duration, 2),
    )


def score_target(
    target: Target,
    timeout: int = 60,
    workers: int = 1,
    check_clean: bool = True,
) -> dict:
    if check_clean:
        verify_clean(target)

    source = (target.project_root / target.module_path).read_text()
    mutants = generate_mutants(source, str(target.module_path))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(
            pool.map(lambda m: _evaluate_one(target, m, timeout), mutants)
        )

    killed = sum(1 for r in results if r.outcome in ("killed", "timeout", "error"))
    total = len(results)

    return {
        "target": target.name,
        "module": str(target.module_path),
        "total_mutants": total,
        "killed": killed,
        "survived": total - killed,
        "kill_score": round(killed / total, 4) if total else 0.0,
        "results": [r.to_dict() for r in results],
    }


def survivors(report: dict) -> list[dict]:
    """The agent's work queue: mutations the current suite fails to detect."""
    return [r for r in report["results"] if r["outcome"] == "survived"]


def write_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2))
