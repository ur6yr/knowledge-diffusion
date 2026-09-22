"""Explicitly synthetic regression records, never an empirical source adapter."""

from kdiff.construction.sources import BatchBuilder
from kdiff.core.contracts import TimeRange


def institute_fixture(store, namespace='fixture:institute-x', before_count=14, after_count=31):
    if not namespace.startswith('fixture:'):
        raise ValueError('Synthetic data requires a fixture namespace')
    b=BatchBuilder(namespace,'synthetic_regression',store)
    b.record({'synthetic':True,'record':'institution','name':'Institute X'},'fixture:institution')
    institute=b.entity('Institution','fixture:institute',{'name':'Institute X'},'$.name')
    topic=b.entity('Topic','fixture:topic:ai',{'name':'Fixture AI'},'$.topic')
    other_topic=b.entity('Topic','fixture:topic:other',{'name':'Other fixture topic'},'$.other_topic')
    authors=[]
    for key,year,role in [('senior',2019,'senior researcher'),('junior',2022,'postdoc')]:
        b.record({'synthetic':True,'name':'A. Chen','join':year,'role':role},'fixture:author:'+key)
        aid=b.entity('Author','fixture:author:'+key,{'name':'A. Chen'},'$.name')
        authors.append(aid)
        b.edge(aid,'affiliatedWith',institute,'$.join',TimeRange.parse(year),'employment',
               {'role_title':role,'start_uncertainty':TimeRange.parse(year).model_dump(mode='json')})
    b.record({'synthetic':True,'name':'Fixture Faculty','affiliation':2000},'fixture:faculty')
    faculty=b.entity('Author','fixture:faculty',{'name':'Fixture Faculty'},'$.name')
    b.edge(faculty,'affiliatedWith',institute,'$.affiliation',TimeRange.parse(2000),'employment')
    for i in range(4):
        operational=i<3
        b.record({'synthetic':True,'site':f'Site {i+1}','operation':2018 if operational else None,
                  'announcement':2024,'planned_year':2025 if not operational else None},f'fixture:site:{i}')
        site=b.entity('Institution',f'fixture:site:{i}',{'name':f'Site {i+1}'},'$.site',subtype='site')
        b.edge(institute,'hasSite' if operational else 'announcedSite',site,
               '$.operation' if operational else '$.announcement',TimeRange.parse(2018 if operational else 2024),
               'operation' if operational else 'announcement',{'planned_year':None if operational else 2025})
    for group,count,start in [('before',before_count,2016),('after',after_count,2020),('outside',2,2010),('wrong-topic',3,2020)]:
        for i in range(count):
            year=start+i%3
            record={'synthetic':True,'id':f'fixture:paper:{group}:{i}','year':year,'topic': 'other' if group=='wrong-topic' else 'ai',
                    'author':'fixture:faculty','institution':'fixture:institute'}
            b.record(record,record['id'])
            paper=b.entity('Paper',record['id'],{'title':f'Fixture {group} {i}','publicationYear':year},'$.id')
            t=TimeRange.parse(year)
            b.edge(faculty,'authorOf',paper,'$.author',t,'publication',{'institution_ids':[institute],'date_basis':'publication'})
            b.edge(paper,'classifiedAs',other_topic if group=='wrong-topic' else topic,'$.topic',t,'publication')
            if i==0:
                # A second source version attests the same paper without creating
                # a second scientific object. Both assertions remain available.
                b.record({**record,'version':'corroboration'},record['id'])
                b.edge(faculty,'authorOf',paper,'$.author',t,'publication',{'institution_ids':[institute],'date_basis':'publication'})
    return b.finish(),{'institution':institute,'senior':authors[0],'junior':authors[1],'faculty':faculty,'topic':topic}
