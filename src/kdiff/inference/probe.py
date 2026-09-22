"""Explicitly authorized provider capability round trips, never run on import."""

import json
from typing import Literal

from pydantic import BaseModel
from autogen_core.models import (UserMessage, AssistantMessage, FunctionExecutionResult,
                                 FunctionExecutionResultMessage)
from autogen_core.tools import FunctionTool

from kdiff.core.contracts import now
from kdiff.inference.client import client_factory, profile_fingerprint, validate_authorization
from kdiff.inference.limits import current_budget


class ProbeOutput(BaseModel):
    value: int
    label: Literal['capability-probe']


async def provider_check(profile):
    if profile.profile=='mock':
        raise ValueError('A scripted response cannot establish live provider capability')
    validate_authorization(profile)
    budget=current_budget(profile)
    client=client_factory(profile,budget=budget)
    report={'profile_fingerprint':profile_fingerprint(profile),'checked_at':now(),
            'model':profile.model,'runtime_revision':profile.runtime_revision,'checks':{},
            'scope':'Live text, tool and structured-output round trips. Failure-path guards are tested separately.'}
    try:
        report['active_check'] = 'completion'
        result=await client.create([UserMessage(content='Reply with the single word READY.',source='user')])
        report['checks']['completion']=isinstance(result.content,str) and result.content.strip()=='READY'

        def add(left:int,right:int)->int:
            """Add two integers for a capability test."""
            return left+right
        tool=FunctionTool(add,description='Add the supplied integers')
        report['active_check'] = 'tool_round_trip'
        messages=[UserMessage(content='Use the add tool to add 4 and 7.',source='user')]
        result=await client.create(messages,tools=[tool],tool_choice='required')
        tool_ok=False
        if isinstance(result.content,list) and len(result.content)==1:
            call=result.content[0]
            args=json.loads(call.arguments)
            if call.name=='add' and args=={'left':4,'right':7}:
                messages += [AssistantMessage(content=result.content,source='assistant'),
                    FunctionExecutionResultMessage(content=[FunctionExecutionResult(content='11',name='add',call_id=call.id,is_error=False)]),
                    UserMessage(content='Reply with only the numeric tool result.',source='user')]
                final=await client.create(messages)
                tool_ok=isinstance(final.content,str) and final.content.strip()=='11'
        report['checks']['tool_round_trip']=tool_ok
        report['active_check'] = 'structured_output'
        result=await client.create([UserMessage(content='Return JSON with value 17 and label capability-probe.',source='user')],json_output=ProbeOutput)
        parsed=ProbeOutput.model_validate_json(result.content) if isinstance(result.content,str) else None
        report['checks']['structured_output']=parsed is not None and parsed.value==17
        report['checks']['token_accounting']=budget.actual_tokens>0 and all(e['status']=='completed' for e in budget.events)
        report['status']='passed' if all(report['checks'].values()) else 'failed'
        report.pop('active_check', None)
    except Exception as exc:
        report.update(status='failed', error_type=type(exc).__name__)
    finally:
        await client.close()
    report['budget']=budget.report()
    return report
