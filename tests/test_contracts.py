import json
from datetime import date

import pytest
from pydantic import ValidationError

from kdiff.analysis.program import CountProgram, count_program
from kdiff.analysis.witness import InsufficientEvidence, count_from_release
from kdiff.construction.openalex import capture_local, parse_capture, validate_batch
from kdiff.core.contracts import Entity, Label, TimeRange, Window, membership, stable_id
from kdiff.core.schema import FIELDS, RELATIONS, validate_relation
from kdiff.inference.client import Provider, client_factory, require_execution_support


def test_registry_preserves_paper_directions_and_optional_fields():
    assert set(FIELDS) == {"Author", "Paper", "Institution", "Topic", "Patent", "Grant"}
    assert "publicationDate" in FIELDS["Patent"]
    assert "gender" in FIELDS["Author"]
    assert "citedByPatent" in RELATIONS and "citesPatent" in RELATIONS
    assert {x.value for x in Label} == {"Supported", "Partial", "Unsupported", "Contradicted", "Ambiguous", "NEI"}
    inst = Entity(canonical_id="i", namespace="fixture:m1", kind="Institution")
    with pytest.raises(ValueError, match="site subtype"):
        validate_relation("hasSite", inst, inst, {})
    site = Entity(canonical_id="s", namespace="fixture:m1", kind="Institution", subtype="site")
    validate_relation("announcedSite", inst, site, {})
    with pytest.raises(ValueError, match="endpoint"):
        validate_relation("authorOf", inst, site, {})


@pytest.mark.parametrize("raw,lower,upper", [("2020", "2020-01-01", "2020-12-31"),
    ("2020-02", "2020-02-01", "2020-02-29"), ("2020-02-03", "2020-02-03", "2020-02-03")])
def test_precision(raw, lower, upper):
    value = TimeRange.parse(raw)
    assert str(value.lower) == lower and str(value.upper) == upper
    assert value.raw == raw


def test_unknown_ongoing_and_fuzzy_are_distinct():
    assert TimeRange.parse("circa 2010").lower is None
    assert TimeRange.parse(None).end_kind == "unknown"
    ongoing = TimeRange(lower=date(2020, 1, 1), end_kind="ongoing", precision="interval")
    assert ongoing.upper is None and ongoing.end_kind == "ongoing"
    with pytest.raises(ValidationError):
        TimeRange(end_kind="ongoing")
    with pytest.raises(ValidationError):
        TimeRange(lower=date(2021, 1, 1), upper=date(2020, 1, 1))


def test_calendar_windows_and_partial_overlap():
    window = Window.last_decade(date(2025, 1, 1))
    assert str(window.start) == "2015-01-01" and str(window.end) == "2024-12-31"
    assert Window.last_decade(date(2026, 9, 22)).end.year == 2025
    partial = Window(start="2020-06-01", end="2020-08-31", reference_date="2025-01-01")
    assert membership(TimeRange.parse(2020), partial) == "indeterminate"
    assert membership(TimeRange.parse(None), partial) == "indeterminate"


def test_parsing_preserves_namesakes_revisions_and_authorship_context(batch):
    validate_batch(batch)
    authors = [e for e in batch.entities if e.kind == "Author"]
    assert len(authors) == 2
    assert len([o for o in batch.observations if o.kind == "Paper"]) == 5
    assert all(a.observation_kind == "publication" for a in batch.assertions)
    assert all(a.qualifiers["kind"] == "bibliometric_observation" for a in batch.assertions if a.relation == "affiliatedWith")
    assert all(a.provenance.source_path.startswith("$.") for a in batch.assertions)
    assert all(not i.person_identity_verified for i in batch.identities)


def test_count_distinct_objects_with_duplicate_assertions(batch, request_count):
    release = {"graph": batch.model_dump(mode="json")}
    result = count_from_release(release, request_count)
    assert result["count"] == 2
    assert len(result["input_assertion_ids"]) == 4  # Includes excluded and duplicate-version operands.


