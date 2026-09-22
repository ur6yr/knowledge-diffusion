"""Bounded native OpenAlex JSONL/JSONL.gz parser; no network access."""

import gzip
import hashlib
import json
import re
from pathlib import Path

from kdiff.core.artifacts import ArtifactStore
from kdiff.core.contracts import (
    Assertion, Batch, Entity, IdentityMapping, Observation, Provenance,
    TimeRange, digest, stable_id,
)

MAX_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 1000


def capture_local(path: Path, store: ArtifactStore, namespace: str) -> dict:
    if not re.fullmatch(r"(?:fixture|local):[a-zA-Z0-9_-]+", namespace):
        raise ValueError("Namespace must start fixture: or local:")
    if not path.is_file() or path.is_symlink():
        raise ValueError("Source is unavailable or is a symlink")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Input exceeds 16 MiB development cap")
    opener = gzip.open if path.suffix == ".gz" else open
    records = []
    total = 0
    with opener(path, "rb") as f:
        while line := f.readline(MAX_BYTES + 1):
            total += len(line)
            if total > MAX_BYTES or len(records) >= MAX_RECORDS:
                raise ValueError("Input limit exceeded; no truncated successful import")
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict) or not record.get("id"):
                raise ValueError("OpenAlex work needs an explicit source ID")
            # Independent per-record hashes preserve reuse when another record changes.
            captured = store.capture(line)
            records.append({**captured, "source_record_id": record["id"]})
    if not records:
        raise ValueError("Empty source: no successful import")
    return {"namespace": namespace, "format": "openalex-native-jsonl-v1",
            "status": "complete_for_declared_scope", "records": records,
            "scope": "Only the supplied local file; no population coverage implied",
            "synthetic": namespace.startswith("fixture:")}


