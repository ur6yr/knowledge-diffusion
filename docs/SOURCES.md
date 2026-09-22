# Source inventory and adapter plan

No original research graph source manifest is supplied. `papers/SOURCE_MANIFEST.json` describes only the PDFs. The national OpenAlex pipeline was not run or changed.

| Source | Verified local availability | M1 implementation | Next bounded verification |
|---|---|---|---|
| OpenAlex | New explicitly synthetic native-shaped JSONL fixture. Sibling `kdproj/data/demo_verify` is also a synthetic demo; `data/raw` had no captured records. No national snapshot path verified. | JSONL/JSONL.gz local bounded capture, authorships, publication precision, author/institution/topic observations and exact source mappings. API, references, Parquet and expansion pending. | Inspect a supplied native snapshot sample or Parquet schema without rescan; preserve required authorship/raw columns and version metadata. |
| ORCID | No real local records or configured public API credentials located. Sibling demo record is synthetic. | Unimplemented, `not_configured`; no empty-success fallback. | Versioned public XML/JSON fixture, sparse employment/education records, current public-client access check. |
| ROR | No verified real registry dump located; sibling demo fixture only. | Unimplemented, `not_configured`. | Reuse a verified release with checksum, inspect versioned names/locations/relationships. |
| USPTO | No verified local bulk XML located. | Unimplemented, `not_configured`. | Inspect packaging/version and a bounded actual NPL/inventor/assignee XML sample; retain filing/publication/grant date basis. |
| PatCit | No verified release/index located. | Unimplemented, `not_configured`. | Check actual release columns and mappings to DOI/OpenAlex. P1 states v0.15; do not invent a lookup API. |
| Targeted public web/CV/dissertation | No captured real source located within audited directories. | Unimplemented, network retrieval disabled. | Explicit allowlisted institutional sources; validate public DNS/IP destinations and every redirect, access restrictions, MIME and byte bounds, exact source spans. No fetched instructions executed. |
| Ioannidis seeds | `../Knowledge Diffusion/Ionnidis Data/` contains an August 2024 archive and extracted directory. Archive size 165,625,262 bytes. Listed only; contents/checksum not yet validated. | Import pending M2. Not the August 2025 cohort cited by P3. | Inspect CSV header and release metadata, use as discovery cohort rather than identity truth. No new archive download. |
| Grant evidence | No universal paper-specified API; no local feed verified. | Nullable schema only. | Acknowledgment IDs and explicitly configured public funder/institution evidence with sponsor/administrator roles. |

The M1 file cap is 16 MiB decompressed/1,000 records. An exceeded cap fails the batch; it is never silently reported as complete. Every raw line is content-addressed and has a captured timestamp; duplicate versions retain all assertions without multiplying distinct-paper counts. Source status `complete_for_declared_scope` means the supplied file only, never source-wide coverage.

Scientific field names/signatures are in `src/kdiff/core/schema.py`; the frozen-release contract is in `src/kdiff/analysis/witness.py`. The three relation families parsed in M1 (`authorOf`, `affiliatedWith`, `classifiedAs`), identities, and attribute observations are grounded in captured record paths. Citation/reference parsing is not implemented yet.

Primary technical documentation inspected for implementation: [AutoGen model clients](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/models.html), [AssistantAgent API](https://microsoft.github.io/autogen/stable/reference/python/autogen_agentchat.agents.html), [Neo4j file locations](https://neo4j.com/docs/operations-manual/current/configuration/file-locations/). Installed-source inspection and actual tests, rather than documentation alone, establish the M1 compatibility claims. Live OpenAI/vLLM use remains untested.
