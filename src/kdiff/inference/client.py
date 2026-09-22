"""Explicit provider factory with capability gates and no provider fallback."""

import os
import json
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.openai import OpenAIChatCompletionClient
from autogen_ext.models.replay import ReplayChatCompletionClient
from pydantic import Field, model_validator

from kdiff.core.contracts import Contract, canonical, digest


class Provider(Contract):
    profile: Literal["mock", "local-dev", "openai"]
    model: str
    base_url: str | None = None
    model_info: dict | None = None
    max_output_tokens: int = Field(default=1024, ge=128, le=4096)
    timeout_seconds: int = Field(default=30, ge=1, le=120)
    max_model_calls: int = Field(default=14, ge=1, le=16)
    capability_report: str | None = None
    execution_authorized: bool = False
    max_total_tokens: int = Field(default=100000, ge=1000, le=1000000)
    context_window: int = Field(default=16384, ge=1024, le=262144)
    max_cost_usd: Decimal = Field(default=Decimal('0'), ge=0, le=100)
    input_usd_per_million: Decimal | None = Field(default=None, gt=0)
    output_usd_per_million: Decimal | None = Field(default=None, gt=0)
    price_date: date | None = None
    runtime_revision: str | None = None
    token_parameter: Literal['max_tokens','max_completion_tokens'] = 'max_completion_tokens'

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
                   allow_mock: bool = False, budget=None):
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
              "max_retries": 0, profile.token_parameter: profile.max_output_tokens,
              "parallel_tool_calls": False}
    if profile.profile == "local-dev":
        kwargs.update(base_url=profile.base_url, api_key=os.environ.get("VLLM_API_KEY", "local-no-key"),
                      model_info=profile.model_info)
    else:
        if not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY is not configured")
        kwargs.update(api_key=os.environ['OPENAI_API_KEY'], base_url='https://api.openai.com/v1')
        if profile.model_info:
            kwargs['model_info']=profile.model_info
    client=OpenAIChatCompletionClient(**kwargs)
    if budget is not None:
        from kdiff.inference.limits import BoundedClient
        return BoundedClient(client,budget)
    return client


def require_execution_support(profile: Provider):
    if profile.profile == 'mock':
        return
    validate_authorization(profile)
    if not profile.capability_report:
        raise ValueError('Run provider-check and configure its capability report before live execution')
    report=json.loads(Path(profile.capability_report).read_text())
    required={'completion','tool_round_trip','structured_output','token_accounting'}
    if (report.get('status') != 'passed' or report.get('profile_fingerprint')!=profile_fingerprint(profile)
            or not required.issubset({k for k,v in report.get('checks',{}).items() if v is True})):
        raise ValueError('Provider capability report does not match the selected runtime')
    checked=datetime.fromisoformat(report['checked_at'])
    if not checked.tzinfo or not datetime.now(timezone.utc)-timedelta(days=7)<=checked<=datetime.now(timezone.utc):
        raise ValueError('Provider capability report is expired or has an invalid timestamp')


def profile_fingerprint(profile):
    return digest({k:getattr(profile,k) for k in ('profile','model','base_url','model_info','runtime_revision','token_parameter','context_window')})


def validate_authorization(profile):
    if not profile.execution_authorized:
        raise ValueError('Live execution requires explicit authorization in the selected profile')
    if profile.profile=='openai' and (not profile.max_cost_usd or not profile.input_usd_per_million
            or not profile.output_usd_per_million or not profile.price_date):
        raise ValueError('OpenAI execution requires a cost budget and dated price configuration')
    if profile.profile=='local-dev' and not profile.runtime_revision:
        raise ValueError('Local execution requires the verified serving runtime revision')
