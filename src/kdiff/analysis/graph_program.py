"""Reconstructed typed temporal program and deterministic bounded executor.

Execution receives a complete, verified Neo4j export for the selected release.
The same executor operates on saved operands for network-free witness replay.
"""

from collections import deque
from datetime import date
from fractions import Fraction
from typing import Literal

from pydantic import Field, model_validator

from kdiff.core.contracts import Contract, TimeRange, Window, digest, membership
from kdiff.core.schema import EntityType, RELATIONS
from kdiff.analysis.program import OPERATORS


class Resolve(Contract):
    kind: EntityType
    identifier: str | None = None
    name: str | None = None
    context: dict = Field(default_factory=dict)

    @model_validator(mode='after')
    def valid(self):
        if not self.identifier and not self.name:
            raise ValueError('ResolveEntity needs an ID or name')
        if set(self.context)-{'institution_id','role','source_id'}:
            raise ValueError('Unknown identity context')
        return self


class Normalize(Contract):
    window: Window


class Filter(Contract):
    input: str
    window: str
    mode: Literal['contained','overlap'] = 'contained'


class Expand(Contract):
    input: str
    relation: str
    direction: Literal['out','in'] = 'out'
    head_kind: EntityType
    tail_kind: EntityType
    max_edges: int = Field(default=1000,ge=1,le=10000)

    @model_validator(mode='after')
    def valid(self):
        if self.relation not in RELATIONS:
            raise ValueError('Unknown relation')
        head,tails,_,_=RELATIONS[self.relation]
        if self.head_kind!=head or self.tail_kind not in tails:
            raise ValueError('Invalid relation signature')
        return self


class Path(Contract):
    input: str
    target_id: str
    relations: list[str] = Field(min_length=1,max_length=8)
    direction: Literal['out','in'] = 'out'
    window: str
    max_hops: int = Field(ge=1,le=6)
    max_paths: int = Field(default=100,ge=1,le=1000)
    temporal_rule: Literal['nondecreasing','within_window']

    @model_validator(mode='after')
    def valid(self):
        if set(self.relations)-set(RELATIONS):
            raise ValueError('Unknown path relation')
        return self


class Aggregate(Contract):
    input: str
    window: str
    unit: Literal['Paper','Institution','Author','Patent','Grant','Topic']
    policy: Literal['distinct_canonical_ids'] = 'distinct_canonical_ids'
    attribution: Literal['recorded_relation','publication_authorship'] = 'recorded_relation'
    institution_id: str | None = None
    topic_id: str | None = None
    source: str | None = None
    group_by: Literal['none','year'] = 'none'
    metric: Literal['count', 'event_trace', 'mobility_returns'] = 'count'
    subject_id: str | None = None

    @model_validator(mode='after')
    def valid(self):
        if self.metric != 'count' and not self.subject_id:
            raise ValueError('Event aggregation requires a resolved subject')
        if self.attribution=='publication_authorship' and (self.unit!='Paper' or not self.institution_id):
            raise ValueError('Institutional output requires paper units and authorship institution ID')
        return self


class Compare(Contract):
    left: str
    right: str
    operation: Literal['ratio','difference'] = 'ratio'
    matched_year_windows: bool = True


class Project(Contract):
    input: str


class Read(Contract):
    key: Literal['resolved_ids','window','topic_id','reference_date','prior_witnesses']


class Update(Contract):
    input: str
    key: Literal['resolved_ids','window','topic_id','reference_date','prior_witnesses']


ARGUMENTS = dict(zip(OPERATORS,(Resolve,Normalize,Filter,Expand,Path,Aggregate,Compare,Project,Read,Update)))
OUTPUTS = dict(zip(OPERATORS,('entities','window','edges','edges','paths','aggregate','comparison','provenance','state','state')))


class Step(Contract):
    id: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_]{0,63}$')
    operator: Literal['ResolveEntity','NormalizeTime','FilterInterval','Expand','PathSearch','Aggregate',
                      'CompareWindows','ProjectProvenance','ReadState','UpdateState']
    arguments: dict
    depends_on: list[str] = Field(default_factory=list)


