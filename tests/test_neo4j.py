import asyncio
import copy
import json
import os
import socket
from pathlib import Path

import pytest

from kdiff.agents import ANALYSIS_ROLES, CONSTRUCTION_ROLES
from kdiff.analysis.witness import freeze, replay
from kdiff.construction.openalex import capture_local, parse_capture
from kdiff.core.artifacts import exclusive_lock
from kdiff.core.contracts import CountRequest, Window, stable_id
from kdiff.core.graph import Graph
from kdiff.inference.client import Provider
from kdiff.workflows import ask, build

FIXTURE = Path(__file__).parent / "fixtures/m1_openalex.jsonl"
PROFILE = Provider(profile="mock", model="scripted-fixture-v1")
pytestmark = pytest.mark.neo4j


@pytest.fixture
def graph():
    manifest = os.environ.get("KDIFF_SERVICE")
    if not manifest:
        pytest.skip("Real isolated Neo4j not configured; unit tests cannot pass M1")
    graph = Graph(Path(manifest))
    graph.verify_owner()
    with exclusive_lock(Path(graph.meta["root"]) / "application.lock"):
        yield graph
    graph.close()


def query(namespace):
    return CountRequest(author_id=stable_id(namespace, "Author", "fixture:openalex:A1"),
                        window=Window(start="2016-01-01", end="2020-12-31", reference_date="2025-01-01"))


def construct(graph, store, namespace):
    return asyncio.run(build(graph, store, PROFILE, True, FIXTURE, namespace))


def answer(graph, store, release, namespace):
    return asyncio.run(ask(graph, store, PROFILE, True, release, query(namespace)))


def test_real_autogen_neo4j_idempotency_and_offline_replay(graph, store, monkeypatch):
    namespace = "fixture:integration"
    first = construct(graph, store, namespace)
    second = construct(graph, store, namespace)
    assert first["counts"] == second["counts"]
    assert first["reused"] is False and second["reused"] is True
    release_id = freeze(graph, store, namespace, "integration-test")
    result = answer(graph, store, release_id, namespace)
    assert result["type"] == "Answer", store.get(result["run"])
    expected = set()
    for line in FIXTURE.read_text().splitlines():
        record = json.loads(line)
        if 2016 <= record["publication_year"] <= 2020 and any(a["author"]["id"] == "fixture:openalex:A1" for a in record["authorships"]):
            expected.add(record["id"])
    witness_id = result["witnesses"][0]
    witness = store.get(witness_id)
    assert witness["result"]["count"] == len(expected)
    for run_id, roles in [(first["run"], CONSTRUCTION_ROLES), (result["run"], ANALYSIS_ROLES)]:
        run = store.get(run_id)
        assert [r["stage"] for r in run["receipts"]] == list(roles)
        assert all(r["status"] == "success" for r in run["receipts"])
        calls = [e for e in run["events"] if e["event"]["type"] == "ToolCallRequestEvent"]
        assert [e["role"] for e in calls] == list(roles)
    def forbidden(*a, **kw):
        raise AssertionError("Replay attempted network access")
    monkeypatch.setattr(socket, "socket", forbidden)
    assert replay(store, witness_id)["count"] == len(expected)


def test_new_record_changes_count_and_old_release_replays(graph, store, tmp_path):
    namespace = "fixture:mutation"
    construct(graph, store, namespace)
    old_release = freeze(graph, store, namespace, "before")
    old_answer = answer(graph, store, old_release, namespace)
    record = json.loads(FIXTURE.read_text().splitlines()[0])
    record["id"] = "fixture:openalex:W5"
    record["publication_year"] = 2019
    variant = tmp_path / "new.jsonl"
    variant.write_text(json.dumps(record))
    asyncio.run(build(graph, store, PROFILE, True, variant, namespace))
    with pytest.raises(ValueError, match="Working graph changed"):
        answer(graph, store, old_release, namespace)
    new_release = freeze(graph, store, namespace, "after")
    new_answer = answer(graph, store, new_release, namespace)
    assert replay(store, old_answer["witnesses"][0])["count"] == 2
    assert replay(store, new_answer["witnesses"][0])["count"] == 3


