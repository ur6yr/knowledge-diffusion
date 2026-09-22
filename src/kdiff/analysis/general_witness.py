"""Complete graph-program operands and claim-local, network-free witnesses."""

from kdiff.core.contracts import digest
from kdiff.analysis.graph_program import GraphProgram, Executor
from kdiff.analysis.claims import AtomicClaim, verify, render, greedy_cover
from kdiff.analysis.witness import load_release

WITNESS_VERSION = 'temporal-program-witness-v2'


def proof_selection(program, execution, claim, graph, budget):
    """Cover source and derivation obligations with shared evidence bundles."""
    obligations = {'release_integrity', 'claim_entailment', 'counterevidence'}
    offers = {
        'release': {'covers': ['release_integrity'], 'cost': 1, 'references': ['frozen_graph']},
        'full_pool': {'covers': ['counterevidence'], 'cost': 1, 'references': ['complete_execution']},
        'claim': {'covers': ['claim_entailment'], 'cost': 1, 'references': [claim.claim_id]},
    }
    records = {x['assertion_id']: x for x in graph['assertions']}
    records.update({x['observation_id']: x for x in graph['observations']})
    by_source = {}
    for step in program.steps:
        value = execution['steps'][step.id]
        required = ['typed_derivation:' + step.id, 'complete_operands:' + step.id]
        obligations.update(required)
        offers['step:' + step.id] = {'covers': required, 'cost': 1, 'references': [step.id]}
        operands = set(value.get('input_ids', []))
        if value['type'] == 'edges':
            operands.update(value['ids'])
            operands.update(value.get('filter_input_ids', []))
        for record_id in operands:
            record = records.get(record_id)
            if record is None:
                raise ValueError('Witness derivation references an unavailable graph operand')
            source = record['provenance']['raw_artifact_pointer']
            obligation = 'fact:' + record_id
            obligations.add(obligation)
            by_source.setdefault(source, set()).add(obligation)
    for source, covered in sorted(by_source.items()):
        offers['source:' + source] = {'covers': sorted(covered), 'cost': 1,
                                     'references': [source] + sorted(x[5:] for x in covered)}
    selected = greedy_cover(sorted(obligations), offers, budget)
    selected['cost_unit'] = 'evidence_cards'
    selected['bundles'] = {key: offers[key] for key in selected['selected']}
    return sorted(obligations), selected


def make_program_witness(store,release_id,program,result,claim,state=None,budget=32):
    pool=result['steps']
    checked=verify(claim,pool)
    if checked.label!='Supported':
        raise ValueError('Unverified claim cannot receive a witness')
    release = load_release(store, release_id)
    obligations, cover = proof_selection(program, result, claim, release['graph'], budget)
    witness={'kind':WITNESS_VERSION,'release_id':release_id,
             'program':program.model_dump(mode='json'),'state_before':state or {},
             'complete_execution':result,'claim':claim.model_dump(mode='json'),
             'proof_obligations':obligations,'selection':cover,
             'display':render(claim,pool)}
    wid=store.put(witness)
    replay_program(store,wid)
    return wid


def replay_program(store,witness_id):
    witness=store.get(witness_id)
    if witness['kind'] not in {'temporal-program-witness-v1', WITNESS_VERSION}:
        raise ValueError('Unsupported program witness')
    release=load_release(store,witness['release_id'])
    program=GraphProgram.model_validate(witness['program'])
    execution=Executor(release['graph'],witness['release_id'],witness['state_before']).execute(program)
    legacy = witness['kind'] == 'temporal-program-witness-v1'
    if legacy:
        # Version 1 did not expose completeness on resolved identities or path
        # lag fields. Remove only these additive fields when replaying old runs.
        import copy
        execution = copy.deepcopy(execution)
        pairs = [(execution['result'], witness['complete_execution']['result'])]
        pairs += [(value, witness['complete_execution']['steps'][key]) for key, value in execution['steps'].items()]
        for value, saved in pairs:
            if value.get('type') == 'entities' and 'complete' not in saved:
                value.pop('complete', None)
            if value.get('type') == 'paths':
                for path, old in zip(value['paths'], saved['paths']):
                    if 'elapsed' not in old:
                        path.pop('elapsed', None)
    if execution!=witness['complete_execution']:
        raise ValueError('Program operand set or derived result does not replay')
    claim=AtomicClaim.model_validate(witness['claim'])
    checked=verify(claim,execution['steps'])
    if checked.label!='Supported' or witness['display']!=render(claim,execution['steps']):
        raise ValueError('Claim does not replay')
    if legacy:
        expected = ['release_integrity','complete_operands','typed_derivation','claim_entailment','counterevidence']
        selection = greedy_cover(expected, {'full-replay': {'covers': expected, 'cost': len(execution['steps']) + 1}},
                                 witness['selection']['requested_budget'])
    else:
        expected, selection = proof_selection(program, execution, claim, release['graph'], witness['selection']['requested_budget'])
    if witness['proof_obligations']!=expected or witness['selection']!=selection:
        raise ValueError('Witness omits proof obligations')
    return {'status':'verified','witness_id':witness_id,'release_id':witness['release_id'],
            'claim':claim.model_dump(mode='json'),'result':execution['result'],'text':witness['display']}
