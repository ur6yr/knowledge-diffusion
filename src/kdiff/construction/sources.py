"""Bounded source readers with record-local provenance and explicit format versions."""

import csv
import gzip
import io
import json
import re
from pathlib import Path

import ijson
from defusedxml import ElementTree as SafeET

from kdiff.core.contracts import (Assertion, Batch, Entity, IdentityMapping, Observation,
                                  Provenance, TimeRange, canonical, digest, stable_id)
from kdiff.construction.openalex import MAX_BYTES, MAX_RECORDS, validate_batch

SOURCES = ('openalex', 'orcid', 'ror', 'uspto', 'patcit', 'web')


class BatchBuilder:
    def __init__(self, namespace, source, store):
        if not re.fullmatch(r'(?:fixture|local):[A-Za-z0-9_-]+', namespace):
            raise ValueError('Use an explicit fixture: or local: namespace')
        self.namespace, self.source, self.store = namespace, source, store
        self.entities, self.observations, self.assertions, self.identities, self.sources = {}, {}, {}, {}, {}
        self.unresolved = []

    def record(self, record, source_id, *, raw=None):
        self.meta = self.store.capture(raw if raw is not None else canonical(record))
        self.record_id = str(source_id)
        self.sources[self.meta['sha256']] = {**self.meta, 'source': self.source,
            'source_record_id': self.record_id, 'synthetic': self.namespace.startswith('fixture:')}

    def provenance(self, path):
        return Provenance(source=self.source, source_record_id=self.record_id,
            source_version_or_hash=self.meta['sha256'], raw_artifact_pointer=self.meta['sha256'],
            retrieved_at=self.meta['retrieved_at'], source_path=path, extraction_method=f'{self.source}-reader-v1')

    def entity(self, kind, identifier, attrs, path, *, subtype=None):
        if not identifier:
            raise ValueError('Missing source identity')
        eid = stable_id(self.namespace, kind, str(identifier))
        self.entities[eid] = Entity(canonical_id=eid, namespace=self.namespace, kind=kind, subtype=subtype)
        attrs = {k: v for k, v in attrs.items() if v is not None}
        attrs[kind.lower() + 'ID'] = eid
        prov = self.provenance(path)
        oid = digest([eid, attrs, prov])
        self.observations[oid] = Observation(observation_id=oid, entity_id=eid, kind=kind,
                                            attributes=attrs, provenance=prov)
        mid = digest([eid, str(identifier), self.meta['sha256']])
        self.identities[mid] = IdentityMapping(mapping_id=mid, source=self.source, source_id=str(identifier),
                                               canonical_id=eid, evidence_hash=self.meta['sha256'])
        return eid

    def edge(self, head, relation, tail, path, time=None, kind='other', qualifiers=None):
        qualifiers = qualifiers or {}
        time = time or TimeRange()
        logical = digest([head, relation, tail, qualifiers])
        aid = digest([logical, time, self.meta['sha256'], path])
        self.assertions[aid] = Assertion(assertion_id=aid, logical_fact_key=logical, head_id=head,
            relation=relation, tail_id=tail, valid_time=time, observation_kind=kind,
            provenance=self.provenance(path), qualifiers=qualifiers)
        return aid

    def finish(self):
        return validate_batch(Batch(namespace=self.namespace, **{
            key: list(getattr(self, key).values())
            for key in ('entities', 'observations', 'assertions', 'identities', 'sources')}))


def date_parts(value):
    if not value:
        return TimeRange()
    parts = []
    for field, width in [('year', 4), ('month', 2), ('day', 2)]:
        v = (value.get(field) or {}).get('value')
        if v is None:
            break
        parts.append(str(v).zfill(width))
    return TimeRange.parse('-'.join(parts) or None)


def affiliation_time(start, end):
    # Missing ORCID end dates do not establish an ongoing appointment.
    return TimeRange(lower=start.lower, upper=end.upper, precision='interval',
                     end_kind='bounded' if start.lower and end.upper else 'unknown',
                     raw=json.dumps({'start': start.raw, 'end': end.raw}))