def test_missing_and_tampered_operands_fail_replay(graph, store):
    namespace = "fixture:tamper"
    construct(graph, store, namespace)
    release = freeze(graph, store, namespace, "test")
    result = answer(graph, store, release, namespace)
    witness_id = result["witnesses"][0]
    witness = store.get(witness_id)
    bad = copy.deepcopy(witness)
    bad["result"]["input_assertion_ids"].pop()
    with pytest.raises(ValueError, match="operand set"):
        replay(store, store.put(bad))
    bad = copy.deepcopy(witness)
    bad["claim"]["value"] += 1
    with pytest.raises(ValueError, match="Claim is not supported"):
        replay(store, store.put(bad))
    bad = copy.deepcopy(witness)
    bad["claim"]["predicate"] = "caused_output_growth"
    with pytest.raises(ValueError, match="Claim is not supported"):
        replay(store, store.put(bad))
    bad = copy.deepcopy(witness)
    bad["proof_obligations"].pop()
    with pytest.raises(ValueError, match="proof obligations"):
        replay(store, store.put(bad))
    source_sha = store.get(release)["graph"]["sources"][0]["sha256"]
    store.path(source_sha).write_bytes(b"altered source")
    with pytest.raises(ValueError, match="integrity"):
        replay(store, witness_id)


def test_conflicting_source_dates_abstain_with_no_zero(graph, store, tmp_path):
    namespace = "fixture:conflict"
    construct(graph, store, namespace)
    record = json.loads(FIXTURE.read_text().splitlines()[0])
    record["publication_year"] = 2025
    path = tmp_path / "conflict.jsonl"
    path.write_text(json.dumps(record))
    asyncio.run(build(graph, store, PROFILE, True, path, namespace))
    release = freeze(graph, store, namespace, "conflict")
    result = answer(graph, store, release, namespace)
    assert result["type"] == "Abstain" and result["witnesses"] == []
    assert "0 distinct papers" not in json.dumps(result)
    errors = [r for r in store.get(result["run"])["receipts"] if r["status"] == "error"]
    assert errors[0]["error_type"] == "InsufficientEvidence"


def test_failed_query_cannot_issue_witness(graph, store, monkeypatch):
    namespace = "fixture:timeout"
    construct(graph, store, namespace)
    release = freeze(graph, store, namespace, "timeout")
    def timeout(*args):
        raise TimeoutError("Query did not complete")
    monkeypatch.setattr(graph, "count_papers", timeout)
    result = answer(graph, store, release, namespace)
    assert result["type"] == "Abstain" and not result["witnesses"]


def test_graph_property_drift_is_detected(graph, store):
    namespace = "fixture:drift"
    construct(graph, store, namespace)
    release = freeze(graph, store, namespace, "before-drift")
    with graph.driver.session(database="neo4j") as s:
        s.run("MATCH ()-[r:authorOf]->() WHERE r.namespace=$namespace SET r.lower='2099-01-01'",
              namespace=namespace).consume()
    with pytest.raises(ValueError, match="Graph fact"):
        answer(graph, store, release, namespace)


