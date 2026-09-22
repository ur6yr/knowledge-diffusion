# Evaluation protocol

Quality scores require stored executions and independent annotations. The system's own verifier is not accepted as unquestioned gold. The original graph, 215 questions and annotation artifacts were not supplied, so no original benchmark score is produced.

## Import a supplied benchmark

```bash
python -m kdiff --store artifacts/project/store benchmark-build \
  --input /absolute/path/to/questions.jsonl --name supplied-original --origin original
```

Each JSONL record follows `kdiff.evaluation.benchmark.Item`: `item_id`, `family`, `turns`, `difficulty`, `exposure_stratum`, `release_id`, `reference_date`, `gold_entities`, `gold_windows`, `required_claims`, `acceptable_rewrites`, `minimum_witnesses` and `annotation_provenance`. Annotation provenance needs a nonempty `source`. Gold entities must exist in the pinned release, and all gold windows share the reference date.

An `original` import must contain the described family counts: entity resolution 28, temporal existence 32, mobility sequence 26, pre/post comparison 38, diffusion path 30, patent pathway 21 and multi-turn dialogue 40. These counts validate a supplied artifact. They never generate missing questions. Synthetic and independent suites use different names and explicit origins.

## Independent annotations

Annotations follow `kdiff.evaluation.metrics.Annotation`. Each row records one complete item or dialogue, its stored run IDs, variant, factual assertions/correct assertions/unsupported assertions, required/recovered claims, Partial candidates/handled candidates, refusals/correct refusals, final witness IDs, optional identity/temporal/plan-validity judgments, reviewer, method, review date and an immutable annotation-artifact pointer.

Accepted review methods are `independent_human` and `independent_adjudicated_gold`. These fields record supplied provenance. The software does not claim review took place merely because a schema was filled out. Calibrate reviewers separately and retain their actual judgments in the cited artifact.

```bash
python -m kdiff --store artifacts/project/store evaluate \
  --benchmark BENCHMARK_HASH --annotations /absolute/path/to/annotations.jsonl
```

The evaluator checks every dialogue turn is present, the release matches, factual-assertion and witness denominators match stored runs, and every witness replays. Missing required operands are witness failures. Unannotated items remain explicitly listed.

Factual precision and unsupported-claim rate divide by factual assertions, excluding refusals. Zero denominators are JSON `null`, not perfect accuracy or zero error. Required-claim recall is reported separately so narrowing a Partial claim does not automatically recover a stronger requested claim. Partial handling and correct refusals have separate denominators. Answer, clarification and abstention coverage are item-level. A dialogue receives Answer coverage only if every dependent turn answers.

## Paired bootstrap and experimental controls

```bash
python -m kdiff --store artifacts/project/store evaluate \
  --benchmark BENCHMARK_HASH --annotations /absolute/path/to/paired-annotations.jsonl \
  --compare full comparison_variant --bootstrap-seed 2026
```

The default bootstrap performs 10,000 resamples with a recorded seed. Both arms use the same sampled complete item IDs. Dependent dialogue turns are never sampled as independent claims. Undefined resamples are counted, and the report includes a percentile 95% interval for right-minus-left claim correctness.

Cache keys include benchmark, item, variant, model/profile fingerprint and seed. The evaluator rejects attempts to label a stored full-method final answer as an ablation. Automated baseline and ablation execution is not implemented yet. Importing independently recorded baseline results still needs the same stored-run and annotation contracts. No custom agent is relabeled as a historical closed product.

Construction tests record actual wall time, input records, duplicate submissions, graph entities/assertions, aggregate throughput and throughput per worker. GPU-hours are `null` when unmeasured. Coordinated writer tests establish bounded convergence, not parallel scaling speedups. Synthetic mock-model timings cannot estimate national rebuild time or GPU inference performance.
