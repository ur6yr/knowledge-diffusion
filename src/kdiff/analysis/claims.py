"""Atomic claims, six-label decisions, proof coverage and constrained synthesis."""

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

from pydantic import Field

from kdiff.core.contracts import Contract, Label, digest


class AtomicClaim(Contract):
    claim_id: str
    predicate: Literal['recorded_count','recorded_comparison','recorded_paths','recorded_facts',
                       'scoped_absence','causal_effect','documented_causal_attribution',
                       'plausible_contribution','capability_transformation', 'recorded_identity',
                       'recorded_state', 'recorded_returns']
    step_id: str
    value: Any
    requested_scope: str = 'frozen_release'
    evidence_ids: list[str] = Field(default_factory=list)
    original_claim_id: str | None = None


class Verification(Contract):
    claim: AtomicClaim
    label: Label
    reason: str
    licensed_claim: AtomicClaim | None = None
    original_label: Label
    unmet_scope: str | None = None


def candidate(step_id, result, *, requested_scope='frozen_release'):
    kind=result['type']
    predicate={'aggregate':'recorded_count','comparison':'recorded_comparison',
               'paths':'recorded_paths','edges':'recorded_facts', 'entities': 'recorded_identity',
               'event_trace': 'recorded_state', 'mobility_returns': 'recorded_returns'}.get(kind)
    if not predicate:
        raise ValueError('This output cannot be presented as a factual claim')
    value=result['value'] if kind=='aggregate' else result
    return AtomicClaim(claim_id=digest([step_id,predicate,value,requested_scope]),
                       predicate=predicate,step_id=step_id,value=value,
                       requested_scope=requested_scope,evidence_ids=[digest(result)])


def verify(claim, pool, *, ambiguous=False, counterevidence=False):
    """Immutable pool only. No graph, source, model or fetch capability is accepted."""
    def decision(label,reason,licensed=None,unmet=None):
        return Verification(claim=claim,label=label,original_label=label,reason=reason,
                            licensed_claim=licensed,unmet_scope=unmet)
    if ambiguous:
        return decision(Label.AMBIGUOUS,'Identity or temporal interpretation is unresolved')
    if counterevidence:
        return decision(Label.CONTRADICTED,'Full evidence pool contains counterevidence')
    result=pool.get(claim.step_id)
    if result is None or not result.get('complete',False):
        return decision(Label.NEI,'Complete evidence is unavailable')
    if claim.evidence_ids!=[digest(result)]:
        return decision(Label.UNSUPPORTED,'Candidate does not cite the supplied evidence')
    if claim.predicate in {'causal_effect','documented_causal_attribution','plausible_contribution','capability_transformation'}:
        # A graph association is never upgraded to a stronger interpretation.
        return decision(Label.NEI,'No verified evidence template licenses this interpretation')
    if claim.predicate=='scoped_absence':
        if result.get('type') not in {'aggregate','edges','paths'} or claim.value!=0:
            return decision(Label.UNSUPPORTED,'Absence needs a complete typed empty-result query')
        size=result.get('value',len(result.get('ids',result.get('paths',[]))))
        if size!=0:
            return decision(Label.CONTRADICTED,'The complete query returned recorded evidence')
        if claim.requested_scope!='frozen_release':
            narrow=claim.model_copy(update={'requested_scope':'frozen_release',
                'original_claim_id':claim.claim_id,'claim_id':digest([claim.claim_id,'scoped'])})
            return decision(Label.PARTIAL,'Only absence in the declared snapshot is established',narrow,claim.requested_scope)
        return decision(Label.SUPPORTED,'Complete query establishes scoped absence',claim)
    expected=candidate(claim.step_id,result)
    if claim.predicate!=expected.predicate or claim.value!=expected.value:
        return decision(Label.CONTRADICTED,'Candidate conflicts with deterministic evidence')
    if claim.requested_scope!='frozen_release':
        narrow=expected.model_copy(update={'original_claim_id':claim.claim_id})
        return decision(Label.PARTIAL,'Imported coverage supports a narrower proposition',narrow,claim.requested_scope)
    return decision(Label.SUPPORTED,'Claim matches complete deterministic evidence',claim)


