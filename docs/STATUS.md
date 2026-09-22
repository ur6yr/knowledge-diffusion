# Validation status

This is a reconstructed, bounded research implementation. It is not a reproduction of the original empirical snapshot, benchmark scores or hardware performance. Status was recorded on 22 September 2026.

| Area | Implemented | Executed locally | Rivanna or live provider |
|---|---|---|---|
| Five construction and seven analysis AutoGen role families | Yes | Actual AssistantAgent/tool traces with explicit mock inference and real Neo4j | Not run |
| Native source readers | OpenAlex JSONL, ORCID v3 JSON, ROR v2 JSON, USPTO XML, mapped PatCit CSV/JSON | Synthetic native-format fixtures | Real source captures not supplied |
| Public documents | Allowlisted HTTPS, public DNS/IP validation, redirect checks, bounded capture, exact text spans, AutoGen extraction | Parser/security fixtures and a synthetic five-role extraction | Live website/model extraction not run |
| Discovery seeds | CSV and bounded XLSX prefix import | Two actual rows from an existing 83,649,050-byte local workbook, SHA-256 `ceeb315346dbdc6b859d47c64ef30af45f686d35fe632e554a252f568e5d42fe` | No identity resolution or national expansion run |
| Analysis grammar | All ten named operators | Type checks, Institute X comparison, sites, paths, state and return tools | Live planning not run |
| Claims and witnesses | Six labels, Partial narrowing, constrained rendering, proof coverage, complete replay | Numeric/source mutations, missing operands, causal refusal, network-disabled replay | No scientific accuracy claim from mock tests |
| File artifacts and SQLite tasks | Versioned captures, durable intent, fencing, checkpoints, bounded coordinated workers and extraction cache | Local unit and Neo4j integration checks | Cross-node SQLite is unsupported |
| PostgreSQL and Redis | Immutable BYTEA/JSONB artifacts, authoritative tasks, BLMOVE delivery and reconstruction, optional L2 cache class, owned launchers | No server binaries available | Two explicit live-service test skips |
| Local service recovery | Fresh directories, ownership, hostname/generation/expiry, bounded shutdown, logical restore | A stopped Neo4j release restored into another fresh store with the same graph hash and reused batch receipt | Slurm signal propagation, node loss and metadata export/restore not run |
| GPU serving | Authenticated co-located vLLM launcher, separate environment or read-only SIF, GPU/version inventory and capability gate | Configuration and client guard tests | No GPU, checkpoint or serving runtime supplied |
| OpenAI | Actual AutoGen client, explicit authorization, shared run budgets and dated cost accounting | Admission, timeout and capability-report validation | No authorized credential/model/budget supplied |
| Evaluation | Strict benchmark import, independent annotation schema, stored-run checks, witness replay and paired bootstrap | Denominator and item-pair tests | Original benchmark and independent annotations absent |

The full local suite passed **67 tests with 4 explicit skips** against isolated Neo4j. The skips are two live model profiles and two PostgreSQL/Redis service checks. The earlier worker run rejected invalid fixture identifier prefixes. Correcting those test inputs produced passing 1/2/4/8-worker convergence checks. A skip is never counted as an infrastructure or model pass. The installed 0.2.0 package also replayed the completed fixture job with network access disabled.

The successful logical restore check preserved graph hash `aed16445ee1bf177d55c8d49c4570db08c104e409059122a23d68ee2a7592637`, eight fixture entities and fifteen assertions. These are synthetic software fixtures.

Extraction caching is an explicit opt-in and defaults off. Its enabled/disabled graph invariance is checked on the ROR fixture. No throughput benefit is claimed.

## Remaining scientific and operational scope

The following gaps are explicit implementation limits, not claims of successful reproduction.

- Identity resolution currently uses exact identifiers with conflict checks. Embedding/HNSW candidate search, model adjudication of difficult identities and reversible reviewed merge/split administration are not implemented.
- APIs, Parquet projections and large snapshot streaming are not implemented. Native local formats and bounded document capture are the available paths. Seed expansion records bounded inclusion decisions but does not automatically fetch a national corpus.
- Conflict reconciliation produces a nondestructive alternative view. Automated W2 longer-context re-extraction and the full W3 embedding-index maintenance policy are not implemented.
- Construction workers coordinate writes. Disjoint write-set parallelism and distributed shared-service discovery are not implemented. Local thread/worker tests do not establish PostgreSQL/Redis crash recovery across nodes.
- General analysis evaluates a bounded complete Neo4j export through trusted Python templates. It is not a scalable Cypher compiler for every operator. Exact counts retain a fixed parameterized Cypher cross-check. Working-graph changes require a new release for new questions, while old witnesses remain replayable.
- Causal-effect, documented-attribution, plausible-contribution and capability-transformation candidates remain NEI unless their evidence templates are implemented and verified. Existing paths and pre/post ratios do not establish these stronger interpretations.
- Natural-language translation and unstructured relation extraction require model-specific quality evaluation. Exact source spans establish traceability, not perfect semantic entailment. Production synthesis uses constrained claim templates.
- The H1 gRPC inference router, slack dispatch, prefix-locality routing, wave coalescing, L3 identity cache, LMCache sharing/pinning, xgrammar configuration and n-gram speculation remain unavailable. No speedup or four-replica historical performance result is claimed. These mechanisms are not silently activated by a profile name.
- The original 215 questions, original graph, historical prompts and original baseline implementations were not supplied. Automated baseline/ablation execution, independent annotation collection and the full historical evaluation remain incomplete. The importer never generates stand-in original questions or scores.

## Evidence and reproducibility

Development used Python 3.13.13, AutoGen 0.7.5, Neo4j Community 2026.02.2, driver 6.1.0 and OpenJDK 21.0.10 on macOS arm64. Direct and transitive application dependencies are pinned in `requirements-lock.txt`. GPU runtime versions are recorded by the serving launcher from the selected environment. No untested vLLM/LMCache version is presented as a tested pin.

Generated JUnit logs, logical releases, run receipts, seed manifests, scaling measurements and recovery reports belong in the ignored `artifacts/` tree. The project archive contains source and reproducibility instructions, not secrets, database stores or the user's source data.
