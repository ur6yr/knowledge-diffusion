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