class GraphProgram(Contract):
    grammar: Literal['temporal-graph-v1'] = 'temporal-graph-v1'
    release_id: str
    steps: list[Step] = Field(min_length=1,max_length=30)
    output: str

    @model_validator(mode='after')
    def valid(self):
        seen={}
        for step in self.steps:
            args=ARGUMENTS[step.operator].model_validate(step.arguments)
            refs={getattr(args,key) for key in ('input','window','left','right')
                  if hasattr(args,key) and isinstance(getattr(args,key),str)}
            if step.id in seen or refs!=set(step.depends_on) or any(ref not in seen for ref in refs):
                raise ValueError('Unbound, duplicate, cyclic or missing proof dependency')
            if hasattr(args,'window') and isinstance(args.window,str) and seen[args.window]!='window':
                raise ValueError('Window binding has the wrong type')
            if step.operator=='Expand' and seen[args.input] not in {'entities','edges'}:
                raise ValueError('Expand requires resolved entities')
            if step.operator in {'FilterInterval','Aggregate'} and seen[args.input]!='edges':
                raise ValueError('Filter and aggregate require typed relation evidence')
            if step.operator=='PathSearch' and seen[args.input]!='entities':
                raise ValueError('PathSearch requires resolved start entities')
            if step.operator=='CompareWindows' and any(seen[x]!='aggregate' for x in (args.left,args.right)):
                raise ValueError('CompareWindows requires two aggregates')
            seen[step.id]=OUTPUTS[step.operator]
            if step.operator == 'Aggregate' and args.metric != 'count':
                seen[step.id] = args.metric
        if self.output not in seen:
            raise ValueError('Output binding unavailable')
        return self


class Clarify(ValueError):
    def __init__(self, message, candidates=()):
        super().__init__(message)
        self.candidates=list(candidates)


class Incomplete(ValueError):
    pass


def overlap(time, window):
    if time.upper and time.upper<window.start or time.lower and time.lower>window.end:
        return 'outside'
    if time.lower and time.upper:
        return 'inside'
    return 'indeterminate'


