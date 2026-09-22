import copy
import socket

import pytest

from kdiff.analysis.fixtures import institute_fixture
from kdiff.analysis.graph_program import GraphProgram, Executor, Clarify, Incomplete
from kdiff.analysis.requests import AnalysisRequest, plan_request
from kdiff.analysis.claims import candidate, verify, synthesize, AtomicClaim, greedy_cover
from kdiff.analysis.general_witness import make_program_witness, replay_program
from kdiff.core.contracts import Window, digest, Label


@pytest.fixture
def institute(store):
    batch,ids=institute_fixture(store)
    body=batch.model_dump(mode='json')
    body.pop('schema_version')
    body.pop('namespace')
    body['batches']=[{'batch_id':digest(body),'namespace':batch.namespace}]
    release={'kind':'kdiff-logical-release-v1','namespace':batch.namespace,'graph':body,
             'graph_hash':digest(body),'replay_version':'author-count-replay-v1','synthetic':True}
    release_id=store.put(release)
    return body,ids,release_id


def comparison(ids):
    return AnalysisRequest(family='comparison',kind='Institution',identifier=ids['institution'],topic_id=ids['topic'],
        before=Window(start='2016-01-01',end='2018-12-31',reference_date='2025-01-01'),
        window=Window(start='2020-01-01',end='2022-12-31',reference_date='2025-01-01'),ask_causation=True)


def test_institute_counts_ratio_mutation_and_replay(store,institute,monkeypatch):
    graph,ids,release_id=institute
    program=plan_request(comparison(ids),release_id)
    result=Executor(graph,release_id).execute(program)
    assert result['result']['before']['value']==14
    assert result['result']['after']['value']==31
    assert (result['result']['numerator'],result['result']['denominator'])==(31,14)
    claim=candidate(program.output,result['result'])
    wid=make_program_witness(store,release_id,program,result,claim)
    monkeypatch.setattr(socket,'socket',lambda *a,**k:(_ for _ in ()).throw(AssertionError('network replay')))
    assert '2.21' in replay_program(store,wid)['text']
    mutated=copy.deepcopy(graph)
    target=result['result']['after']['ids'][0]
    mutated['assertions']=[a for a in mutated['assertions'] if not (a['tail_id']==target and a['relation']=='authorOf')]
    changed=Executor(mutated,release_id).execute(program)
    assert changed['result']['after']['value']==30
    assert replay_program(store,wid)['result']['after']['value']==31
    witness=store.get(wid)
    witness['complete_execution']['result']['numerator']=999
    with pytest.raises(ValueError,match='does not replay'):
        replay_program(store,store.put(witness))


def test_identity_ambiguity_and_evidence_context(institute):
    from kdiff.analysis.graph_program import Resolve
    graph,ids,release_id=institute
    executor=Executor(graph,release_id)
    with pytest.raises(Clarify) as exc:
        executor.resolve(Resolve(kind='Author',name='A. Chen'))
    assert set(exc.value.candidates)=={ids['senior'],ids['junior']}
    result=executor.resolve(Resolve(kind='Author',name='A. Chen',context={'institution_id':ids['institution'],'role':'senior researcher'}))
    assert result['ids']==[ids['senior']]


def test_sites_keep_announcement_separate(institute):
    graph,ids,release_id=institute
    request=AnalysisRequest(family='sites',kind='Institution',identifier=ids['institution'],
                            window=Window.last_decade(__import__('datetime').date(2025,1,1)))
    result=Executor(graph,release_id).execute(plan_request(request,release_id))
    assert result['result']['value']==3
    assert len(result['steps']['announcements']['ids'])==1


def test_six_labels_partial_rewrite_and_postwrite(institute):
    graph,ids,release_id=institute
    program=plan_request(comparison(ids),release_id)
    result=Executor(graph,release_id).execute(program)
    pool=result['steps']
    claim=candidate('after',pool['after'])
    assert verify(claim,pool).label==Label.SUPPORTED
    assert verify(claim,pool,ambiguous=True).label==Label.AMBIGUOUS
    assert verify(claim,pool,counterevidence=True).label==Label.CONTRADICTED
    assert verify(claim.model_copy(update={'evidence_ids':['unknown']}),pool).label==Label.UNSUPPORTED
    assert verify(claim,{}).label==Label.NEI
    broad=candidate('after',pool['after'],requested_scope='all_real_world_publications')
    partial=verify(broad,pool)
    assert partial.label==Label.PARTIAL
    narrow=partial.licensed_claim
    answer=synthesize([partial],pool,{narrow.claim_id:'witness'})
    assert answer['claims'][0]['original_label']=='Partial'
    assert answer['claims'][0]['final_label']=='Supported'
    assert answer['claims'][0]['unmet_scope']=='all_real_world_publications'
    with pytest.raises(ValueError,match='unlicensed'):
        synthesize([partial],pool,{narrow.claim_id:'witness'},proposed_text=answer['text']+' This caused growth.')
    causal=claim.model_copy(update={'predicate':'causal_effect','value':True})
    assert verify(causal,pool).label==Label.NEI


