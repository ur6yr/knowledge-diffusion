# Application usage

Run from an installed application environment. `python -m kdiff COMMAND --help` shows exact options. Global `--store`, `--ledger`, `--service` and `--postgres-artifacts` options precede the subcommand.

## Owned Neo4j and a complete fixture job

Use only an existing binary distribution and a fresh development root. This example runs the full source-to-answer job using explicitly synthetic data and scripted model responses.

```bash
PYTHONPATH=src .venv/bin/python deploy/local/run.py \
  --home /absolute/path/to/neo4j-distribution \
  --java-home /absolute/path/to/java-21 \
  --root .runtime/fixture-job-01 \
  --store artifacts/fixture-job/store \
  --checkpoint-namespace fixture:job-demo \
  -- .venv/bin/python scripts/run_job.py \
  --job configs/job.fixture.json --profile configs/mock.json --allow-mock \
  --store artifacts/fixture-job/store --ledger artifacts/fixture-job/tasks.sqlite \
  --output artifacts/fixture-job/result.json
```

The parent starts a fresh authenticated database, publishes `KDIFF_SERVICE` to the child, waits for the bounded command, freezes the selected namespace and stops its own server. A new root and output path are required for another invocation. The command timeout defaults to 600 seconds and can be set explicitly up to 24 hours.

For tests against actual Neo4j, replace the command after `--` with `.venv/bin/python -m pytest -q --junitxml=artifacts/tests.xml`. Without this owned service, database tests skip.

## Independent construction and analysis

Run the following commands inside a command or shell managed by the owned launcher so `KDIFF_SERVICE` is available. Keep a single artifact store for the entire release.

```bash
python -m kdiff --store artifacts/project/store build \
  --source openalex --input /absolute/path/to/bounded-native-works.jsonl \
  --namespace local:pilot --profile /absolute/path/to/authorized-profile.json

python -m kdiff --store artifacts/project/store snapshot --namespace local:pilot
python -m kdiff --store artifacts/project/store inspect --release RELEASE_HASH

python -m kdiff --store artifacts/project/store ask \
  --release RELEASE_HASH --request /absolute/path/to/request.json \
  --profile /absolute/path/to/authorized-profile.json
```

For ORCID and ROR use `--source orcid --format json` or `--source ror --format json-array`. USPTO uses `--source uspto --format xml`. PatCit needs `--columns` pointing to the mapping described in [source formats](SOURCES.md). Unknown source formats fail instead of dropping fields silently.

An independent analysis request might be:

```json
{
  "family": "comparison",
  "kind": "Institution",
  "identifier": "CANONICAL_INSTITUTION_ID_FROM_INSPECT",
  "window": {
    "start": "2020-01-01",
    "end": "2022-12-31",
    "reference_date": "2025-01-01"
  },
  "before": {
    "start": "2016-01-01",
    "end": "2018-12-31",
    "reference_date": "2025-01-01"
  },
  "topic_id": "CANONICAL_TOPIC_ID_FROM_INSPECT",
  "ask_causation": true
}
```

The returned counts and ratio are tool-computed. `ask_causation` retains the requested hypothesis and its visible NEI refusal when only temporal association is available. For institutional output, the aggregate uses publication-time authorship institution IDs, not an author's later employment.

| Family | Subject | Additional fields |
|---|---|---|
| `identity` | Any registered entity type | Source/canonical ID or name with explicit context |
| `count` | Author or Institution | Optional topic and publication-attribution institution |
| `comparison` | Author or Institution | Matched `before` and `window` ranges |
| `sites` | Institution | Operational records and announcements remain distinct |
| `timeline` | Author | Observed affiliation assertions overlapping the window |
| `state` | Author | Finite observed event trace with concurrent/unknown affiliations |
| `returns` | Author | Documented A-to-B-to-A paths, uncertain candidates and elapsed date bounds |
| `path` | A known entity | `target_id`, allowed `relations`, `max_hops`, `temporal_rule` |

Paths use explicit relation direction and either `nondecreasing` or `within_window` temporal constraints. Citation paths do not become causal knowledge-transfer claims. The ten operators are `ResolveEntity`, `NormalizeTime`, `FilterInterval`, `Expand`, `PathSearch`, `Aggregate`, `CompareWindows`, `ProjectProvenance`, `ReadState` and `UpdateState`.

Natural-language analysis needs a live model and an explicit time scope:

```bash
python -m kdiff --store artifacts/project/store ask \
  --release RELEASE_HASH \
  --question "How many recorded papers did this identified researcher publish?" \
  --start 2016-01-01 --end 2020-12-31 --reference-date 2025-01-01 \
  --profile /absolute/path/to/authorized-profile.json
```

Supply an actual name or identifier in the question. Missing or ambiguous identity yields clarification. Add `--before-start` and `--before-end` for comparisons. The typed `--request` form is preferable for repeatable experiments.

## Dialogue and evidence

```bash
python -m kdiff --store artifacts/project/store --ledger artifacts/project/tasks.sqlite chat \
  --dialogue institution-case --release RELEASE_HASH --request turn-1.json \
  --profile /absolute/path/to/authorized-profile.json
```