def parse_orcid(record, b):
    identifier = (record.get('orcid-identifier') or {}).get('uri')
    if not identifier or not re.fullmatch(r'https://orcid.org/\d{4}-\d{4}-\d{4}-\d{3}[\dX]', identifier):
        raise ValueError('ORCID v3 record requires an ORCID URI')
    b.record(record, identifier)
    name = ((record.get('person') or {}).get('name') or {})
    label = ' '.join((name.get(k) or {}).get('value', '') for k in ('given-names', 'family-name')).strip()
    author = b.entity('Author', identifier, {'name': label or None, 'orcidID': identifier}, '$.person.name')
    activities = record.get('activities-summary') or {}
    for section, singular, relation in [('employments', 'employment', 'affiliatedWith'),
                                         ('educations', 'education', 'obtainedDegreeAt')]:
        groups = (activities.get(section) or {}).get('affiliation-group', [])
        # API 3.0 groups contain summaries. A missing section is not zero coverage.
        for gi, group in enumerate(groups):
            for si, summary in enumerate(group.get('summaries', [])):
                item = summary.get(singular + '-summary')
                if item is None:
                    continue
                path = f'$.activities-summary.{section}.affiliation-group[{gi}].summaries[{si}].{singular}-summary'
                org = item.get('organization') or {}
                if not org.get('name'):
                    raise ValueError('Affiliation has no organization name')
                dis = org.get('disambiguated-organization') or {}
                oid = dis.get('disambiguated-organization-identifier')
                ror = oid if dis.get('disambiguation-source', '').upper() == 'ROR' else None
                if ror and not ror.startswith('https://ror.org/'):
                    ror = 'https://ror.org/' + ror
                org_id = ror or f'{identifier}:organization:{digest(org)}'
                inst = b.entity('Institution', org_id, {'name': org['name'], 'rorID': ror,
                    'location': org.get('address')}, path + '.organization')
                start, end = date_parts(item.get('start-date')), date_parts(item.get('end-date'))
                b.edge(author, relation, inst, path, affiliation_time(start, end),
                       'employment' if singular == 'employment' else 'other',
                       {'role_title': item.get('role-title'), 'department': item.get('department-name'),
                        'start_uncertainty': start.model_dump(mode='json'),
                        'end_uncertainty': end.model_dump(mode='json'), 'date_basis': singular})


def parse_ror(record, b):
    identifier = record.get('id')
    if not identifier or not identifier.startswith('https://ror.org/') or not isinstance(record.get('names'), list):
        raise ValueError('Expected a native ROR v2 record')
    b.record(record, identifier)
    names = record['names']
    preferred = [x['value'] for x in names if 'ror_display' in x.get('types', [])]
    if len(preferred) != 1:
        raise ValueError('ROR record must contain one display name')
    org = b.entity('Institution', identifier, {'rorID': identifier, 'name': preferred[0],
        'aliases': [x['value'] for x in names if x['value'] != preferred[0]],
        'type': record.get('types'), 'location': record.get('locations'),
        'foundedYear': record.get('established'), 'annotation': {'registry_status': record.get('status')}}, '$')
    for i, rel in enumerate(record.get('relationships', [])):
        if str(rel.get('type', '')).lower() not in {'parent', 'child'}:
            continue  # Other ROR relationships are retained in the raw record.
        other = b.entity('Institution', rel['id'], {'rorID': rel['id'], 'name': rel.get('label')}, f'$.relationships[{i}]')
        head, tail = (org, other) if rel['type'].lower() == 'parent' else (other, org)
        b.edge(head, 'subunitOf', tail, f'$.relationships[{i}]')


def xml_text(node, path):
    found = node.find(path)
    return ''.join(found.itertext()).strip() if found is not None else None


def patent_identifier(doc):
    if doc is None:
        raise ValueError('Patent document identifier missing')
    country, number, kind = (xml_text(doc, name) for name in ('country', 'doc-number', 'kind'))
    if not country or not number:
        raise ValueError('Patent country/number missing')
    return f'{country}-{number}-{kind or "unknown-kind"}'


def xml_date(raw):
    if raw and re.fullmatch(r'\d{8}', raw):
        raw = f'{raw[:4]}-{raw[4:6]}-{raw[6:]}'
    return TimeRange.parse(raw)


def parse_uspto(root, raw, b):
    if root.tag not in {'us-patent-grant', 'us-patent-application'}:
        raise ValueError('Unsupported USPTO XML document root')
    bib = root.find('us-bibliographic-data-grant')
    if bib is None:
        bib = root.find('us-bibliographic-data-application')
    if bib is None:
        raise ValueError('USPTO bibliography missing')
    pub = bib.find('publication-reference/document-id')
    ident = patent_identifier(pub)
    pub_time = xml_date(xml_text(pub, 'date'))
    filing = xml_date(xml_text(bib, 'application-reference/document-id/date'))
    b.record({}, ident, raw=raw)
    patent = b.entity('Patent', ident, {'patentNumber': ident, 'title': xml_text(bib, 'invention-title'),
        'filingDate': filing.raw, 'publicationDate': pub_time.raw,
        'grantDate': pub_time.raw if root.tag == 'us-patent-grant' else None,
        'patentOffice': xml_text(pub, 'country'),
        'nonPatentCitations': [xml_text(n, 'othercit') for n in bib.findall('.//nplcit')]}, '/'+root.tag)
    for i, assignee in enumerate(bib.findall('.//assignee')):
        name = xml_text(assignee, 'addressbook/orgname')
        if name:
            inst = b.entity('Institution', f'{ident}:assignee:{i}', {'name': name}, f'//assignee[{i}]')
            b.edge(patent, 'assignedTo', inst, f'//assignee[{i}]', pub_time, qualifiers={'date_basis': 'publication'})
    # A patent-local inventor identity is not merged with a named author.
    for i, inventor in enumerate(bib.findall('.//inventor')):
        name = ' '.join(filter(None, [xml_text(inventor, 'addressbook/first-name'), xml_text(inventor, 'addressbook/last-name')]))
        if name:
            author = b.entity('Author', f'{ident}:inventor:{i}', {'name': name}, f'//inventor[{i}]')
            b.edge(patent, 'inventedBy', author, f'//inventor[{i}]', pub_time, qualifiers={'date_basis': 'publication'})
    for i, npl in enumerate(bib.findall('.//nplcit')):
        b.unresolved.append({'patent_id': patent, 'source_record_id': ident,
            'raw_artifact_pointer': b.meta['sha256'], 'source_path': f'//nplcit[{i}]',
            'citation': xml_text(npl, 'othercit'), 'status': 'requires_identifier_match'})


