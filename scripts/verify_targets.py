"""One-off verification: run the frozen runner against every target.json entry.

Not part of the pipeline -- a smoke test for the fetch step. Confirms each
target's suite passes clean, mutants land in the 15-60 band, and prints the
resulting kill_score baseline (pre-agent, pre-baseline-arm) for sanity.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from killcheck.runner import Target, score_target

targets = json.loads((ROOT / "targets.json").read_text())

for t in targets:
    target = Target.from_dict(t, ROOT)
    try:
        rep = score_target(target, timeout=30, workers=4)
        flag = "  [15-60 OK]" if 15 <= rep["total_mutants"] <= 60 else "  [OUT OF RANGE]"
        print(
            f"{t['name']:25s} mutants={rep['total_mutants']:4d} "
            f"killed={rep['killed']:4d} kill_score={rep['kill_score']:.4f}{flag}"
        )
    except Exception as e:
        print(f"{t['name']:25s} FAILED: {e}")
