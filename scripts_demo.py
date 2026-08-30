"""Smoke test: proves the frozen core works. Expect kill_score ~= 0.095."""
from pathlib import Path
from killcheck.runner import Target, score_target, survivors

t = Target(
    name="fixture-bank",
    project_root=Path(__file__).parent / "fixture",
    module_path=Path("bank.py"),
    test_command=["python3", "-m", "pytest", "tests", "-x", "-q"],
)
rep = score_target(t, timeout=30, workers=4)
print(f"mutants={rep['total_mutants']} killed={rep['killed']} "
      f"survived={rep['survived']} kill_score={rep['kill_score']}")
for s in survivors(rep)[:5]:
    print(f"  {s['mutant_id']} L{s['lineno']} {s['operator']}: {s['description']}")
