"""Per-run admission limits shared by all AutoGen clients in a workflow."""

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal

from autogen_core.models import ChatCompletionClient
from kdiff.core.contracts import canonical

ACTIVE_BUDGET = ContextVar('kdiff_run_budget', default=None)


def current_budget(profile):
    active = ACTIVE_BUDGET.get()
    if active is not None:
        if active.profile != profile:
            raise ValueError('A shared budget cannot silently switch provider profiles')
        return active
    return RunBudget(profile)


@contextmanager
def budget_scope(profile):
    budget = RunBudget(profile)
    token = ACTIVE_BUDGET.set(budget)
    try:
        yield budget
    finally:
        ACTIVE_BUDGET.reset(token)


class RunBudget:
    def __init__(self, profile):
        self.profile=profile
        self.calls=0
        self.reserved_tokens=0
        self.actual_tokens=0
        self.reserved_usd=Decimal(0)
        self.actual_usd=Decimal(0)
        self.events=[]

    def reserve(self,messages,tools):
        serialized=[m.model_dump(mode='json') for m in messages]
        schemas=[t.schema if hasattr(t,'schema') else t for t in tools]
        # Conservative text-only upper estimate, not a claimed exact tokenizer.
        prompt_bound=len(canonical([serialized,schemas]))+1024
        output=self.profile.max_output_tokens
        if prompt_bound+output>self.profile.context_window:
            raise ValueError('Request exceeds configured context admission bound')
        if self.calls>=self.profile.max_model_calls or self.reserved_tokens+prompt_bound+output>self.profile.max_total_tokens:
            raise ValueError('Run request/token budget exhausted')
        cost=Decimal(0)
        if self.profile.profile=='openai':
            cost=(Decimal(prompt_bound)*self.profile.input_usd_per_million+Decimal(output)*self.profile.output_usd_per_million)/1000000
            if self.reserved_usd+cost>self.profile.max_cost_usd:
                raise ValueError('Run cost admission budget exhausted')
        self.calls+=1
        self.reserved_tokens+=prompt_bound+output
        self.reserved_usd+=cost
        ticket={'call':self.calls,'input_upper_estimate':prompt_bound,'output_cap':output,
                'reserved_usd':str(cost) if self.profile.profile=='openai' else None,'status':'reserved'}
        self.events.append(ticket)
        return ticket

    def finish(self,ticket,result):
        usage=result.usage
        if usage.prompt_tokens<0 or usage.completion_tokens<0 or usage.prompt_tokens+usage.completion_tokens==0:
            ticket['status']='missing_usage'
            raise ValueError('Live provider did not report usable token accounting')
        self.actual_tokens+=usage.prompt_tokens+usage.completion_tokens
        if self.profile.profile=='openai':
            self.actual_usd+=(Decimal(usage.prompt_tokens)*self.profile.input_usd_per_million+
                              Decimal(usage.completion_tokens)*self.profile.output_usd_per_million)/1000000
        ticket.update(status='completed',prompt_tokens=usage.prompt_tokens,completion_tokens=usage.completion_tokens)
        if usage.prompt_tokens>ticket['input_upper_estimate'] or usage.completion_tokens>ticket['output_cap']:
            self.calls=self.profile.max_model_calls
            raise ValueError('Provider exceeded admission assumptions, further calls disabled')

    def report(self):
        unknown = any(e['status'] != 'completed' for e in self.events)
        return {'calls':self.calls,'reserved_tokens':self.reserved_tokens,'actual_tokens':self.actual_tokens,
                'estimated_usd':str(self.actual_usd) if self.profile.profile=='openai' and not unknown else None,
                'usage_incomplete': unknown, 'reserved_usd': str(self.reserved_usd) if self.profile.profile == 'openai' else None,
                'price_date':str(self.profile.price_date) if self.profile.price_date else None,'events':self.events}


class BoundedClient(ChatCompletionClient):
    def __init__(self,inner,budget):
        self.inner,self.budget=inner,budget

    @property
    def model_info(self):
        return self.inner.model_info

    @property
    def capabilities(self):
        return self.inner.capabilities

    def count_tokens(self,messages,*,tools=()):
        return self.inner.count_tokens(messages,tools=tools)

    def remaining_tokens(self,messages,*,tools=()):
        return self.inner.remaining_tokens(messages,tools=tools)

    def total_usage(self):
        return self.inner.total_usage()

    def actual_usage(self):
        return self.inner.actual_usage()

    async def close(self):
        await self.inner.close()

    async def create(self,messages,*,tools=(),tool_choice='auto',json_output=None,extra_create_args=None,cancellation_token=None):
        if extra_create_args:
            raise ValueError('Per-call provider overrides are disabled by the budget wrapper')
        ticket=self.budget.reserve(messages,tools)
        started = time.monotonic()
        try:
            result=await asyncio.wait_for(self.inner.create(messages,tools=tools,tool_choice=tool_choice,
                json_output=json_output,cancellation_token=cancellation_token),self.budget.profile.timeout_seconds)
        except BaseException:
            ticket['status']='failed_usage_unknown'
            # Keep the entire reservation on unknown usage, including timeouts.
            raise
        finally:
            ticket['elapsed_seconds'] = time.monotonic() - started
        self.budget.finish(ticket,result)
        return result

    async def create_stream(self,*args,**kwargs):
        raise NotImplementedError('Streaming is disabled for bounded auditable agent turns')
        yield  # Declare an async iterator for the AutoGen client protocol.