def parse_capture(capture: dict, store: ArtifactStore) -> Batch:
    namespace = capture["namespace"]
    entities, observations, assertions, identities, sources = {}, {}, {}, {}, {}

    def check_id(value, prefix):
        if not isinstance(value, str):
            raise ValueError("Missing endpoint source ID")
        pattern = rf"fixture:openalex:{prefix}[A-Za-z0-9_-]+" if capture["synthetic"] else rf"https://openalex.org/{prefix}\d+"
        if not re.fullmatch(pattern, value):
            raise ValueError("Source ID/fixture namespace mismatch")

    for record_meta in capture["records"]:
        raw = store.get_bytes(record_meta["sha256"])
        record = json.loads(raw)
        record_hash = hashlib.sha256(raw).hexdigest()
        work_source_id = record["id"]
        check_id(work_source_id, "W")
        sources[record_hash] = {**record_meta, "source": "openalex", "synthetic": capture["synthetic"]}

        def provenance(path):
            return Provenance(source="openalex", source_record_id=work_source_id,
                              source_version_or_hash=record_hash, raw_artifact_pointer=record_hash,
                              source_path=path, retrieved_at=record_meta["retrieved_at"])

        def entity(kind, source_id, attrs, path):
            eid = stable_id(namespace, kind, source_id)
            e = Entity(canonical_id=eid, namespace=namespace, kind=kind)
            entities[eid] = e
            attrs = {k: v for k, v in attrs.items() if v is not None}
            attrs[f"{kind.lower()}ID"] = eid
            prov = provenance(path)
            oid = digest([eid, attrs, prov.model_dump(mode="json")])
            observations[oid] = Observation(observation_id=oid, entity_id=eid, kind=kind,
                                           attributes=attrs, provenance=prov)
            # Mapping evidence is versioned per source record, without changing canonical IDs.
            mid = digest([eid, source_id, record_hash])
            identities[mid] = IdentityMapping(mapping_id=mid, source="openalex", source_id=source_id,
                                               canonical_id=eid, evidence_hash=record_hash)
            return eid

        raw_date = record.get("publication_date") or record.get("publication_year")
        time = TimeRange.parse(raw_date)
        if record.get("publication_date") and record.get("publication_year"):
            if time.lower and time.lower.year != record["publication_year"]:
                raise ValueError("Inconsistent publication date/year within source record")
        paper = entity("Paper", work_source_id, {"title": record.get("display_name"),
                       "publicationYear": record.get("publication_year"), "doi": record.get("doi")}, "$")

        def assertion(head, relation, tail, path, qualifiers=None):
            qualifiers = qualifiers or {}
            logical = digest([head, relation, tail, qualifiers])
            aid = digest([logical, time.model_dump(mode="json"), record_hash, path])
            assertions[aid] = Assertion(assertion_id=aid, logical_fact_key=logical,
                                       head_id=head, relation=relation, tail_id=tail, valid_time=time,
                                       observation_kind="publication", provenance=provenance(path),
                                       qualifiers=qualifiers)

        authorships = record.get("authorships")
        if not isinstance(authorships, list):
            raise ValueError("Missing authorships; cannot assert complete source parsing")
        for i, authorship in enumerate(authorships):
            author = authorship.get("author") or {}
            check_id(author.get("id"), "A")
            apath = f"$.authorships[{i}]"
            aid = entity("Author", author["id"], {"name": author.get("display_name"),
                         "orcidID": author.get("orcid")}, apath + ".author")
            institutions = []
            for j, inst in enumerate(authorship.get("institutions") or []):
                check_id(inst.get("id"), "I")
                iid = entity("Institution", inst["id"], {"name": inst.get("display_name"),
                             "rorID": inst.get("ror")}, f"{apath}.institutions[{j}]")
                institutions.append(iid)
                assertion(aid, "affiliatedWith", iid, f"{apath}.institutions[{j}]",
                          {"context_paper_id": paper, "kind": "bibliometric_observation"})
            assertion(aid, "authorOf", paper, apath,
                      {"position": authorship.get("author_position"),
                       "institution_ids": sorted(institutions), "date_basis": "publication"})
        for i, topic in enumerate(record.get("topics") or []):
            check_id(topic.get("id"), "T")
            tid = entity("Topic", topic["id"], {"name": topic.get("display_name"),
                         "openAlexID": topic["id"]}, f"$.topics[{i}]")
            assertion(paper, "classifiedAs", tid, f"$.topics[{i}]", {"taxonomy": "openalex-topics"})
        for i, reference in enumerate(record.get('referenced_works') or []):
            check_id(reference, 'W')
            target = entity('Paper', reference, {}, f'$.referenced_works[{i}]')
            assertion(paper, 'citesPaper', target, f'$.referenced_works[{i}]', {'date_basis': 'citing_publication'})

    return Batch(namespace=namespace, entities=list(entities.values()), observations=list(observations.values()),
                 assertions=list(assertions.values()), identities=list(identities.values()), sources=list(sources.values()))


def validate_batch(batch: Batch) -> Batch:
    from kdiff.core.schema import validate_relation
    entities = {e.canonical_id: e for e in batch.entities}
    if len(entities) != len(batch.entities):
        raise ValueError("Duplicate canonical entity")
    if any(e.namespace != batch.namespace for e in batch.entities):
        raise ValueError('Entity namespace differs from its batch')
    sources = {row['sha256'] for row in batch.sources}
    if any(not re.fullmatch('[0-9a-f]{64}', sha) for sha in sources):
        raise ValueError('Invalid source artifact hash')
    for row in [*batch.observations, *batch.assertions]:
        if row.provenance.raw_artifact_pointer not in sources:
            raise ValueError('Graph record has no captured source operand')
    if any(mapping.evidence_hash not in sources for mapping in batch.identities):
        raise ValueError('Identity mapping has no source evidence')
    for a in batch.assertions:
        if a.head_id not in entities or a.tail_id not in entities:
            raise ValueError("Missing relationship endpoint")
        validate_relation(a.relation, entities[a.head_id], entities[a.tail_id], a.qualifiers)
    for obs in batch.observations:
        if obs.entity_id not in entities or entities[obs.entity_id].kind != obs.kind:
            raise ValueError("Attribute observation has no matching entity")
    for mapping in batch.identities:
        if mapping.canonical_id not in entities:
            raise ValueError("Identity mapping has no endpoint")
    return batch
