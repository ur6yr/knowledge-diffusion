"""Finite event-trace interpretation, retaining date uncertainty and concurrency."""

from datetime import date
from itertools import permutations
from kdiff.core.contracts import TimeRange, digest


def endpoints(assertion):
    interval = TimeRange.model_validate(assertion['valid_time'])
    q = assertion.get('qualifiers', {})
    # Explicit endpoint uncertainty is preferred over the enclosing interval.
    start = TimeRange.model_validate(q['start_uncertainty']) if q.get('start_uncertainty') else (
        interval if interval.precision != 'interval' else
        TimeRange.parse(interval.lower.isoformat()) if interval.lower else TimeRange())
    end = TimeRange.model_validate(q['end_uncertainty']) if q.get('end_uncertainty') else (
        TimeRange.parse(interval.upper.isoformat()) if interval.upper and interval.precision == 'interval' else TimeRange())
    return start, end, interval.end_kind


def active_status(assertion, at, reference_date):
    start, end, end_kind = endpoints(assertion)
    if at > reference_date:
        return 'unknown'
    if start.lower and at < start.lower or end.upper and at > end.upper:
        return 'inactive'
    if start.upper and at >= start.upper and ((end.lower and at <= end.lower) or end_kind == 'ongoing'):
        return 'active'
    return 'unknown'


def event_trace(graph, author_id, window):
    if not any(e['canonical_id'] == author_id and e['kind'] == 'Author' for e in graph['entities']):
        raise ValueError('Researcher state requires a known author')
    edges = graph['assertions']
    affiliations = [a for a in edges if a['head_id'] == author_id and a['relation'] in {'affiliatedWith', 'visitingAt', 'emeritusAt'}
                    and a['observation_kind'] == 'employment']
    bibliometric = [a['assertion_id'] for a in edges if a['head_id'] == author_id and a['relation'] == 'affiliatedWith'
                    and a['observation_kind'] == 'publication']
    events, dates = [], {window.start, window.end}
    for a in affiliations:
        start, end, end_kind = endpoints(a)
        events.append({'assertion_id': a['assertion_id'], 'institution_id': a['tail_id'],
                       'relation': a['relation'], 'start': start.model_dump(mode='json'),
                       'end': end.model_dump(mode='json'), 'end_kind': end_kind})
        for day in (start.lower, start.upper, end.lower, end.upper):
            if day and window.start <= day <= window.end:
                dates.add(day)
    authorships = [a for a in edges if a['head_id'] == author_id and a['relation'] == 'authorOf']
    for a in authorships:
        t = TimeRange.model_validate(a['valid_time'])
        for day in (t.lower, t.upper):
            if day and window.start <= day <= window.end:
                dates.add(day)
    states = []
    inputs = {a['assertion_id'] for a in affiliations + authorships}
    for at in sorted(dates):
        certain, uncertain = [], []
        for a in affiliations:
            status = active_status(a, at, window.reference_date)
            if status == 'active':
                certain.append(a['tail_id'])
            elif status == 'unknown':
                uncertain.append(a['tail_id'])
        papers, unknown_papers = set(), set()
        by_paper = {}
        for a in authorships:
            t = TimeRange.model_validate(a['valid_time'])
            status = 'recorded' if t.upper and t.upper <= at else ('future' if t.lower and t.lower > at else 'unknown')
            by_paper.setdefault(a['tail_id'], set()).add(status)
        for paper, statuses in by_paper.items():
            if statuses == {'recorded'}:
                papers.add(paper)
            elif statuses != {'future'}:
                unknown_papers.add(paper)
        collaborators, topics, grants, patents = set(), set(), set(), set()
        for a in edges:
            if a['relation'] == 'authorOf' and a['tail_id'] in papers and a['head_id'] != author_id:
                collaborators.add(a['head_id'])
                inputs.add(a['assertion_id'])
            if a['head_id'] in papers:
                target = {'classifiedAs': topics, 'fundedBy': grants, 'citedByPatent': patents}.get(a['relation'])
                if target is not None:
                    # Patent events must already have occurred. Topic/funding
                    # annotations describe the included publication itself.
                    t = TimeRange.model_validate(a['valid_time'])
                    if a['relation'] != 'citedByPatent' or t.upper and t.upper <= at:
                        target.add(a['tail_id'])
                    inputs.add(a['assertion_id'])
        states.append({'at': at.isoformat(), 'active_affiliations': sorted(set(certain)),
                       'uncertain_affiliations': sorted(set(uncertain)), 'papers': sorted(papers),
                       'uncertain_papers': sorted(unknown_papers), 'collaborators': sorted(collaborators),
                       'topics': sorted(topics), 'grants': sorted(grants), 'patents': sorted(patents)})
    return {'type': 'event_trace', 'author_id': author_id, 'events': sorted(events, key=lambda x: x['assertion_id']),
            'states': states, 'input_ids': sorted(inputs), 'complete': True, 'bibliometric_affiliation_ids': sorted(bibliometric),
            'window': window.model_dump(mode='json'), 'interpretation': 'finite_observed_event_trace',
            'coverage': 'Unobserved records are not inferred. Simultaneous affiliations remain simultaneous.'}


def return_moves(graph, author_id, window):
    trace = event_trace(graph, author_id, window)
    events = trace['events']
    if len(events) > 100:
        raise ValueError('Mobility event budget exceeded')
    certain, uncertain = [], []
    for first, middle, last in permutations(events, 3):
        if first['institution_id'] != last['institution_id'] or middle['institution_id'] == first['institution_id']:
            continue
        starts = [TimeRange.model_validate(e['start']) for e in (first, middle, last)]
        ends = [TimeRange.model_validate(e['end']) for e in (first, middle)]
        if any(t.lower and t.lower > window.end or t.upper and t.upper < window.start for t in starts):
            continue
        if not all(t.lower and t.upper for t in starts):
            continue  # Unknown event order cannot identify even a bounded return.
        if starts[0].lower > starts[1].upper or starts[1].lower > starts[2].upper:
            continue
        if starts[0].lower == starts[2].lower and starts[0].upper == starts[2].upper:
            continue  # Multiple attestations of one appointment are not a return.
        ordered = starts[0].upper < starts[1].lower and starts[1].upper < starts[2].lower
        departed = all(end.upper and end.upper < start.lower for end, start in zip(ends, starts[1:]))
        in_scope = starts[0].lower >= window.start and starts[2].upper <= window.end
        elapsed = {'minimum_days': max(0, (starts[2].lower - starts[0].upper).days),
                   'maximum_days': (starts[2].upper - starts[0].lower).days}
        value = {'institutions': [e['institution_id'] for e in (first, middle, last)],
                 'assertions': [e['assertion_id'] for e in (first, middle, last)], 'elapsed': elapsed}
        (certain if ordered and departed and in_scope else uncertain).append(value)
    return {'type': 'mobility_returns', 'author_id': author_id, 'returns': certain,
            'ambiguous_candidates': uncertain, 'input_ids': trace['input_ids'], 'complete': True,
            'window': window.model_dump(mode='json'),
            'interpretation': 'Observed A-to-B-to-A with documented departures, not inferred exclusive employment'}