Subsequent typed requests can set `use_previous_entity` to true. They retain resolved IDs and the pinned release outside free-form history. Every changed window triggers fresh retrieval and a new witness. A dialogue cannot silently switch its release.

```bash
python -m kdiff --store artifacts/project/store replay --run ANALYSIS_RUN_HASH
python -m kdiff --store artifacts/project/store replay --witness WITNESS_HASH
```

Replay uses no database, network or model. It verifies raw operand hashes, recomputes the program, checks claim entailment and reconstructs proof coverage. Compact display cards reference complete source and derivation operands. A witness budget may be relaxed rather than omit required evidence. Costs currently count evidence cards, not model tokens.

## Durable construction tasks

A queue manifest is a JSON array of objects containing `input`, `namespace` and optional `source`, `format` and `columns`. Input paths must be accessible from the worker. Submission records file hashes, code revision, schema and provider fingerprint. Changed inputs need a new task.

```bash
python -m kdiff --store artifacts/project/store --ledger artifacts/project/tasks.sqlite \
  queue-build --manifest batches.json --profile configs/mock.json --allow-mock
python -m kdiff --store artifacts/project/store --ledger artifacts/project/tasks.sqlite \
  worker --max-tasks 1 --profile configs/mock.json --allow-mock
python -m kdiff --ledger artifacts/project/tasks.sqlite tasks
```

Extraction caching is disabled by default. Add `--extraction-cache` to a build or worker command for an explicit experiment. The returned hit counters distinguish local memory, optional Redis and durable artifact reuse. Correctness is checked with caching both disabled and enabled.

Mock profiles require fixture namespaces. Each worker invocation handles at most three source batches under one inference budget. Writes are coordinated with a host file lock. This is at-least-once delivery with an idempotent graph commit, not global exactly once. A durable write intent plus a fenced task transaction handles the graph-commit/acknowledgment crash window. Expired leases can be reclaimed on a later worker invocation.

For dedicated PostgreSQL tables, use `--postgres-artifacts`, configure `KDIFF_POSTGRES_DSN` privately, and initialize with `init-state --postgres`. `worker --redis` uses `KDIFF_REDIS_URL` privately and reconstructs construction deliveries from authoritative task state. The owned metadata launcher sets both credentials without printing them. Do not point initialization or tests at an existing user's database.

## Seeds and public documents

```bash
python -m kdiff seed --input /absolute/path/to/existing/cohort.xlsx \
  --release "inspected release label" --sheet Data --name-column authfull --limit 2
```

The XLSX limit explicitly selects a worksheet prefix. It does not imply complete cohort coverage or verified identities. CSV imports use their inspected column names and fail if their declared record budget is exceeded.

A public-fetch policy lists exact allowed hostnames, request/byte/time caps, pacing and redirect limits. For example:

```json
{"allowed_hosts": ["www.example.edu"], "max_calls": 5, "max_bytes": 1048576, "max_seconds": 60}
```

```bash
python -m kdiff --store artifacts/project/store capture-web \
  --url https://www.example.edu/research/profile --policy policy.json
python -m kdiff --store artifacts/project/store build-web \
  --document DOCUMENT_HASH --namespace local:public-cv \
  --profile /absolute/path/to/authorized-profile.json
```

Only configure a real public institutional source you are permitted to access. Robots restrictions, private destinations, credential-bearing URLs and redirects outside the allowlist are rejected. HTML/PDF transforms and exact text offsets are retained. Embedded document instructions never add tool permissions. Semantic extraction quality still needs independent review.

## Recovery and live tests

```bash
PYTHONPATH=src .venv/bin/python scripts/check_recovery.py \
  --home /absolute/path/to/neo4j-distribution --java-home /absolute/path/to/java-21 \
  --root .runtime/recovery-check-01 --artifacts artifacts/recovery-check-01
```

This checks a real stop and logical restore into a second fresh Neo4j store. It verifies graph hashes, batch reuse and different service generations. It does not establish Slurm wall-time or node-loss recovery.

Live test opt-ins are `KDIFF_RUN_LIVE_TESTS=1` with `KDIFF_LOCAL_TEST_PROFILE` or `KDIFF_OPENAI_TEST_PROFILE`. These perform real requests and require explicit authorized budgets. PostgreSQL/Redis tests require `KDIFF_RUN_METADATA_TESTS=1` and the `KDIFF_METADATA_SERVICE` manifest created by the owned launcher. Unavailable services skip explicitly.

## SSH endpoint diagnostics

Application and graph clients run inside the co-located allocation. No tunnel is needed for the normal job. If your site permits SSH to your allocated compute node, you can temporarily forward an owned loopback endpoint for diagnostics while that allocation is running:

```bash
ssh -N -J YOUR_COMPUTING_ID@login.hpc.virginia.edu \
  -L 127.0.0.1:28000:127.0.0.1:18000 \
  YOUR_COMPUTING_ID@ACTUAL_COMPUTE_HOST_FROM_SERVICE_MANIFEST
```

This keeps the forwarded port on your workstation's loopback interface. Authentication remains required. Use your existing institution-approved SSH configuration and keep credentials in environment variables or restricted secret files. A tunnel does not enable distributed graph mode or change manifest hostname checks. Closing the SSH session removes the tunnel, and the job launcher still owns the service lifecycle.
