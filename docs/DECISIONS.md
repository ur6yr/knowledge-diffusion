# Reproduction decisions

These are reconstruction choices, not recovered historical implementation details.

| ID | Decision and basis |
|---|---|
| D01 | P3 five construction families and P2 §4/Figure 3 seven analysis agents are authoritative; P2's abstract says six while naming seven. Each role is an actual AutoGen AssistantAgent. Mock responses are explicitly scripted testing only. |
| D02 | P3 Table 11 defines canonical analysis signatures. P1 S2 supplies additional relations. `citedByPatent` and `citesPatent` stay distinct. The grant arrows in the schema illustration do not override textual signatures; grants require sponsor/administrator qualifiers. |
| D03 | All P1 S2 field names are retained in the registry as optional unless they identify the entity. Demographic inference is disabled. Sites are Institution subtypes; programs and achievements have no invented representation. |
| D04 | Stable UUID5 application IDs initially mean source-entity identity in a namespace, not resolved real-person identity. No name merge or cross-source person merge is authorized in M1. Mapping version is frozen in releases. |
| D05 | Preserve versioned entity attribute observations and relation assertions. No destructive preference for an authoritative source. Contradictory dates remain alternatives; exact counts abstain when membership is uncertain. |
| D06 | M1 uses file artifacts and a single-host advisory lock. PostgreSQL JSONB, write-intent recovery, Redis leasing/fencing, W2/W3, and multiple workers remain M2/M4 gates; file locks are not a cluster lease. |
| D07 | M1 snapshot is a complete bounded logical export under the same exclusive lock as writes, including entities, attribute observations, assertions, identities, and raw artifacts. Analysis queries Neo4j only while its content hash matches this frozen export and keeps the lock for the turn. Changed working graphs require a new release for new questions; old witnesses replay from the frozen export. This is not a live filesystem backup or scalable online versioning. |
| D08 | Driver transactions with uniqueness constraints replace APOC parallel import for the correctness-first slice. No disjoint-endpoint assumption, APOC dependency, or performance reproduction claim. |
| D09 | M1 supports an explicit-ID author publication count over an inclusive normalized date window. It uses recorded publication-time authorship, distinct canonical papers, all retained date assertions, complete inputs, and strict uncertainty checks. Other question families/operators are unavailable until M3. |
| D10 | M1 Synthesizer selects an already verified claim/witness via a typed tool and uses a deterministic sentence template. This restrictive reconstruction prevents added facts but is not the full natural-language post-write verifier or greedy witness optimization of M3. All operands are retained. |
| D11 | Missing P2 appendices, original benchmark, source snapshots, prompts, and exact grammar remain missing. Paper counts and printed placeholder DOIs are never source records. New prompts and fixtures are labeled reconstructions. |
| D12 | Explicit-URL institutional/CV/dissertation sources follow P1 S5, not the broader Wikipedia example in Table 1. Web fetching is not enabled in M1. Future fetches need allowlisted public destinations and redirect checks. |
| D13 | Temporal association cannot establish causation. Announcement-only records do not prove operation or real-world nonoperation. Partial rewrites must retain candidate lineage; full handling remains M3. |
| D14 | Historical backbones remain Llama 3.1 8B (construction) and Llama 4 Scout (analysis; 109B total/17B active). This local mock run reproduces mechanics only. No current live-provider or cluster compatibility is inferred from configuration serialization. |
| D15 | Python 3.13 and existing Neo4j 2026.02.2/OpenJDK 21 are local substitutions. Dependencies are pinned from actual installation, with transitive lock and test evidence. No environment in sibling repositories is changed. |