class Executor:
    def __init__(self, graph, release_id, state=None):
        self.graph, self.release_id = graph, release_id
        self.entities={e['canonical_id']:e for e in graph['entities']}
        self.edges={a['assertion_id']:a for a in graph['assertions']}
        self.observations=graph['observations']
        self.state=dict(state or {})
        if self.state.get('release_id',release_id)!=release_id:
            raise ValueError('Dialogue release changed')

    def resolve(self,args):
        matches={eid for eid,e in self.entities.items() if e['kind']==args.kind}
        if args.identifier:
            matches &= {args.identifier} | {i['canonical_id'] for i in self.graph['identities'] if i['source_id']==args.identifier}
        if args.name:
            named={o['entity_id'] for o in self.observations
                   if str(o['attributes'].get('name',o['attributes'].get('title',''))).casefold()==args.name.casefold()
                   or args.name.casefold() in [str(a).casefold() for a in o['attributes'].get('aliases',[])]}
            matches &= named
        if args.context.get('institution_id'):
            matches &= {a['head_id'] for a in self.edges.values() if a['tail_id']==args.context['institution_id']
                        and a['relation'] in {'affiliatedWith','visitingAt'}
                        and (not args.context.get('role') or a['qualifiers'].get('role_title')==args.context['role'])}
        if args.context.get('source_id'):
            matches &= {i['canonical_id'] for i in self.graph['identities'] if i['source_id']==args.context['source_id']}
        if args.context.get('role') and not args.context.get('institution_id'):
            matches &= {a['head_id'] for a in self.edges.values() if a['qualifiers'].get('role_title')==args.context['role']}
        if len(matches)!=1:
            raise Clarify('Supply a unique identity and evidence-backed context',sorted(matches))
        return {'type':'entities','ids':sorted(matches),'kind':args.kind,'complete':True,'input_ids':sorted(
            o['observation_id'] for o in self.observations if o['entity_id'] in matches)}

    def expand(self,args,values):
        initial=values[args.input]
        required=args.head_kind if args.direction=='out' else args.tail_kind
        if initial['kind']!=required:
            raise ValueError('Expansion input does not match relation endpoint type')
        origin='head_id' if args.direction=='out' else 'tail_id'
        target='tail_id' if args.direction=='out' else 'head_id'
        starts=initial['ids'] if initial['type']=='entities' else [self.edges[x][initial['target']] for x in initial['ids']]
        rows=[a for a in self.edges.values() if a['relation']==args.relation and a[origin] in starts]
        if len(rows)>args.max_edges:
            raise Incomplete('Edge limit reached, no complete query certificate')
        if any(a['head_id'] not in self.entities or a['tail_id'] not in self.entities for a in rows):
            raise Incomplete('Missing relationship endpoint')
        return {'type':'edges','ids':sorted(a['assertion_id'] for a in rows),'target':target,
                'kind':args.tail_kind if args.direction=='out' else args.head_kind,'complete':True}

    def aggregate(self,args,values):
        source=values[args.input]
        if args.metric != 'count':
            from kdiff.analysis.temporal import event_trace, return_moves
            if any(self.edges[x]['head_id'] != args.subject_id or self.edges[x]['relation'] != 'affiliatedWith' for x in source['ids']):
                raise ValueError('Event trace input must contain only the subject affiliations')
            function = event_trace if args.metric == 'event_trace' else return_moves
            return function(self.graph, args.subject_id, Window.model_validate(values[args.window]['window']))
        if source['kind']!=args.unit or not source['complete']:
            raise ValueError('Aggregate unit or completeness mismatch')
        window=Window.model_validate(values[args.window]['window'])
        if source.get('window') and source['window']!=window.model_dump(mode='json'):
            raise ValueError('Filtered and aggregate windows disagree')
        rows=[self.edges[x] for x in source.get('filter_input_ids',source['ids'])]
        if args.source:
            rows=[r for r in rows if r['provenance']['source']==args.source]
            if not any(s['source']==args.source for s in self.graph['sources']):
                raise Incomplete('Requested source is absent')
        by_object={}
        for row in rows:
            if args.attribution=='publication_authorship':
                if row['relation']!='authorOf' or source['target']!='tail_id':
                    raise ValueError('Institution output must use publication-time authorship')
                if args.institution_id not in row['qualifiers'].get('institution_ids',[]):
                    continue
            obj=row[source['target']]
            if args.topic_id:
                if args.topic_id not in self.entities:
                    raise Incomplete('Requested topic endpoint absent')
                if not any(a['head_id']==obj and a['relation']=='classifiedAs' and a['tail_id']==args.topic_id for a in self.edges.values()):
                    continue
            by_object.setdefault(obj,[]).append(row)
        selected=[]
        series={}
        for obj,group in by_object.items():
            statuses={membership(TimeRange.model_validate(r['valid_time']),window) for r in group}
            if 'indeterminate' in statuses or len(statuses)>1:
                raise Incomplete('Unknown or disagreeing dates affect the aggregate')
            if statuses=={'inside'}:
                selected.append(obj)
                if args.group_by=='year':
                    years={TimeRange.model_validate(r['valid_time']).lower.year for r in group}
                    if len(years)!=1 or any(TimeRange.model_validate(r['valid_time']).upper.year not in years for r in group):
                        raise Incomplete('Year assignment is ambiguous')
                    year=str(next(iter(years)))
                    series[year]=series.get(year,0)+1
        return {'type':'aggregate','value':len(selected),'ids':sorted(selected),
                'input_ids':sorted(source.get('filter_input_ids',source['ids'])),'unit':args.unit,'policy':args.policy,
                'attribution':args.attribution,'institution_id':args.institution_id,'topic_id':args.topic_id,
                'source':args.source,'window':window.model_dump(mode='json'),'series':series,
                'complete':True,'scope':'Recorded evidence in this frozen release'}

    def paths(self,args,values):
        if args.target_id not in self.entities:
            raise Incomplete('Path target missing, not proof of no path')
        window=Window.model_validate(values[args.window]['window'])
        origin,target=('head_id','tail_id') if args.direction=='out' else ('tail_id','head_id')
        queue=deque((eid,[],[eid],None) for eid in values[args.input]['ids'])
        paths=[]
        inspected=set()
        states=0
        while queue:
            states+=1
            if states>10000:
                raise Incomplete('Path search state budget exhausted')
            current,edges,vertices,previous=queue.popleft()
            if len(edges)>=args.max_hops:
                continue
            for a in sorted(self.edges.values(),key=lambda x:x['assertion_id']):
                if a[origin]!=current or a['relation'] not in args.relations:
                    continue
                inspected.add(a['assertion_id'])
                interval=TimeRange.model_validate(a['valid_time'])
                status=membership(interval,window)
                if status=='indeterminate':
                    raise Incomplete('Unknown path event time')
                if status=='outside':
                    continue
                if a[target] not in self.entities:
                    raise Incomplete('Path endpoint missing')
                if args.temporal_rule=='nondecreasing' and previous and interval.lower<previous:
                    # Overlapping uncertainty cannot prove the required order.
                    if interval.upper>=previous:
                        raise Incomplete('Path event ordering ambiguous')
                    continue
                next_edges=edges+[a['assertion_id']]
                next_vertices=vertices+[a[target]]
                if a[target]==args.target_id:
                    first = TimeRange.model_validate(self.edges[next_edges[0]]['valid_time'])
                    elapsed = {'minimum_days': max(0, (interval.lower - first.upper).days),
                               'maximum_days': max(0, (interval.upper - first.lower).days)}
                    if len(next_edges) == 1:
                        elapsed = {'minimum_days': 0, 'maximum_days': 0}
                    paths.append({'assertions':next_edges,'entities':next_vertices, 'elapsed': elapsed})
                    if len(paths)>args.max_paths:
                        raise Incomplete('Path result budget exhausted')
                elif a[target] not in vertices:
                    queue.append((a[target],next_edges,next_vertices,interval.upper))
        return {'type':'paths','paths':paths,'input_ids':sorted(inspected),'complete':True,
                'max_hops':args.max_hops,'temporal_rule':args.temporal_rule,'window':window.model_dump(mode='json')}

    def execute(self,program:GraphProgram):
        if program.release_id!=self.release_id:
            raise ValueError('Program release mismatch')
        values={}
        for step in program.steps:
            args=ARGUMENTS[step.operator].model_validate(step.arguments)
            op=step.operator
            if op=='ResolveEntity':
                result=self.resolve(args)
            elif op=='NormalizeTime':
                result={'type':'window','window':args.window.model_dump(mode='json')}
            elif op=='Expand':
                result=self.expand(args,values)
            elif op=='FilterInterval':
                prior=values[args.input]
                window=Window.model_validate(values[args.window]['window'])
                selected=[]
                for aid in prior['ids']:
                    time=TimeRange.model_validate(self.edges[aid]['valid_time'])
                    status=(membership if args.mode=='contained' else overlap)(time,window)
                    if status=='indeterminate':
                        raise Incomplete('Temporal filter membership indeterminate')
                    if status=='inside':
                        selected.append(aid)
                result={**prior,'ids':selected,'filter_input_ids':prior['ids'],'window':window.model_dump(mode='json')}
            elif op=='Aggregate':
                result=self.aggregate(args,values)
            elif op=='PathSearch':
                result=self.paths(args,values)
            elif op=='CompareWindows':
                left,right=values[args.left],values[args.right]
                for key in ('unit','policy','attribution','institution_id','topic_id','source'):
                    if left[key]!=right[key]:
                        raise ValueError('Incompatible aggregate comparison policies')
                a,b=Window.model_validate(left['window']),Window.model_validate(right['window'])
                if a.reference_date!=b.reference_date or a.end>=b.start:
                    raise ValueError('Comparison windows overlap, are reversed or use different references')
                if args.matched_year_windows and (a.start.month,a.start.day,a.end.month,a.end.day,a.end.year-a.start.year)!=(b.start.month,b.start.day,b.end.month,b.end.day,b.end.year-b.start.year):
                    raise ValueError('Comparison window lengths/patterns do not match')
                if args.operation=='ratio' and not left['value']:
                    raise Incomplete('Ratio denominator is zero')
                value=Fraction(right['value'],left['value']) if args.operation=='ratio' else Fraction(right['value']-left['value'])
                result={'type':'comparison','operation':args.operation,'numerator':value.numerator,'denominator':value.denominator,
                        'before':left,'after':right,'complete':True,'interpretation':'temporal_association_only'}
            elif op=='ProjectProvenance':
                result={'type':'provenance','derivation':values[args.input],
                        'source_hashes':sorted(s['sha256'] for s in self.graph['sources']),
                        'graph_hash':digest(self.graph),'complete':True}
            elif op=='ReadState':
                if args.key not in self.state:
                    raise Clarify('Dialogue state is missing the requested context')
                result={'type':'state','key':args.key,'value':self.state[args.key]}
            else:
                value = values[args.input]
                if args.key == 'resolved_ids' and value['type'] == 'entities':
                    updated = value['ids']
                elif args.key == 'topic_id' and value['type'] == 'entities' and value['kind'] == 'Topic' and len(value['ids']) == 1:
                    updated = value['ids'][0]
                elif args.key in {'window', 'reference_date'} and value['type'] == 'window':
                    updated = value['window'] if args.key == 'window' else value['window']['reference_date']
                elif value['type'] == 'state' and value['key'] == args.key:
                    updated = value['value']
                else:
                    raise ValueError('State update has the wrong value type')
                self.state[args.key]=updated
                self.state['release_id']=self.release_id
                result={'type':'state','key':args.key,'value':self.state[args.key]}
            values[step.id]=result
        return {'result':values[program.output],'steps':values,'state':self.state,
                'release_id':self.release_id,'complete':True,'graph_hash':digest(self.graph)}