def test_program_rejects_wrong_signatures_missing_windows_and_cypher(institute):
    graph,ids,release_id=institute
    original=plan_request(comparison(ids),release_id).model_dump(mode='json')
    for mutate in [lambda p:p['steps'][2]['arguments'].update(head_kind='Patent'),
                   lambda p:p['steps'][4]['arguments'].pop('window'),
                   lambda p:p['steps'][2]['arguments'].update(cypher='MATCH (n) DETACH DELETE n'),
                   lambda p:p['steps'][0]['depends_on'].append('after')]:
        broken=copy.deepcopy(original)
        mutate(broken)
        with pytest.raises(ValueError):
            GraphProgram.model_validate(broken)


def test_witness_cover_retains_all_proof_obligations():
    cover=greedy_cover(['a','b','counter'],{'one':{'covers':['a','b'],'cost':1},'two':{'covers':['counter'],'cost':4}},budget=2)
    assert cover['selected']==['one','two'] and cover['budget_relaxed']
    with pytest.raises(ValueError,match='Uncovered'):
        greedy_cover(['a','counter'],{'one':{'covers':['a'],'cost':1}},budget=100)


def test_bounded_patent_path_lags_unknown_dates_and_proof_coverage(store):
    from kdiff.construction.sources import BatchBuilder
    from kdiff.core.contracts import TimeRange
    from datetime import date
    b = BatchBuilder('fixture:path', 'fixture:records', store)
    b.record({'synthetic': 'path evidence'}, 'fixture:path-source')
    first = b.entity('Paper', 'fixture:first', {'title': 'First'}, '$.first')
    second = b.entity('Paper', 'fixture:second', {'title': 'Second'}, '$.second')
    patent = b.entity('Patent', 'fixture:patent', {'title': 'Patent'}, '$.patent')
    b.edge(first, 'citesPaper', second, '$.citation', TimeRange.parse('2018-02-01'))
    b.edge(second, 'citedByPatent', patent, '$.npl', TimeRange.parse('2020-03-01'), qualifiers={'date_basis': 'publication'})
    graph = b.finish().model_dump(mode='json')
    request = AnalysisRequest(family='path', kind='Paper', identifier=first, target_id=patent,
        relations=['citesPaper', 'citedByPatent'], max_hops=2,
        window=Window(start='2016-01-01', end='2022-12-31', reference_date='2025-01-01'))
    program = plan_request(request, 'release')
    result = Executor(graph, 'release').execute(program)
    assert len(result['result']['paths']) == 1
    lag = (date(2020, 3, 1) - date(2018, 2, 1)).days
    assert result['result']['paths'][0]['elapsed'] == {'minimum_days': lag, 'maximum_days': lag}
    from kdiff.analysis.general_witness import proof_selection
    claim = candidate(program.output, result['result'])
    obligations, cover = proof_selection(program, result, claim, graph, 1)
    assert cover['budget_relaxed']
    assert {x for x in obligations if x.startswith('fact:')} >= {'fact:' + a['assertion_id'] for a in graph['assertions']}
    changed = copy.deepcopy(graph)
    changed['assertions'][1]['valid_time'] = TimeRange().model_dump(mode='json')
    with pytest.raises(Incomplete, match='Unknown'):
        Executor(changed, 'release').execute(program)


def test_state_bindings_remain_pinned(institute):
    from kdiff.analysis.graph_program import Step
    graph, ids, release_id = institute
    window = Window(start='2020-01-01', end='2022-12-31', reference_date='2025-01-01')
    program = GraphProgram(release_id=release_id, output='read', steps=[
        Step(id='window', operator='NormalizeTime', arguments={'window': window.model_dump(mode='json')}),
        Step(id='write', operator='UpdateState', arguments={'key': 'window', 'input': 'window'}, depends_on=['window']),
        Step(id='read', operator='ReadState', arguments={'key': 'window'})])
    result = Executor(graph, release_id).execute(program)
    assert result['result']['value'] == window.model_dump(mode='json')
    assert result['state']['release_id'] == release_id
    with pytest.raises(ValueError, match='release changed'):
        Executor(graph, 'new-release', result['state'])