def greedy_cover(obligations, offers, budget):
    """Weighted greedy cover with deterministic ties and explicit budget relaxation."""
    remaining=set(obligations)
    selected=[]
    cost=0
    while remaining:
        choices=[]
        for key,offer in offers.items():
            gain=remaining & set(offer['covers'])
            if gain:
                if offer['cost']<=0:
                    raise ValueError('Witness costs must be positive')
                choices.append((-(len(gain)/offer['cost']),key,gain,offer['cost']))
        if not choices:
            raise ValueError('Uncovered witness proof obligation')
        _,key,gain,weight=min(choices,key=lambda x:(x[0],x[1]))
        selected.append(key)
        remaining-=gain
        cost+=weight
    return {'selected':selected,'cost':cost,'requested_budget':budget,'budget_relaxed':cost>budget,
            'covered':sorted(obligations)}


def render(claim,pool):
    result=pool[claim.step_id]
    if claim.predicate=='recorded_count':
        window=result['window']
        return (f"The frozen release records {result['value']} distinct {result['unit']} objects "
                f"within {window['start']} to {window['end']} under the {result['attribution']} policy.")
    if claim.predicate=='recorded_comparison':
        value=(Decimal(result['numerator'])/Decimal(result['denominator'])).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)
        return (f"Recorded output changes from {result['before']['value']} to {result['after']['value']}. "
                f"The {result['operation']} is {value}. This is a temporal association within the imported scope.")
    if claim.predicate=='recorded_paths':
        return f"The frozen release contains {len(result['paths'])} qualifying paths within {result['max_hops']} hops under the recorded temporal constraints."
    if claim.predicate=='scoped_absence':
        return 'The complete query found no matching records in this frozen release. This does not establish real-world absence.'
    if claim.predicate=='recorded_facts':
        return f"The frozen release contains {len(result['ids'])} matching source assertions."
    if claim.predicate == 'recorded_identity':
        return 'The supplied identity context resolves to ' + ', '.join(result['ids']) + ' in this release.'
    if claim.predicate == 'recorded_state':
        return f"The observed event trace contains {len(result['states'])} dated states. Concurrent and uncertain affiliations are retained in the witness."
    if claim.predicate == 'recorded_returns':
        return (f"The release supports {len(result['returns'])} documented A-to-B-to-A traces and "
                f"{len(result['ambiguous_candidates'])} uncertain candidates. Elapsed date bounds are in the witness.")
    raise ValueError('Unlicensed interpretation cannot be rendered')


def synthesize(verifications,pool,mapping,proposed_text=None):
    lines=[]
    retained=[]
    refusals=[]
    for value in verifications:
        result=Verification.model_validate(value) if isinstance(value,dict) else value
        licensed=result.licensed_claim
        if licensed is None:
            refusals.append({'claim_id':result.claim.claim_id,'label':result.label.value,'reason':result.reason})
            continue
        final=verify(licensed,pool)
        if final.label!=Label.SUPPORTED or licensed.claim_id not in mapping:
            raise ValueError('Final claim lacks a supportable assigned witness')
        lines.append(render(licensed,pool)+f' [witness {mapping[licensed.claim_id]}]')
        retained.append({'original':result.claim.model_dump(mode='json'),'original_label':result.original_label.value,
                         'final':licensed.model_dump(mode='json'),'final_label':final.label.value,
                         'rewrite':licensed.model_dump(mode='json') if result.label==Label.PARTIAL else None,
                         'unmet_scope':result.unmet_scope})
    lines.extend('Withheld: '+r['reason']+'.' for r in refusals)
    text='\n'.join(lines)
    # The typed synthesis contract permits only licensed templates. Comparing the
    # complete rendering checks every connective sentence, not selected regexes.
    if proposed_text is not None and proposed_text!=text:
        raise ValueError('Post-synthesis output introduced an unlicensed change')
    return {'text':text,'claims':retained,'refusals':refusals,'post_write_check':'full_typed_rendering',
            'claim_to_witness':mapping,'type':'Answer' if retained else 'Abstain'}
