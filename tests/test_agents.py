import asyncio

import pytest

from kdiff.agents import AgentRun
from kdiff.core.contracts import CountRequest
from kdiff.inference.client import Provider


def test_actual_autogen_tool_validates_nested_model_and_records_receipt(store, request_count):
    run = AgentRun(store, Provider(profile="mock", model="scripted-fixture-v1"), True, "fixture:agent")
    def plan_count(count_request: CountRequest) -> dict:
        """Check nested typed request round trip."""
        assert isinstance(count_request, CountRequest)
        return count_request.model_dump(mode="json")
    result = asyncio.run(run.turn("Planner", plan_count, {"count_request": request_count.model_dump(mode="json")}))
    assert result == request_count.model_dump(mode="json")
    assert len(run.receipts) == 1
    assert run.receipts[0]["arguments"]["count_request"] == result
    assert any(e["event"]["type"] == "ToolCallExecutionEvent" for e in run.events)


def test_actual_autogen_failure_receipt_is_not_a_success(store):
    run = AgentRun(store, Provider(profile="mock", model="scripted-fixture-v1"), True, "fixture:agent")
    def retrieve_count(program_id: str) -> dict:
        """Simulate a failed deterministic query without an invented empty result."""
        raise TimeoutError("Query timeout")
    with pytest.raises(ValueError, match="validated tool result"):
        asyncio.run(run.turn("Retriever", retrieve_count, {"program_id": "fixture"}))
    assert run.receipts[0]["error_type"] == "TimeoutError"
    assert run.receipts[0]["status"] == "error"
    assert run.receipts[0]["result_hash"] is None
