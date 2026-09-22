"""Actual AssistantAgent turns, role-local tools, validated results and receipts."""

import asyncio
import inspect
from functools import wraps
from pathlib import Path
from uuid import uuid4

from autogen_agentchat.agents import AssistantAgent
from autogen_core.tools import FunctionTool

from kdiff.core.contracts import ToolReceipt, digest, now
from kdiff.inference.client import Provider, client_factory, require_execution_support

CONSTRUCTION_ROLES = ("Orchestrator", "Ingestion", "Extraction", "Disambiguation", "Integration")
ANALYSIS_ROLES = ("Manager", "Planner", "Retriever", "Drafting_Bridge", "Verifier", "Evidence_Builder", "Synthesizer")


class AgentRun:
    def __init__(self, store, profile: Provider, allow_mock: bool, seed_id: str):
        require_execution_support(profile)
        self.store, self.profile, self.allow_mock = store, profile, allow_mock
        self.run_id, self.seed_id = str(uuid4()), seed_id
        self.receipts, self.events, self.agents = [], [], []
        self.calls = 0

    async def turn(self, role, function, arguments, sources=()):
        self.calls += 1
        if self.calls > self.profile.max_model_calls:
            raise ValueError("Model call budget exhausted")
        family = "construction" if role in CONSTRUCTION_ROLES else "analysis"
        prompt = (Path(__file__).parent / "prompts" / family / f"{role}.txt").read_text()
        prompt_hash = digest(prompt)
        outputs = []

        @wraps(function)
        async def guarded(*args, **kwargs):
            if outputs:
                raise ValueError("Only one successful tool call per role in M1")
            started = now()
            task_id = digest([role, function.__name__, kwargs, sources, prompt_hash,
                              self.profile.model, "kdiff-0.1"])
            try:
                result = function(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                result_hash = self.store.put(result)
                outputs.append(result)
                receipt = ToolReceipt(run_id=self.run_id, seed_id=self.seed_id, instance_id="single-host-m1",
                    stage=role, task_id=task_id, tool=function.__name__, arguments=kwargs,
                    result_hash=result_hash, status="success", source_artifact_ids=list(sources),
                    model=self.profile.model, prompt_hash=prompt_hash, started_at=started, finished_at=now())
                self.receipts.append(receipt.model_dump(mode="json"))
                return {"receipt": result_hash, "result": result}
            except Exception as exc:
                receipt = ToolReceipt(run_id=self.run_id, seed_id=self.seed_id, instance_id="single-host-m1",
                    stage=role, task_id=task_id, tool=function.__name__, arguments=kwargs,
                    status="error", error_type=type(exc).__name__, source_artifact_ids=list(sources),
                    model=self.profile.model, prompt_hash=prompt_hash, started_at=started, finished_at=now())
                self.receipts.append(receipt.model_dump(mode="json"))
                raise

        tool = FunctionTool(guarded, description=function.__doc__ or function.__name__)
        client = client_factory(self.profile, mock_call=(function.__name__, arguments), allow_mock=self.allow_mock)
        agent = AssistantAgent(role, client, tools=[tool], system_message=prompt,
                               max_tool_iterations=1, reflect_on_tool_use=False,
                               metadata={"run_id": self.run_id, "seed_id": self.seed_id, "stage": role,
                                         "provider": self.profile.profile})
        self.agents.append(agent)
        try:
            result = await asyncio.wait_for(agent.run(task="Execute your scoped tool using this typed input: " +
                                                      __import__("json").dumps(arguments)),
                                            timeout=self.profile.timeout_seconds)
            for message in result.messages:
                self.events.append({"role": role, "provider": self.profile.profile,
                                    "event": message.model_dump(mode="json")})
            usage = client.total_usage()
            self.events.append({"role": role, "event": {"type": "ModelUsage", "model": self.profile.model,
                               "prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens,
                               "synthetic_accounting": self.profile.profile == "mock"}})
            if len(outputs) != 1:
                raise ValueError(f"{role} did not produce one validated tool result")
            return outputs[0]
        finally:
            await client.close()

    def save(self, outcome):
        record = {"run_id": self.run_id, "seed_id": self.seed_id, "provider": self.profile.profile,
                  "model": self.profile.model, "synthetic_model": self.profile.profile == "mock",
                  "receipts": self.receipts, "events": self.events, "outcome": outcome}
        return self.store.put(record)
