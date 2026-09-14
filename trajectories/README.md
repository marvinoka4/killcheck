# trajectories/

One JSONL file per run, one row per model turn, written live as the run
happens — never reconstructed afterward. This is the raw turn-by-turn record;
`results/generated_tests.jsonl` and `results/agent_arm_c.json` hold the
richer per-draft data (mechanical features, assertion class, official
scoring) derived from the same runs.

## Which file is which

| file | arm | targets covered | rows |
| --- | --- | --- | --- |
| `armA-20260830-143134.jsonl` | A (single prompt) | all 10 scoring targets | 10 |
| `armB-20260830-144157.jsonl` | B (budget-matched) | all 10 scoring targets | 53 |
| `armC-20260831-101121.jsonl` | C (agent) | `slugify-special` (smoke test) | 4 |
| `armC-20260831-102123.jsonl` | C (agent) | `tenacity-stop` | 89 |

Arm C stopped after these two targets when the Anthropic account's API
credit was exhausted on `cachetools-func`'s first call — see CHANGELOG.md's
"Arm C's results" entry. There is no third arm C file; the run never
reached a third target.

The `run_id` embedded in each filename (`arm{A,B,C}-YYYYMMDD-HHMMSS`) is the
`run_id` baseline.py/agent.py generated at the start of that invocation. Two
separate arm C invocations (the smoke test, then the full `tenacity-stop`
run) produced two separate files — nothing here spans a restart.

## Row schema

Every row:

```json
{"ts": "2026-08-31T10:21:...Z", "target": "...", "mutant_id": "M-abc123",
 "phase": "generate|gate_clean|gate_mutant|decision",
 "prompt_tokens": 0, "completion_tokens": 0, "content": "...",
 "outcome": "...", "truncated": false}
```

- **`target`** — target name from `targets.json`.
- **`mutant_id`** — the specific mutant this turn concerns. Empty string
  (`""`) for arms A and B, which have no per-mutant targeting (see
  METHODOLOGY.md's Logging section) — every row for those two arms is one
  `generate` call for the whole target.
- **`phase`** — arms A and B only ever log `generate` (one call, no gate, no
  retry, by design). Arm C logs all four:
  - `generate` — the model call. `outcome` is `attempt1` or `attempt2`.
  - `gate_clean` — did the drafted test pass on unmutated source. `outcome`
    is `pass` or `fail`.
  - `gate_mutant` — only logged if `gate_clean` passed. Did the test fail
    (kill) the mutant. `outcome` is `killed` or `survived`.
  - `decision` — what happened to this attempt. `outcome` is `kept`,
    `retry` (gate rejected, attempt 2 about to fire), or `discarded`
    (gate rejected attempt 2, or attempt 2 doesn't exist and attempt 1 was
    rejected with no retry left).
- **`truncated`** — true when a `generate` call's `completion_tokens` hit
  the call's `max_tokens` ceiling (see CHANGELOG's truncation-handling
  entry). Always `false` for `gate_clean`/`gate_mutant`/`decision` rows,
  which don't call the model.
- **`content`** — for `generate`, the extracted test source (after fence
  extraction, truncation salvage, and arm C's one-test-per-response cap).
  For `gate_clean`/`gate_mutant`, the tail of the actual pytest output.
  Empty for `decision`.

Row counts don't always divide evenly: `armC-...-102123.jsonl` has 89 rows
across 23 attempts (14 mutants, 9 retried), not the 92 you'd get from
4 rows × 23 attempts, because 3 of those 23 attempts failed `gate_clean` and
so never reached `gate_mutant` — confirmed directly, not a gap in the log.

## Following one mutant end to end

`M-22369c8f` in `armC-20260831-102123.jsonl` (`tenacity-stop`), retried once
and kept:

```
generate    attempt1                     <- first draft
gate_clean  fail                         <- attempt 1 didn't even pass clean
decision    retry                        <- gate rejects, retry fires
generate    attempt2                     <- second draft, real pytest output fed back
gate_clean  pass                         <- attempt 2 passes on clean source
gate_mutant killed                       <- attempt 2 fails on the mutant -> a real kill
decision    kept                         <- both gate conditions met, test is kept
```

To pull this yourself:

```bash
python3 -c "
import json
rows = [json.loads(l) for l in open('trajectories/armC-20260831-102123.jsonl')]
for r in rows:
    if r['mutant_id'] == 'M-22369c8f':
        print(r['phase'], '|', r['outcome'])
"
```

For the actual test source at each attempt, the mechanical features
computed on it, and how this mutant's kept test scored in the official
batch rescore, cross-reference `mutant_id` against
`results/agent_arm_c.json`'s `per_mutant_drafts` (per-attempt detail) and
`official_per_mutant` (final scoring, with which kept test(s) were
responsible for the kill).

## For arms A and B

Arms A and B have no per-mutant targeting or gate, so there's no
`decision`/`gate_*` sequence to follow — one `generate` row per call is the
whole record. `armA-...jsonl` has exactly 10 rows (one call per target);
`armB-...jsonl` has 53 (one call per reachable survivor, capped at 25 per
target, matching `CALL_CAP` in `killcheck/baseline.py`). What each call
actually produced (test count, clean-pass, kill count) is in
`results/baseline_arm_a.json` / `results/baseline_arm_b.json`, scored per
batch rather than per call — see METHODOLOGY.md's Logging section for why arms
A/B's `test_source` in `results/generated_tests.jsonl` is a whole target's
batch, not one test.
