"""Conservative identifier resolution with explicit conflict and decision records."""

from kdiff.core.contracts import Batch, digest
from kdiff.construction.sources import normalize_doi

IDENTIFIERS = {'Author': ('orcidID',), 'Institution': ('rorID',), 'Paper': ('doi',), 'Patent': ('patentNumber',)}


def identifiers(observation):
    result = []
    for field in IDENTIFIERS.get(observation['kind'], ()):
        value = observation['attributes'].get(field)
        if value:
            value = normalize_doi(value) if field == 'doi' else str(value).strip().rstrip('/')
            result.append((observation['kind'], field, value))
    return result


def resolve_identifiers(batch: Batch, existing: dict):
    """Match exact identifiers only when the entire candidate slate is consistent."""
    rows = existing.get('observations', []) + [o.model_dump(mode='json') for o in batch.observations]
    index, per_entity = {}, {}
    for row in rows:
        for key in identifiers(row):
            index.setdefault(key, set()).add(row['entity_id'])
            per_entity.setdefault(row['entity_id'], {}).setdefault(key[1], set()).add(key[2])
    old_ids = {e['canonical_id'] for e in existing.get('entities', [])}
    replacements, decisions = {}, []
    for entity in batch.entities:
        eid = entity.canonical_id
        keys = [k for k, ids in index.items() if eid in ids]
        candidates = set().union(*(index[k] for k in keys)) if keys else {eid}
        # Exact-identifier transitive expansion must not hide a conflicting ID.
        changed = True
        while changed:
            before = len(candidates)
            for ids in index.values():
                if ids & candidates:
                    candidates |= ids
            changed = len(candidates) != before
        conflicting = any(len(set().union(*(per_entity.get(c, {}).get(field, set()) for c in candidates))) > 1
                          for field in IDENTIFIERS.get(entity.kind, ()))
        established = candidates & old_ids
        if conflicting or len(established) > 1:
            decisions.append({'entity_id': eid, 'candidates': sorted(candidates), 'status': 'conflict',
                              'reason': 'conflicting identifiers or multiple existing canonical identities'})
            continue
        target = next(iter(established)) if established else min(candidates)
        replacements[eid] = target
        decisions.append({'entity_id': eid, 'canonical_id': target, 'status': 'exact_identifier' if target != eid else 'retained',
                          'evidence': [o['observation_id'] for o in rows if o['entity_id'] in candidates],
                          'person_identity_verified': False})

    def remap(value):
        if isinstance(value, str):
            return replacements.get(value, value)
        if isinstance(value, list):
            return [remap(v) for v in value]
        if isinstance(value, dict):
            return {k: remap(v) for k, v in value.items()}
        return value

    body = batch.model_dump(mode='json')
    entities = {}
    for e in body['entities']:
        e['canonical_id'] = replacements.get(e['canonical_id'], e['canonical_id'])
        entities[e['canonical_id']] = e
    body['entities'] = list(entities.values())
    for row in body['observations']:
        before = dict(row)
        row['entity_id'] = replacements.get(row['entity_id'], row['entity_id'])
        row['attributes'] = remap(row['attributes'])
        if row != before:
            row['observation_id'] = digest({k: v for k, v in row.items() if k != 'observation_id'})
    for row in body['assertions']:
        before = dict(row)
        row['head_id'], row['tail_id'] = remap(row['head_id']), remap(row['tail_id'])
        row['qualifiers'] = remap(row['qualifiers'])
        row['logical_fact_key'] = digest([row['head_id'], row['relation'], row['tail_id'], row['qualifiers']])
        if row != before:
            row['assertion_id'] = digest({k: v for k, v in row.items() if k != 'assertion_id'})
    for row in body['identities']:
        before = dict(row)
        row['canonical_id'] = remap(row['canonical_id'])
        if row != before:
            row['mapping_id'] = digest({k: v for k, v in row.items() if k != 'mapping_id'})
    return Batch.model_validate(body), decisions


def conflict_view(assertions):
    """Derive conflict flags without modifying the preserved source assertions."""
    groups = {}
    for a in assertions:
        groups.setdefault((a['head_id'], a['relation'], a['tail_id']), []).append(a)
    return [{'assertion_ids': sorted(a['assertion_id'] for a in group),
             'conflict_flag': len({digest(a['valid_time']) for a in group}) > 1,
             'reason': 'alternative attested dates', 'resolution': 'retain_alternatives'}
            for group in groups.values() if len({digest(a['valid_time']) for a in group}) > 1]
