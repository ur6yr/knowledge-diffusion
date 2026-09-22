from kdiff.analysis.temporal import event_trace, return_moves
from kdiff.construction.sources import BatchBuilder
from kdiff.core.contracts import TimeRange, Window
from kdiff.analysis.graph_program import Executor
from kdiff.analysis.requests import AnalysisRequest, plan_request


def mobility(store):
    builder = BatchBuilder('fixture:mobility', 'fixture:mobility-records', store)
    builder.record({'example': 'synthetic mobility'}, 'fixture:mobility-record')
    author = builder.entity('Author', 'fixture:person', {'name': 'Synthetic Researcher'}, '$.name')
    a = builder.entity('Institution', 'fixture:a', {'name': 'A'}, '$.a')
    b = builder.entity('Institution', 'fixture:b', {'name': 'B'}, '$.b')
    c = builder.entity('Institution', 'fixture:c', {'name': 'Concurrent visitor'}, '$.c')
    for inst, start, end in [(a, '2010', '2012'), (b, '2013', '2015'), (a, '2016', '2019'), (c, '2014', None)]:
        lower, upper = TimeRange.parse(start), TimeRange.parse(end)
        builder.edge(author, 'affiliatedWith', inst, '$.' + start,
                     TimeRange(lower=lower.lower, upper=upper.upper, precision='interval',
                               end_kind='bounded' if end else 'unknown'), 'employment',
                     {'start_uncertainty': lower.model_dump(mode='json'), 'end_uncertainty': upper.model_dump(mode='json')})
    batch = builder.finish().model_dump(mode='json')
    return batch, author


def test_returns_preserve_date_bounds_and_concurrency(store):
    graph, author = mobility(store)
    window = Window(start='2010-01-01', end='2020-12-31', reference_date='2025-01-01')
    result = return_moves(graph, author, window)
    assert len(result['returns']) == 1
    elapsed = result['returns'][0]['elapsed']
    assert elapsed['minimum_days'] == 1827
    assert elapsed['maximum_days'] == 2556
    trace = event_trace(graph, author, window)
    state = next(x for x in trace['states'] if x['at'] == '2015-01-01')
    assert len(state['active_affiliations']) == 1
    assert len(state['uncertain_affiliations']) == 1
    assert any(e['end_kind'] == 'unknown' for e in trace['events'])
    request = AnalysisRequest(family='returns', kind='Author', identifier=author, window=window)
    executed = Executor(graph, 'release').execute(plan_request(request, 'release'))
    assert executed['result'] == result
