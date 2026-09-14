"""Conservation invariants: counts that must reconcile at every stage that
produces one. CHECK B of the scorer-validation work -- see CHANGELOG.md's
"The scorer itself was never checked" entry for why this exists.

None of this validates that a mutant was scored correctly (that's CHECK A,
scripts/test_scorer_checks.py's known-outcome fixtures). This validates
that the AGGREGATION arithmetic never silently drops or duplicates a
mutant, a test, or a target on the way from a per-mutant outcome to a
published pooled number. A sum that stops reconciling is not a rounding
quirk to shrug off -- it means something was lost or double-counted
somewhere between two stages that are each individually plausible, which
is exactly how three of this project's own instrument bugs happened
(batched rows scored as one row, dropped imports manufacturing failures,
a misclassified assertion type) -- none of which would have been caught by
a check like this one, but all of which are the same SHAPE of bug this
guards against on the arithmetic side.

Every function here raises AssertionError on violation, deliberately, not
a logged warning: METHODOLOGY.md's own rule for the kill gate ("no exceptions,
no close enough") applies here too -- a number that doesn't reconcile
means every number built from it is suspect, and the caller must ABORT,
not proceed with a footnote. Every function takes an explicit `context`
string so a caller five stack frames away from the actual bug still says
which target/arm/stage failed, not just "assertion failed" with no address
to go debug.
"""
from __future__ import annotations


def assert_outcome_conservation(counts: dict, total: int, context: str) -> None:
    """killed + survived + timeout + error must equal the total mutant
    count, always -- these four outcomes are exhaustive and mutually
    exclusive by construction (every result has exactly one `outcome`
    field), so anything else means a mutant was lost or duplicated between
    generating the mutant list and tallying outcomes for it."""
    summed = sum(counts.get(k, 0) for k in ("killed", "survived", "timeout", "error"))
    if summed != total:
        raise AssertionError(
            f"[{context}] outcome counts do not conserve: {counts} sums to {summed}, "
            f"expected total_mutants={total}"
        )


def assert_reachability_conservation(reach: dict, total: int, context: str) -> None:
    """reachable_survivor + unreachable + unknown_reachability_survivor +
    killed must equal the total mutant count. Every mutant is partitioned
    into exactly one of these four buckets by build_mutant_records/
    summarize_reachability (METHODOLOGY.md's Denominator section) -- the
    unknown bucket exists specifically so a coverage-measurement failure
    degrades to "don't know," not to a silently wrong classification, and
    it must still be counted here or the identity breaks whenever it's
    nonzero."""
    summed = sum(
        reach.get(k, 0)
        for k in ("reachable_survivor", "unreachable", "unknown_reachability_survivor", "killed")
    )
    if summed != total:
        raise AssertionError(
            f"[{context}] reachability counts do not conserve: {reach} sums to {summed}, "
            f"expected total_mutants={total}"
        )


def assert_pooled_conservation(pooled: int, per_target: list[int], context: str) -> None:
    """A pooled figure this project publishes (pooled reachable survivors,
    pooled killed, ...) must equal the sum of the per-target figures it was
    built from -- METHODOLOGY.md's primary metric is defined as exactly this
    sum, so if it ever doesn't reconcile the pooled number is not the
    metric METHODOLOGY.md defines, whatever else it is."""
    expected = sum(per_target)
    if pooled != expected:
        raise AssertionError(
            f"[{context}] pooled figure {pooled} != sum of per-target figures {expected} "
            f"(per-target values: {per_target})"
        )


def assert_ids_subset(ids: set, universe: set, context: str) -> None:
    """Every id in `ids` must exist in `universe` -- e.g. every mutant_id a
    results file mentions must be a real mutant_id for that target in
    target_verification.json, not a stale id from a previous engine.py
    version or a typo that would silently score as "not found" downstream
    instead of raising here."""
    missing = ids - universe
    if missing:
        raise AssertionError(
            f"[{context}] {len(missing)} id(s) not present in the expected universe: "
            f"{sorted(missing)[:10]}{'...' if len(missing) > 10 else ''}"
        )


def assert_no_duplicates(ids: list, context: str) -> None:
    """A work queue (e.g. the reachable-survivor ids an arm is about to
    score) must not contain the same id twice -- a duplicate silently
    doubles that mutant's weight in a pooled figure without doubling the
    denominator, which inflates a rate without anyone asking it to."""
    seen = set()
    dupes = set()
    for i in ids:
        if i in seen:
            dupes.add(i)
        seen.add(i)
    if dupes:
        raise AssertionError(f"[{context}] duplicate id(s) in work queue: {sorted(dupes)}")
