"""Typed request templates. Free-form questions are translated by a live planner."""

from typing import Literal
from pydantic import Field, model_validator
from kdiff.core.contracts import Contract, Window
from kdiff.analysis.graph_program import GraphProgram, Step
from kdiff.core.schema import EntityType


class AnalysisRequest(Contract):
    family: Literal['count','comparison','sites','path','timeline','state','returns','identity']
    kind: EntityType
    identifier: str | None = None
    name: str | None = None
    context: dict = Field(default_factory=dict)
    window: Window
    before: Window | None = None
    topic_id: str | None = None
    institution_id: str | None = None
    target_id: str | None = None
    relations: list[str] = Field(default_factory=list)
    max_hops: int = Field(default=3,ge=1,le=6)
    temporal_rule: Literal['nondecreasing','within_window'] = 'nondecreasing'
    direction: Literal['out', 'in'] = 'out'
    ask_causation: bool = False
    use_previous_entity: bool = False
    requested_scope: Literal['frozen_release', 'real_world'] = 'frozen_release'

    @model_validator(mode='after')
    def valid(self):
        if self.family=='comparison' and self.before is None:
            raise ValueError('Comparison needs explicit before and after windows')
        if self.family=='path' and (not self.target_id or not self.relations):
            raise ValueError('Path request needs a target and explicit relations')
        if self.family in {'count','comparison'} and self.kind not in {'Author','Institution'}:
            raise ValueError('Publication counts require an author or institution')
        if self.family=='sites' and self.kind!='Institution':
            raise ValueError('Sites require an institution')
        if self.family in {'timeline','state','returns'} and self.kind!='Author':
            raise ValueError('Affiliation timelines require an author')
        return self


def plan_request(request,release_id):
    steps=[]
    def add(id,op,args,deps=()):
        steps.append(Step(id=id,operator=op,arguments=args,depends_on=list(deps)))
    add('subject','ResolveEntity',{'kind':request.kind,'identifier':request.identifier,
        'name':request.name,'context':request.context})
    add('window','NormalizeTime',{'window':request.window.model_dump(mode='json')})
    if request.family == 'identity':
        output = 'subject'
    elif request.family=='path':
        add('paths','PathSearch',{'input':'subject','target_id':request.target_id,'relations':request.relations,
            'window':'window','max_hops':request.max_hops,'temporal_rule':request.temporal_rule,
            'direction': request.direction},['subject','window'])
        output='paths'
    elif request.family in {'count','comparison'}:
        start='subject'
        if request.kind=='Institution':
            add('authors','Expand',{'input':start,'relation':'affiliatedWith','direction':'in',
                'head_kind':'Author','tail_kind':'Institution'},[start])
            start='authors'
        add('papers','Expand',{'input':start,'relation':'authorOf','head_kind':'Author','tail_kind':'Paper'},[start])
        args={'input':'papers','window':'window','unit':'Paper','topic_id':request.topic_id,
              'attribution':'publication_authorship' if request.kind=='Institution' or request.institution_id else 'recorded_relation',
              'institution_id':request.institution_id or (request.identifier if request.kind=='Institution' else None)}
        add('after','Aggregate',args,['papers','window'])
        output='after'
        if request.family=='comparison':
            add('before_window','NormalizeTime',{'window':request.before.model_dump(mode='json')})
            add('before','Aggregate',{**args,'window':'before_window'},['papers','before_window'])
            add('comparison','CompareWindows',{'left':'before','right':'after'},['before','after'])
            output='comparison'
    else:
        relation='hasSite' if request.family=='sites' else 'affiliatedWith'
        add('relations','Expand',{'input':'subject','relation':relation,'head_kind':request.kind,
            'tail_kind':'Institution'},['subject'])
        if request.family=='sites':
            add('sites','Aggregate',{'input':'relations','window':'window','unit':'Institution'},['relations','window'])
            add('announcements','Expand',{'input':'subject','relation':'announcedSite',
                'head_kind':'Institution','tail_kind':'Institution'},['subject'])
            output='sites'
        elif request.family in {'state', 'returns'}:
            add('trace', 'Aggregate', {'input': 'relations', 'window': 'window', 'unit': 'Institution',
                'subject_id': request.identifier, 'metric': 'event_trace' if request.family == 'state' else 'mobility_returns'}, ['relations', 'window'])
            output = 'trace'
        else:
            add('timeline','FilterInterval',{'input':'relations','window':'window','mode':'overlap'},['relations','window'])
            output='timeline'
    add('provenance','ProjectProvenance',{'input':output},[output])
    return GraphProgram(release_id=release_id,steps=steps,output=output)
