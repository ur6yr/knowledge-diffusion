# Source formats and coverage

All graph assertions retain a source record, source version or content hash, capture time, extraction method and record path or exact text span. Missing data is never translated into a successful zero-result source status.

| Source | Available reader | Preserved evidence and limits |
|---|---|---|
| OpenAlex | Native works JSONL or JSONL.gz | Work, author, institution and topic IDs, authorship-level institution IDs, publication dates, referenced works and JSON paths. A bibliometric affiliation is not an employment interval. |
| ORCID | Public v3 record JSON, JSON arrays or JSONL | Author URI, names, grouped employment and education summaries, organization identifiers, role and department, separate uncertain start and end dates. Missing end dates do not establish ongoing employment. |
| ROR | Native v2 JSON object or array | Display names, aliases, ROR IDs, locations, types, registry status and parent/child relationships. Native v1 objects are not silently interpreted as v2. |
| USPTO | Bounded grant/application XML, including concatenated document records | Document identifier, filing/publication/grant dates, inventor and assignee source identities, non-patent references. XML entities and external expansion are rejected. |
| PatCit | Local CSV, JSON or JSONL with an explicit inspected column map | Patent identity, normalized DOI, source row, citation direction and filing/publication/grant date basis. Unmatched NPL stays unresolved. |
| Public institutional documents | Allowlisted HTTPS capture, HTML, text and PDF transforms | Raw bytes, text transform, character offsets, exact quotations, model revision and prompt hash for AutoGen extraction. Public destinations and each redirect are validated. |
| Ioannidis discovery cohorts | CSV and bounded XLSX worksheet prefixes | File hash, inspected columns, source row and release label. Names and bibliometric indicators are discovery inputs, not authoritative identity evidence. |

Native source batches are limited to 1,000 records and 16 MiB decompressed. Generic readers accept `json`, `json-array`, `jsonl`, `csv` or `xml` where applicable. JSON arrays use an incremental parser, but the bounded batch is validated before graph integration. This is not a national snapshot loader. API pagination and Parquet projections remain unavailable.

The adapter tests use synthetic native-format records and do not establish live API access or original snapshot coverage. The only existing real-data input exercised locally in this session was a two-row discovery prefix from the local workbook labeled August 2024. That label was not independently authenticated, and it is not the August 2025 cohort described in the qualifying report. No national pipeline was run.

## PatCit mapping

Inspect the selected release's actual header first. Supply a mapping from these semantic keys to actual column names:

```json
{
  "record_id": "ACTUAL_ROW_IDENTIFIER_COLUMN",
  "patent_id": "ACTUAL_PATENT_IDENTIFIER_COLUMN",
  "doi": "ACTUAL_MATCHED_DOI_COLUMN",
  "date": "ACTUAL_CITATION_DATE_COLUMN",
  "date_basis": "ACTUAL_DATE_BASIS_COLUMN"
}
```

Values in the date-basis column must be `filing`, `publication` or `grant`. The patent identifier must match the observed USPTO country-number-kind identifier for an exact join. The relation is `Paper -> citedByPatent -> Patent`. `citesPatent` is a different relation for a paper citing a patent. No invented PatCit API or unverified release column names are built in.

## Identity and conflicts

Exact DOI, ORCID, ROR and patent identifiers can link new source observations to one existing canonical entity. Conflicting identifiers or multiple established candidate identities are retained as conflicts. Names alone never merge records. Every resolution decision is stored separately, and original raw records remain available.

Embedding-based candidate search, reviewed model adjudication and reversible administrative merge/split operations remain unfinished. `reconcile` creates a nondestructive view of alternative dates. Its flags request review and are not proof that sequential appointments contradict one another.

## Public source restrictions

Use explicit permitted institutional hosts and respect robots and access restrictions. The fetcher rejects private, loopback, link-local and multicast addresses, unapproved redirects, embedded credentials and common token-bearing query parameters. TLS connects to the validated address with the original hostname. Request count, bytes, pacing and elapsed time are bounded.

Web extraction uses exact source spans. These protect provenance and prevent invented quotations. They do not alone prove the semantic entailment of every model-selected relation. Review live extraction quality independently before using it for substantive conclusions. No document can introduce a new tool or grant permission to execute commands.

Optional demographic fields remain nullable and disabled for inference. Grant evidence is accepted only through explicit observed records and the registered relation contract. No universal grant feed or inferred award history is assumed.

## Primary references checked

Implementation was checked against [ROR v2 fields](https://ror.readme.io/v2/docs/fields), [ORCID affiliation structures](https://github.com/ORCID/ORCID-Source/blob/main/orcid-api-web/tutorial/affiliations.md), [PatCit's repository](https://github.com/cverluise/PatCit), [AutoGen client protocols](https://microsoft.github.io/autogen/stable/reference/python/autogen_core.models.html), [vLLM tool calling](https://docs.vllm.ai/en/latest/features/tool_calling/) and [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs). Live compatibility must still be established by the explicit provider checks for the selected runtime.
