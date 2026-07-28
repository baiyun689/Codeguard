# TradeFlow Complex PR Benchmark v1

`complex-pr-v1` is a controlled project-level Java code-review dataset.
It is not a collection of real open-source PRs. A clean, complete
seven-module Spring Boot project is used as the common baseline, and
every case contains a fully materialized independent PR snapshot.

## Contract

- 20 independent PR cases and exactly 3 gold issues per PR.
- Every reviewed snapshot contains all seven Maven modules.
- `changes.diff` is the only review patch.
- `ground-truth.yaml` and `oracle-tests/` are evaluator-only and must
  never be mounted into the Gateway project snapshot.
- Risk/knowledge coverage is frozen at 36 exact, 16 composite and 8 gap
  issues. Gap names are annotation vocabulary, not production RiskTags.

## Rebuilding

Run `python build_dataset.py` from this directory to deterministically
rebuild the baseline and all committed snapshots. Rebuilding does not
invoke Codeguard, an LLM, Maven, or the oracle tests.