def normalize_doi(value):
    value = str(value or '').strip().lower()
    value = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', value)
    if not re.fullmatch(r'10\.\d{4,9}/\S+', value):
        raise ValueError('Invalid DOI')
    return value


def parse_patcit(record, b, columns):
    required = {'record_id', 'patent_id', 'doi', 'date', 'date_basis'}
    if set(columns) != required:
        raise ValueError('PatCit needs an explicit versioned column map: ' + ', '.join(sorted(required)))
    if any(v not in record for v in columns.values()):
        raise ValueError('PatCit release does not match configured columns')
    r = {key: record[col] for key, col in columns.items()}
    b.record(record, r['record_id'])
    if not r['patent_id']:
        raise ValueError('PatCit patent endpoint missing')
    if not r['doi']:
        b.unresolved.append({'source_record_id': r['record_id'], 'status': 'no_resolved_doi',
                             'raw_artifact_pointer': b.meta['sha256']})
        return
    doi = normalize_doi(r['doi'])
    if r['date_basis'] not in {'filing', 'publication', 'grant'}:
        raise ValueError('PatCit date basis must be explicit')
    paper = b.entity('Paper', 'doi:' + doi, {'doi': 'https://doi.org/' + doi}, '$.' + columns['doi'])
    patent = b.entity('Patent', r['patent_id'], {'patentNumber': r['patent_id']}, '$.' + columns['patent_id'])
    b.edge(paper, 'citedByPatent', patent, '$', TimeRange.parse(r['date'] or None),
           qualifiers={'date_basis': r['date_basis'], 'match_method': 'supplied_patcit_doi'})


class LimitedReader:
    def __init__(self, stream, cap):
        self.stream, self.cap, self.total = stream, cap, 0

    def read(self, size=-1):
        data = self.stream.read(min(self.cap - self.total + 1, size) if size >= 0 else self.cap - self.total + 1)
        self.total += len(data)
        if self.total > self.cap:
            raise ValueError('Decompressed input byte budget exceeded')
        return data


def read_records(path, format, max_bytes=MAX_BYTES):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise ValueError('Source unavailable or symlinked')
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rb') as stream:
        limited = LimitedReader(stream, max_bytes)
        if format == 'json-array':
            yield from ijson.items(limited, 'item', use_float=True)
        elif format == 'json':
            value = json.loads(limited.read())
            if not isinstance(value, dict):
                raise ValueError('Expected a JSON object')
            yield value
        elif format == 'jsonl':
            # Bounded entire batches, with independent raw record capture.
            for line in limited.read().splitlines():
                if line.strip():
                    yield json.loads(line)
        elif format == 'csv':
            yield from csv.DictReader(io.StringIO(limited.read().decode('utf-8-sig')))
        elif format == 'xml':
            raw = limited.read()
            # USPTO bulk concatenations are split at document declarations. Never
            # expand entities or load an external DTD, including a network DTD.
            chunks = re.split(br'(?=<\?xml\s)', raw)
            for chunk in chunks:
                if not chunk.strip():
                    continue
                root = SafeET.fromstring(chunk, forbid_dtd=False, forbid_entities=True, forbid_external=True)
                yield (root, chunk)
        else:
            raise ValueError('Unsupported input format')


def parse_source(path, store, namespace, source, *, format='jsonl', columns=None,
                 max_records=MAX_RECORDS, max_bytes=MAX_BYTES):
    if source not in {'orcid', 'ror', 'uspto', 'patcit'}:
        raise ValueError('Use the dedicated OpenAlex or web capture workflow')
    b = BatchBuilder(namespace, source, store)
    count = 0
    for record in read_records(path, format, max_bytes):
        count += 1
        if count > max_records:
            raise ValueError('Record budget exceeded, incomplete batch cannot be integrated')
        if source == 'uspto':
            if format != 'xml':
                raise ValueError('USPTO requires XML')
            parse_uspto(*record, b)
        elif source == 'patcit':
            parse_patcit(record, b, columns or {})
        else:
            {'orcid': parse_orcid, 'ror': parse_ror}[source](record, b)
    if not count:
        raise ValueError('Empty source, coverage unknown')
    batch = b.finish()
    return batch, {'source': source, 'records': count, 'format': format,
        'status': 'complete_for_declared_scope', 'scope': 'Only supplied records',
        'unresolved': b.unresolved, 'batch_id': store.put(batch.model_dump(mode='json'))}