def test_institute_seven_roles_real_graph_and_offline_witnesses(graph, store, monkeypatch):
    from kdiff.analysis.fixtures import institute_fixture
    from kdiff.analysis.requests import AnalysisRequest
    from kdiff.analysis.workflow import analyze
    batch, ids = institute_fixture(store, namespace='fixture:institute-integration')
    graph.integrate(batch)
    release = freeze(graph, store, batch.namespace, 'institute-integration')
    request = AnalysisRequest(family='comparison', kind='Institution', identifier=ids['institution'], topic_id=ids['topic'],
        before=Window(start='2016-01-01', end='2018-12-31', reference_date='2025-01-01'),
        window=Window(start='2020-01-01', end='2022-12-31', reference_date='2025-01-01'), ask_causation=True)
    result = asyncio.run(analyze(graph, store, PROFILE, True, release, request))
    assert result['type'] == 'Answer', store.get(result['run'])
    assert len(result['witnesses']) == 3
    assert '2.21' in result['text']
    assert result['refusals'][0]['label'] == 'NEI'
    run = store.get(result['run'])
    assert [r['stage'] for r in run['receipts']] == list(ANALYSIS_ROLES)
    with monkeypatch.context() as context:
        context.setattr(socket, 'socket', lambda *a, **k: (_ for _ in ()).throw(AssertionError('network replay')))
        for wid in result['witnesses']:
            assert replay(store, wid)['status'] == 'verified'
    ambiguous = request.model_copy(update={'family':'count','kind':__import__('kdiff.core.schema',fromlist=['EntityType']).EntityType.AUTHOR,'identifier':None,'name':'A. Chen','ask_causation':False})
    clarified = asyncio.run(analyze(graph, store, PROFILE, True, release, ambiguous))
    assert clarified['type'] == 'Clarify'
    assert len(clarified['candidates']) == 2
    assert store.get(clarified['run'])['receipts'][0]['stage'] == 'Manager'
    sites = request.model_copy(update={'family':'sites','before':None,'ask_causation':False,
        'window':Window(start='2015-01-01',end='2024-12-31',reference_date='2025-01-01')})
    answer = asyncio.run(analyze(graph, store, PROFILE, True, release, sites))
    assert answer['type'] == 'Answer', store.get(answer['run'])
    assert answer['claims'][0]['final']['value'] == 3
    assert len(answer['claims'][1]['final']['value']['ids']) == 1


def test_orcid_ror_adapters_through_five_autogen_roles(graph, store, tmp_path):
    from kdiff.core.durable import TaskLedger
    namespace = 'fixture:source-integration'
    record = {'id':'https://ror.org/fixture-institute', 'names':[{'value':'Fixture Institute','types':['ror_display']}],
              'relationships':[]}
    path=tmp_path/'ror.json'
    path.write_text(json.dumps(record))
    first=asyncio.run(build(graph,store,PROFILE,True,path,namespace,source='ror',input_format='json'))
    second=asyncio.run(build(graph,store,PROFILE,True,path,namespace,source='ror',input_format='json'))
    assert first['counts']==second['counts']
    assert second['reused']
    assert [r['stage'] for r in store.get(first['run'])['receipts']]==list(CONSTRUCTION_ROLES)
    ledger = TaskLedger(tmp_path / 'source-cache.sqlite', initialize=True)
    cold = asyncio.run(build(graph,store,PROFILE,True,path,namespace,source='ror',input_format='json',ledger=ledger,use_cache=True))
    warm = asyncio.run(build(graph,store,PROFILE,True,path,namespace,source='ror',input_format='json',ledger=ledger,use_cache=True))
    assert first['cache'] is None
    assert cold['cache']['miss'] == 1 and warm['cache']['durable'] == 1
    assert warm['counts'] == cold['counts'] == first['counts']


def test_real_web_extraction_and_program_replay(graph, store):
    from kdiff.construction.web import capture_document
    from kdiff.construction.web_workflow import build_document
    from kdiff.analysis.requests import AnalysisRequest
    from kdiff.analysis.workflow import analyze
    text = 'Synthetic Ada joined Example University in 2019.'
    document = capture_document(text.encode(), 'text/plain', 'https://example.org/fixture-cv', store)
    extraction = {'entities': [
        {'key': 'ada', 'kind': 'Author', 'name': 'Synthetic Ada', 'start': 0, 'end': 13},
        {'key': 'org', 'kind': 'Institution', 'name': 'Example University', 'start': 21, 'end': 39}],
        'facts': [{'head': 'ada', 'relation': 'affiliatedWith', 'tail': 'org', 'start': 0,
                   'end': len(text), 'quote': text, 'date_text': '2019', 'observation_kind': 'employment'}]}
    result = asyncio.run(build_document(graph, store, PROFILE, True, store.put(document), 'fixture:web-agent', extraction))
    assert [r['stage'] for r in store.get(result['run'])['receipts']] == list(CONSTRUCTION_ROLES)
    release_id = freeze(graph, store, 'fixture:web-agent', 'web-test')
    request = AnalysisRequest(family='identity', kind='Author', name='Synthetic Ada',
        window=Window(start='2018-01-01', end='2020-12-31', reference_date='2025-01-01'))
    answer = asyncio.run(analyze(graph, store, PROFILE, True, release_id, request))
    assert answer['type'] == 'Answer', store.get(answer['run'])
    assert replay(store, answer['witnesses'][0])['status'] == 'verified'


