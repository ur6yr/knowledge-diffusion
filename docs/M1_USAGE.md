# Original count workflow

The original bounded publication-count workflow remains available for compatibility. Its frozen count witnesses still replay through `kdiff replay`.

For current construction, analysis, GPU/API deployment and recovery commands, use [the application guide](USAGE.md) and [the project README](../README.md). [Validation status](STATUS.md) distinguishes the original local milestone from subsequent work and unexecuted infrastructure checks.

The legacy demonstration is `scripts/m1_smoke.py`. Run it as the command under `deploy/local/run.py` with a fresh owned Neo4j root. It imports `tests/fixtures/m1_openalex.jsonl` twice, asks a synthetic count question, and replays the answer. Its expected count of two is a fixture invariant, not a published research result.
