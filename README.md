# killcheck

An agentic workflow that hardens test suites: it finds mutations of the
source that the existing tests fail to detect, writes tests that detect
them, and verifies each new test against a hard gate before keeping it.

Submission for the micro1 Frontier Engineering Challenge 2026. See
[CLAUDE.md](CLAUDE.md) for the full experimental design — invariants,
metrics, the agent loop contract, and the eval set. See
[CHANGELOG.md](CHANGELOG.md) for findings and decisions in the order they
happened. Results land in `results/` once the arms have run; this file gets
a Results section then, not before.

## Limitations

Stated now, before any arm has run, so scope is fixed before it could be
narrowed to fit whatever the numbers turn out to be.

**Out of scope for this submission:**

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
  catches a mutant that is reachable, executed, and still behaviorally
  equivalent to the original for every input any test happens to construct
  (a classic undecidable problem in general). This project does not attempt
  automated equivalence detection beyond those two mechanical checks. The
  only manual check is a 20-item stratified sample of surviving mutants,
  audited by hand for equivalence, reported as a sample finding rather than
  a corrected metric.

None of the above are treated as blocking issues to work around under time
pressure. They're the honest edges of what this submission measures.
