import json
import socket
from datetime import date

import pytest

from kdiff.construction.sources import BatchBuilder, parse_orcid, parse_ror, parse_patcit, parse_source
from kdiff.construction.resolution import resolve_identifiers, conflict_view
from kdiff.construction.web import public_destination, capture_document, SpanExtraction, validate_extraction
from kdiff.core.durable import TaskLedger


def orcid_record(start='2016'):
    return {'orcid-identifier': {'uri': 'https://orcid.org/0000-0000-0000-0001'},
        'person': {'name': {'given-names': {'value': 'A.'}, 'family-name': {'value': 'Chen'}}},
        'activities-summary': {'employments': {'affiliation-group': [{'summaries': [{'employment-summary': {
            'organization': {'name': 'Fixture Institute', 'disambiguated-organization': {
                'disambiguation-source': 'ROR', 'disambiguated-organization-identifier': 'https://ror.org/fixture'}},
            'start-date': {'year': {'value': start}}, 'end-date': None, 'role-title': 'Researcher'}}]}]}}}


def test_orcid_unknown_end_and_alternative_dates(store):
    b = BatchBuilder('fixture:orcid', 'orcid', store)
    parse_orcid(orcid_record(), b)
    parse_orcid(orcid_record('2017'), b)
    batch = b.finish()
    assert len(batch.entities) == 2
    assert len(batch.assertions) == 2
    assert all(a.valid_time.end_kind == 'unknown' and a.valid_time.upper is None for a in batch.assertions)
    assert batch.assertions[0].qualifiers['start_uncertainty']['precision'] == 'year'
    assert conflict_view([a.model_dump(mode='json') for a in batch.assertions])[0]['conflict_flag']
    assert all(store.get_bytes(a.provenance.raw_artifact_pointer) for a in batch.assertions)


def test_ror_native_v2_parent_and_no_invented_dates(store):
    b = BatchBuilder('fixture:ror', 'ror', store)
    parse_ror({'id': 'https://ror.org/fixture', 'names': [{'value':'Institute','types':['ror_display']}],
               'relationships': [{'id':'https://ror.org/parent','label':'Parent','type':'parent'}]}, b)
    batch = b.finish()
    assert batch.assertions[0].relation == 'subunitOf'
    assert batch.assertions[0].valid_time.lower is None


def test_uspto_and_patcit_direction_date_basis(tmp_path, store):
    path = tmp_path/'patent.xml'
    path.write_text('''<?xml version="1.0"?><us-patent-grant><us-bibliographic-data-grant>
      <publication-reference><document-id><country>US</country><doc-number>FIXTURE1</doc-number><kind>B2</kind><date>20200102</date></document-id></publication-reference>
      <application-reference><document-id><date>20180304</date></document-id></application-reference>
      <invention-title>Fixture Device</invention-title><references-cited><citation><nplcit><othercit>Unresolved fixture citation</othercit></nplcit></citation></references-cited>
      </us-bibliographic-data-grant></us-patent-grant>''')
    patent_batch, report = parse_source(path, store, 'fixture:patents', 'uspto', format='xml')
    attrs = patent_batch.observations[0].attributes
    assert attrs['filingDate'] == '2018-03-04' and attrs['grantDate'] == '2020-01-02'
    assert len(report['unresolved']) == 1
    b = BatchBuilder('fixture:patents', 'patcit', store)
    record = {'id':'fixture-npl','patent':'US-FIXTURE1-B2','doi':'10.9999/fixture-only','date':'2020-01-02','basis':'grant'}
    columns = {'record_id':'id','patent_id':'patent','doi':'doi','date':'date','date_basis':'basis'}
    parse_patcit(record,b,columns)
    batch = b.finish()
    assert batch.assertions[0].relation == 'citedByPatent'
    assert batch.assertions[0].tail_id == patent_batch.entities[0].canonical_id
    assert batch.assertions[0].qualifiers['date_basis'] == 'grant'