@pytest.mark.parametrize('workers', [1, 2, 4, 8])
def test_coordinated_worker_graph_convergence(store, tmp_path, workers):
    import concurrent.futures
    import time
    from kdiff.construction.worker import enqueue_builds, work
    from kdiff.core.durable import TaskLedger
    manifest = os.environ.get('KDIFF_SERVICE')
    if not manifest:
        pytest.skip('Real owned Neo4j required for worker convergence')
    ledger = TaskLedger(tmp_path / 'workers.sqlite', initialize=True)
    namespace = f'fixture:workers-{workers}'
    base = json.loads(FIXTURE.read_text().splitlines()[0])
    entries = []
    for index in range(8):
        record = {**base, 'id': f'fixture:openalex:W{index}', 'publication_year': 2018}
        path = tmp_path / f'input-{index}.jsonl'
        path.write_text(json.dumps(record))
        entries.append({'input': str(path), 'namespace': namespace})
    queued = enqueue_builds(entries + entries, store, ledger, PROFILE)
    assert len(set(queued['tasks'])) == 8
    started = time.monotonic()
    def worker(index):
        own = Graph(Path(manifest))
        try:
            while True:
                report = asyncio.run(work(own, store, ledger, PROFILE, allow_mock=True))
                if not report['tasks']:
                    return
        finally:
            own.close()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, range(workers)))
    graph = Graph(Path(manifest))
    try:
        exported = graph.export(namespace)
        assert all(task['state'] == 'done' for task in ledger.status())
        authorships = [a for a in exported['assertions'] if a['relation'] == 'authorOf']
        assert len(authorships) == 8
        assert len({a['tail_id'] for a in authorships}) == 8
        assert len({a['head_id'] for a in authorships}) == 1
        elapsed = time.monotonic() - started
        from kdiff.deployment.manifest import atomic_json
        folder = Path('artifacts/scaling')
        folder.mkdir(parents=True, exist_ok=True)
        atomic_json(folder / f'workers-{workers}.json', {'synthetic': True, 'mock_inference': True,
            'workers': workers, 'input_records': 8, 'duplicate_submissions': 8, 'wall_seconds': elapsed,
            'aggregate_records_per_second': 8 / elapsed, 'per_worker_records_per_second': 8 / elapsed / workers,
            'graph_entities': len(exported['entities']), 'graph_assertions': len(exported['assertions']),
            'gpu_hours': None, 'policy': 'coordinated writer, no parallel write speedup claimed'})
    finally:
        graph.close()


def test_snapshot_waits_for_server_side_writer_after_client_lock_release(graph, store):
    import concurrent.futures
    from kdiff.core.contracts import digest
    namespace = 'fixture:snapshot-coordination'
    construct(graph, store, namespace)
    before = digest(graph.export(namespace))
    with graph.driver.session(database='neo4j') as session:
        transaction = session.begin_transaction()
        graph._coordinate(transaction)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(graph.export, namespace)
            try:
                with pytest.raises(concurrent.futures.TimeoutError):
                    future.result(timeout=.25)
            finally:
                transaction.commit()
            assert digest(future.result(timeout=10)) == before