def test_unknown_and_conflicting_dates_do_not_become_zero(batch, request_count):
    release = {"graph": batch.model_dump(mode="json")}
    selected = next(a for a in release["graph"]["assertions"] if a["relation"] == "authorOf" and a["head_id"] == request_count.author_id)
    selected["valid_time"] = TimeRange.parse(None).model_dump(mode="json")
    with pytest.raises(InsufficientEvidence, match="Unknown"):
        count_from_release(release, request_count)
    selected["valid_time"] = TimeRange.parse(2018).model_dump(mode="json")
    alternative = {**selected, "assertion_id": "different", "valid_time": TimeRange.parse(2025).model_dump(mode="json")}
    release["graph"]["assertions"].append(alternative)
    with pytest.raises(InsufficientEvidence, match="conflicting"):
        count_from_release(release, request_count)


def test_missing_identity_and_endpoint_fail(batch, request_count):
    release = {"graph": batch.model_dump(mode="json")}
    bad = request_count.model_copy(update={"author_id": "unknown"})
    with pytest.raises(InsufficientEvidence, match="not a zero"):
        count_from_release(release, bad)
    release["graph"]["entities"] = [e for e in release["graph"]["entities"] if e["kind"] != "Paper"]
    with pytest.raises(InsufficientEvidence, match="endpoint"):
        count_from_release(release, request_count)


def test_capture_reuse_and_selective_source_changes(tmp_path, store):
    records = [{"id": f"fixture:openalex:W{i}", "authorships": []} for i in [1, 2]]
    p = tmp_path / "records.jsonl"
    p.write_text("\n".join(map(json.dumps, records)))
    first = capture_local(p, store, "fixture:m1")
    assert first == capture_local(p, store, "fixture:m1")
    records[0]["publication_year"] = 2020
    p.write_text("\n".join(map(json.dumps, records)))
    second = capture_local(p, store, "fixture:m1")
    assert first["records"][0]["sha256"] != second["records"][0]["sha256"]
    assert first["records"][1] == second["records"][1]


def test_reject_missing_endpoint_and_unmarked_fixture(tmp_path, store):
    p = tmp_path / "records.jsonl"
    p.write_text(json.dumps({"id": "fixture:openalex:W1", "authorships": [{"author": {"display_name": "Missing"}}]}))
    with pytest.raises(ValueError, match="Missing endpoint"):
        parse_capture(capture_local(p, store, "fixture:m1"), store)
    with pytest.raises(ValueError, match="namespace mismatch"):
        parse_capture(capture_local(p, store, "local:real"), store)


def test_no_truncated_success(tmp_path, store, monkeypatch):
    import kdiff.construction.openalex as adapter
    monkeypatch.setattr(adapter, "MAX_RECORDS", 1)
    p = tmp_path / "two.jsonl"
    p.write_text('{"id":"fixture:openalex:W1"}\n{"id":"fixture:openalex:W2"}\n')
    with pytest.raises(ValueError, match="no truncated"):
        capture_local(p, store, "fixture:m1")


@pytest.mark.parametrize("mutation", ["unknown_operator", "cycle", "missing_window", "extra_cypher"])
def test_invalid_plans_rejected(request_count, mutation):
    plan = count_program(request_count, "a" * 64).model_dump(mode="json")
    if mutation == "unknown_operator":
        plan["steps"][2]["operator"] = "ExecuteCypher"
    elif mutation == "cycle":
        plan["steps"][0]["depends_on"] = ["count"]
    elif mutation == "missing_window":
        del plan["request"]["window"]
    else:
        plan["cypher"] = "MATCH (n) DETACH DELETE n"
    with pytest.raises(ValidationError):
        CountProgram.model_validate(plan)


def test_provider_never_falls_back_or_silently_mocks(monkeypatch):
    profile = Provider(profile="mock", model="scripted-fixture-v1")
    with pytest.raises(ValueError, match="opt-in"):
        client_factory(profile, mock_call=("nothing", {}))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    profile = Provider(profile="openai", model="test-no-execution")
    with pytest.raises(ValueError, match="not configured"):
        client_factory(profile)
    with pytest.raises(NotImplementedError, match="blocked"):
        require_execution_support(profile)
