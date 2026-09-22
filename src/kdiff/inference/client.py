"""Explicit provider factory. No fallback and no paid execution in M1."""

import os
from typing import Literal
from urllib.parse import urlsplit

from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.openai import OpenAIChatCompletionClient
from autogen_ext.models.replay import ReplayChatCompletionClient
from pydantic import Field, model_validator

from kdiff.core.contracts import Contract, canonical


class Provider(Contract):
    profile: Literal["mock", "local-dev", "openai"]
    model: str
    base_url: str | None = None
    model_info: dict | None = None
    max_output_tokens: int = Field(default=1024, ge=128, le=4096)
    timeout_seconds: int = Field(default=30, ge=1, le=120)
    max_model_calls: int = Field(default=14, ge=1, le=16)
    capability_report: str | None = None

    @model_validator(mode="after")
    def valid(self):
        if self.profile == "local-dev":
            if not self.base_url or not self.model_info:
                raise ValueError("Local provider needs explicit endpoint and verified model capabilities")
            parsed = urlsplit(self.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query:
                raise ValueError("Invalid provider URL")
        if self.profile == "openai" and self.base_url is not None:
            raise ValueError("OpenAI profile uses the SDK endpoint; local endpoints require local-dev")
        if self.profile == "mock" and self.model != "scripted-fixture-v1":
            raise ValueError("Mock model must be explicitly labeled scripted-fixture-v1")
        return self


def client_factory(profile: Provider, *, mock_call: tuple[str, dict] | None = None,
                   allow_mock: bool = False):
    if profile.profile == "mock":
        if not allow_mock or mock_call is None:
            raise ValueError("Mock inference requires explicit --allow-mock fixture opt-in")
        name, args = mock_call
        return ReplayChatCompletionClient([
            CreateResult(finish_reason="function_calls",
                         content=[FunctionCall(id="fixture-call", name=name, arguments=canonical(args).decode())],
                         usage=RequestUsage(prompt_tokens=0, completion_tokens=0), cached=False)
        ], model_info={"vision": False, "function_calling": True, "json_output": True,
                       "family": "unknown", "structured_output": False})
    # Build configurations without making network calls. Execution is gated separately.
    kwargs = {"model": profile.model, "timeout": profile.timeout_seconds,
              "max_retries": 0, "max_tokens": profile.max_output_tokens,
              "parallel_tool_calls": False}
    if profile.profile == "local-dev":
        kwargs.update(base_url=profile.base_url, api_key=os.environ.get("VLLM_API_KEY", "local-no-key"),
                      model_info=profile.model_info)
    else:
        if not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY is not configured")
    return OpenAIChatCompletionClient(**kwargs)


def require_execution_support(profile: Provider):
    if profile.profile != "mock":
        raise NotImplementedError("Live inference execution is blocked until M5 capability and budget enforcement tests; factory only in M1")