def test_xml_entities_rejected(tmp_path, store):
    path = tmp_path/'attack.xml'
    path.write_text('<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><us-patent-grant>&e;</us-patent-grant>')
    with pytest.raises(Exception):
        parse_source(path, store, 'fixture:xml', 'uspto', format='xml')


def test_source_limits_not_empty_success(tmp_path, store):
    p=tmp_path/'ror.json'
    p.write_text('[]')
    with pytest.raises(ValueError, match='Empty'):
        parse_source(p,store,'fixture:ror','ror',format='json-array')
    p.write_text('[{},{}]')
    with pytest.raises(ValueError):
        parse_source(p,store,'fixture:ror','ror',format='json-array',max_bytes=2)


def test_identifier_conflict_and_namesake_no_merge(store):
    b = BatchBuilder('fixture:identity','web',store)
    b.record({'record':'a'},'a')
    first=b.entity('Author','first',{'name':'A. Chen','orcidID':'https://orcid.org/first'},'$')
    b.entity('Author','second',{'name':'A. Chen','orcidID':'https://orcid.org/second'},'$')
    batch, decisions=resolve_identifiers(b.finish(),{})
    assert len(batch.entities)==2
    b.record({'record':'b'},'b')
    b.entity('Author','first',{'orcidID':'https://orcid.org/second'},'$')
    batch, decisions=resolve_identifiers(b.finish(),{})
    assert any(d['status']=='conflict' for d in decisions)
    assert len(batch.entities)==2


def test_public_fetch_private_dns_and_scope():
    resolver=lambda *a, **k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]
    with pytest.raises(ValueError,match='non-public'):
        public_destination('https://example.org/a',{'example.org'},resolver)
    for url in ['http://example.org','https://evil.org','https://u:p@example.org','https://example.org:8443']:
        with pytest.raises(ValueError):
            public_destination(url,{'example.org'},resolver)


def test_document_spans_are_real_operands(store):
    text='Alice works at Institute X since 2020.'
    doc=capture_document(text.encode(),'text/plain','https://example.org/cv',store)
    value={'entities':[{'key':'a','kind':'Author','name':'Alice','start':0,'end':5},
                       {'key':'i','kind':'Institution','name':'Institute X','start':15,'end':26}],
           'facts':[{'head':'a','relation':'affiliatedWith','tail':'i','start':0,'end':len(text),'quote':text,'date_text':'2020'}]}
    batch=validate_extraction(doc,SpanExtraction.model_validate(value),store,'fixture:web')
    assert len(batch.assertions)==1 and len(batch.sources)==1
    assert batch.sources[0]['source']=='web'
    value['facts'][0]['quote']='Alice obtained a doctorate at Institute X.'
    with pytest.raises(ValueError,match='absent'):
        validate_extraction(doc,SpanExtraction.model_validate(value),store,'fixture:web')


def test_fenced_recovery_and_durable_intent(tmp_path, monkeypatch):
    import kdiff.core.durable as durable
    clock=[100.0]
    monkeypatch.setattr(durable.time,'time',lambda:clock[0])
    ledger=TaskLedger(tmp_path/'tasks.db',initialize=True)
    task=ledger.enqueue({'seed':'a'})
    assert ledger.enqueue({'seed':'a'})==task
    old=ledger.claim('worker-old',ttl=1)
    ledger.prepare(old,'batch-a')
    clock[0]=102
    new=ledger.claim('worker-new',ttl=30)
    with pytest.raises(ValueError,match='Stale'):
        ledger.complete(old,{'bad':True})
    writes=[]
    def commit_then_crash():
        writes.append('batch-a')
        raise RuntimeError('crash after external commit')
    with pytest.raises(RuntimeError):
        ledger.integrate(new,'batch-a',commit_then_crash)
    clock[0]=133
    recovered=TaskLedger(tmp_path/'tasks.db').claim('worker-recover',ttl=30)
    result=ledger.integrate(recovered,'batch-a',lambda:{'reused': 'batch-a' in writes})
    assert result['reused']
    assert ledger.status()[0]['state']=='done'
    assert ledger.claim('extra') is None
    with pytest.raises(ValueError):
        ledger.release(old)
