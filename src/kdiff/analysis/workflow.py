"""Seven actual AutoGen roles over typed requests and complete release evidence."""

from kdiff.agents import AgentRun
from kdiff.core.contracts import digest
from kdiff.analysis.claims import AtomicClaim, candidate, verify, synthesize
from kdiff.analysis.graph_program import GraphProgram, Executor, Clarify, Incomplete
from kdiff.analysis.requests import AnalysisRequest, plan_request
from kdiff.analysis.witness import load_release
from kdiff.analysis.general_witness import make_program_witness


async def analyze(graph,store,profile,allow_mock,release_id,request:AnalysisRequest,state=None):
    release=load_release(store,release_id)
    if profile.profile=='mock' and not release['synthetic']:
        raise ValueError('Mock analysis is restricted to fixture releases')
    if state and state.get('release_id')!=release_id:
        raise ValueError('Dialogue cannot silently change release')
    working=graph.export(release['namespace'])
    if digest(working)!=release['graph_hash']:
        raise ValueError('Working graph changed. Create a new release for new questions')
    run=AgentRun(store,profile,allow_mock,release_id)
    state=state or {'release_id':release_id}
    if request.use_previous_entity:
        previous=state.get('resolved_ids',[])
        if len(previous)!=1:
            return {'type':'Clarify','reason':'No unique previous entity','witnesses':[]}
        request=request.model_copy(update={'identifier':previous[0],'name':None})
    # Production evidence comes from actual bounded Neo4j reads. The frozen copy
    # is used independently below for replay and cannot be supplied as a fake DB.
    executor=Executor(working,release_id,state)

    def resolve_subject(query:dict)->dict:
        """Resolve exact source IDs or names plus explicit context, never popularity."""
        if query!=request.model_dump(mode='json'):
            raise ValueError('Manager changed the requested scope')
        from kdiff.analysis.graph_program import Resolve
        try:
            return executor.resolve(Resolve(kind=request.kind,identifier=request.identifier,name=request.name,context=request.context))
        except Clarify as exc:
            return {'type':'Clarify','reason':str(exc),'candidates':exc.candidates}

    def validate_plan(program:GraphProgram)->dict:
        """Type-check a temporal program and enforce the requested question scope."""
        if program!=expected_plan:
            raise ValueError('Program differs from the authorized request')
        return program.model_dump(mode='json')

    def execute_plan(program_id:str)->dict:
        """Read deterministic evidence from the complete, checked Neo4j query result."""
        if program_id!=plan_id:
            raise ValueError('Unknown plan handle')
        return executor.execute(GraphProgram.model_validate(store.get(program_id)))

    def draft_claims(evidence_id:str)->dict:
        """Bind atomic candidates to specific immutable derivations and requested hypotheses."""
        if evidence_id!=execution_id:
            raise ValueError('Unknown evidence handle')
        pool=execution['steps']
        selected=[expected_plan.output]
        if request.family=='comparison':
            selected=['before','after',expected_plan.output]
        elif request.family=='sites':
            selected.append('announcements')
        claims=[candidate(s,pool[s], requested_scope=request.requested_scope).model_dump(mode='json') for s in selected]
        if request.ask_causation:
            base=claims[-1]
            claims.append({**base,'claim_id':digest([base['claim_id'],'causal']),
                           'predicate':'causal_effect','value':True})
        return {'claims':claims,'pool_id':execution_id}

    def verify_candidates(candidates_id:str)->dict:
        """Assign six-way labels using existing deterministic evidence only."""
        if candidates_id!=draft_id:
            raise ValueError('Unknown candidate handle')
        return {'verification':[verify(AtomicClaim.model_validate(c),execution['steps']).model_dump(mode='json')
                                for c in draft['claims']]}

    def build_evidence(verification_id:str)->dict:
        """Assign complete claim-local replay operands and recheck each witness."""
        if verification_id!=checked_id:
            raise ValueError('Unknown verification handle')
        mapping={}
        for v in checked['verification']:
            if v['licensed_claim']:
                claim=AtomicClaim.model_validate(v['licensed_claim'])
                mapping[claim.claim_id]=make_program_witness(store,release_id,expected_plan,execution,claim,state)
        return {'mapping':mapping}

    def synthesize_answer(assignment_id:str)->dict:
        """Render only licensed typed claims, preserving requested refusals."""
        if assignment_id!=assigned_id:
            raise ValueError('Unknown witness assignment')
        return synthesize(checked['verification'],execution['steps'],assigned['mapping'])

    try:
        subject=await run.turn('Manager',resolve_subject,{'query':request.model_dump(mode='json')},[release_id])
        if subject['type']=='Clarify':
            outcome={**subject,'witnesses':[],'state':state}
            return {**outcome,'run':run.save(outcome)}
        resolved_request=request.model_copy(update={'identifier':subject['ids'][0],'name':None})
        expected_plan=plan_request(resolved_request,release_id)
        for attempt in range(2):
            try:
                planned=await run.turn('Planner',validate_plan,{'program':expected_plan.model_dump(mode='json')},[release_id])
                break
            except ValueError:
                if attempt:
                    raise
        plan_id=store.put(planned)
        execution=await run.turn('Retriever',execute_plan,{'program_id':plan_id},[plan_id])
        execution_id=store.put(execution)
        draft=await run.turn('Drafting_Bridge',draft_claims,{'evidence_id':execution_id},[execution_id])
        draft_id=store.put(draft)
        checked=await run.turn('Verifier',verify_candidates,{'candidates_id':draft_id},[draft_id,execution_id])
        checked_id=store.put(checked)
        assigned=await run.turn('Evidence_Builder',build_evidence,{'verification_id':checked_id},[checked_id])
        assigned_id=store.put(assigned)
        outcome=await run.turn('Synthesizer',synthesize_answer,{'assignment_id':assigned_id},[assigned_id])
        outcome.update(witnesses=list(assigned['mapping'].values()),release_id=release_id,synthetic=release['synthetic'])
        outcome['state']={**execution['state'],'resolved_ids':subject['ids'],
                          'window':request.window.model_dump(mode='json'),'topic_id':request.topic_id,
                          'reference_date':request.window.reference_date.isoformat(),'prior_witnesses':outcome['witnesses']}
    except Clarify as exc:
        outcome={'type':'Clarify','reason':str(exc),'candidates':exc.candidates,'witnesses':[],'state':state}
    except Exception as exc:
        outcome={'type':'Abstain','reason':type(exc).__name__,
                 'detail':'Complete verified evidence could not be established. Inspect tool receipts.',
                 'witnesses':[],'state':state}
    return {**outcome,'run':run.save(outcome)}
