# Running the bounded M1 slice

This is a local development path using an existing verified Neo4j distribution. For project-only transfer, environment setup and the bounded Slurm pilot, see the [Rivanna README instructions](../README.md#rivanna-setup). Cluster execution and M5 recovery remain unvalidated. Do not run servers on a cluster login node.

Create a separate application environment, then install the tested dependencies and package:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/kdiff doctor
.venv/bin/python -m pytest -q
```

The lock records this macOS/Python 3.13 installation. Rivanna/Linux compatibility is not yet established. CPU application dependencies contain no GPU serving stack. Preserve `config.sh`/`$OA_ENV`; the README uses Miniforge for a separate cluster environment and requires checking installed modules. On this machine Python 3.13 skipped editable `.pth` files as hidden, so a normal wheel install was used. During development, `PYTHONPATH=src .venv/bin/python -m kdiff ...` runs the working source without reinstalling.

Without `KDIFF_SERVICE`, database integration tests skip explicitly. To exercise the real database gate with the distribution found during audit:

```sh
PYTHONPATH=src .venv/bin/python deploy/local/run.py \
  --home /opt/homebrew/Cellar/neo4j/2026.02.2/libexec \
  --java-home /opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home \
  --root .runtime/my-new-m1-tests \
  -- .venv/bin/python -m pytest -q --junitxml=artifacts/test-results/full.xml
```

Choose a fresh directory every time. The launcher refuses existing directories and occupied/default service ports. It binds Bolt to `127.0.0.1:17687`, disables HTTP, creates a restricted random development password, waits for an authenticated query, initializes only an empty owned store, and stops only its child when the command ends. It never copies, dumps, migrates, starts against, or stops another store. No Docker daemon or root is needed. Test stores/logs remain for inspection; this is not a backup/restore implementation.

A persistent end-to-end demonstration writes CLI commands, two import receipts, the release, all role events and the answer/witness under `artifacts/m1/`:

```sh
PYTHONPATH=src .venv/bin/python deploy/local/run.py \
  --home /opt/homebrew/Cellar/neo4j/2026.02.2/libexec \
  --java-home /opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home \
  --root .runtime/my-new-m1-demo \
  -- .venv/bin/python scripts/m1_smoke.py
```

`scripts/m1_smoke.py` invokes separate `build`, `snapshot`, `ask`, and `replay` CLI processes. Construction uses native-shaped, explicitly synthetic fixture records. `ask` currently accepts an explicit canonical Author ID and normalized dates; general natural-language interpretation remains M3. The result is two distinct papers for the fixture author in 2016–2020, computed from Neo4j and independently checked against complete saved operands. Integration tests add a record and obtain three, while the old witness remains two.

With the development service running, the independent commands have this shape:

```sh
.venv/bin/kdiff --service SERVICE_JSON --store ARTIFACT_DIR build \
  --input tests/fixtures/m1_openalex.jsonl --namespace fixture:example \
  --profile configs/mock.json --allow-mock
.venv/bin/kdiff --service SERVICE_JSON --store ARTIFACT_DIR snapshot --namespace fixture:example
.venv/bin/kdiff --service SERVICE_JSON --store ARTIFACT_DIR ask \
  --release RELEASE_HASH --author CANONICAL_AUTHOR_ID \
  --start 2016-01-01 --end 2020-12-31 --reference-date 2025-01-01 \
  --profile configs/mock.json --allow-mock
.venv/bin/kdiff --store ARTIFACT_DIR replay --run RUN_HASH
.venv/bin/kdiff --store ARTIFACT_DIR replay --witness WITNESS_HASH
```

`SERVICE_JSON`, hashes and IDs above are placeholders, not configured endpoints. `artifacts/m1/result.json` records concrete values from the executed demo. Replay needs only its artifact directory and installed code, and works after Neo4j has stopped. Keep the entire content-addressed store: a witness references its release and all raw operands. Missing files or changed hashes fail visibly. A hash is an integrity check, not a signature from a trusted authority; preserve the original witness/release IDs when sharing artifacts.

New questions require the current graph to match the release hash under the shared single-host application lock. Construction changes require a new snapshot. All M1 writers must use that lock; this is not protection against arbitrary administrators or a tested distributed snapshot protocol.

`sources` reports every required connector's implementation/configuration state. The pending `seed`, `reconcile`, `maintain`, `chat`, `evaluate`, and `benchmark-build` commands exit with status 2. Live provider constructors share a factory, but execution remains blocked until M5 capability and budget controls are implemented and tested. No automatic paid fallback exists.
