from pathlib import Path

import pytest

from kdiff.core.artifacts import ArtifactStore
from kdiff.construction.openalex import capture_local, parse_capture
from kdiff.core.contracts import CountRequest, Window, stable_id

FIXTURE = Path(__file__).parent / "fixtures/m1_openalex.jsonl"


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def batch(store):
    return parse_capture(capture_local(FIXTURE, store, "fixture:m1"), store)


@pytest.fixture
def request_count():
    return CountRequest(author_id=stable_id("fixture:m1", "Author", "fixture:openalex:A1"),
                        window=Window(start="2016-01-01", end="2020-12-31", reference_date="2025-01-01"))
