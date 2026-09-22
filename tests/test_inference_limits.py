import asyncio
import json
from datetime import date
from decimal import Decimal

import pytest
from autogen_core.models import UserMessage, CreateResult, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient

from kdiff.inference.client import Provider, require_execution_support, profile_fingerprint
from kdiff.inference.limits import RunBudget, BoundedClient
from kdiff.core.contracts import now


def live_profile(**kwargs):
    return Provider(profile='local-dev',model='fixture-server',base_url='http://127.0.0.1:18000/v1',
        model_info={'vision':False,'function_calling':True,'json_output':True,'family':'unknown','structured_output':True},
        runtime_revision='transport-test-only',execution_authorized=True,**kwargs)


def test_capability_gate_binds_runtime_and_expires(tmp_path):
    p=live_profile()
    with pytest.raises(ValueError,match='provider-check'):
        require_execution_support(p)
    report={'status': 'passed', 'profile_fingerprint':profile_fingerprint(p),'checked_at':now(),
            'checks':{k:True for k in ['completion','tool_round_trip','structured_output','token_accounting']}}
    path=tmp_path/'capabilities.json'
    path.write_text(json.dumps(report))
    configured=p.model_copy(update={'capability_report':str(path)})
    require_execution_support(configured)
    report['status'] = 'failed'
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match='runtime'):
        require_execution_support(configured)
    report['status'] = 'passed'
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError,match='runtime'):
        require_execution_support(configured.model_copy(update={'runtime_revision':'changed'}))
    report['checked_at']='2000-01-01T00:00:00+00:00'
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError,match='expired'):
        require_execution_support(configured)


def test_token_context_and_call_admission():
    p=live_profile(max_model_calls=1)
    budget=RunBudget(p)
    messages=[UserMessage(content='small',source='user')]
    ticket=budget.reserve(messages,[])
    result=CreateResult(content='ok',finish_reason='stop',usage=RequestUsage(prompt_tokens=8,completion_tokens=2),cached=False)
    budget.finish(ticket,result)
    assert budget.actual_tokens==10
    with pytest.raises(ValueError,match='budget'):
        budget.reserve(messages,[])
    with pytest.raises(ValueError,match='context'):
        RunBudget(live_profile()).reserve([UserMessage(content='x'*20000,source='user')],[])


def test_paid_profile_needs_authorization_and_prices():
    p=Provider(profile='openai',model='configured-model')
    with pytest.raises(ValueError,match='authorization'):
        require_execution_support(p)
    p=p.model_copy(update={'execution_authorized':True})
    with pytest.raises(ValueError,match='cost budget'):
        require_execution_support(p)
    p=p.model_copy(update={'max_cost_usd':Decimal('0.000001'),
        'input_usd_per_million':Decimal('1'),'output_usd_per_million':Decimal('2'),'price_date':date.today()})
    with pytest.raises(ValueError,match='cost'):
        RunBudget(p).reserve([UserMessage(content='test',source='user')],[])


def test_autogen_client_wrapper_records_actual_usage_and_timeout(monkeypatch):
    result=CreateResult(content='ok',finish_reason='stop',usage=RequestUsage(prompt_tokens=8,completion_tokens=2),cached=False)
    inner=ReplayChatCompletionClient([result],model_info={'vision':False,'function_calling':True,'json_output':True,'family':'unknown','structured_output':True})
    budget=RunBudget(live_profile())
    client=BoundedClient(inner,budget)
    assert asyncio.run(client.create([UserMessage(content='test',source='user')])).content=='ok'
    assert budget.report()['actual_tokens']==10
    class Delayed:
        async def create(self,*a,**k):
            await asyncio.sleep(2)
        async def close(self):
            pass
    budget=RunBudget(live_profile(timeout_seconds=1))
    client=BoundedClient(Delayed(),budget)
    with pytest.raises(TimeoutError):
        asyncio.run(client.create([UserMessage(content='test',source='user')]))
    assert budget.events[0]['status']=='failed_usage_unknown'
    assert budget.reserved_tokens>0
    import kdiff.inference.probe as probe
    monkeypatch.setattr(probe, 'client_factory', lambda profile, budget: BoundedClient(Delayed(), budget))
    report = asyncio.run(probe.provider_check(live_profile(timeout_seconds=1)))
    assert report['status'] == 'failed' and report['error_type'] == 'TimeoutError'
    assert report['active_check'] == 'completion'
    assert report['budget']['usage_incomplete']
