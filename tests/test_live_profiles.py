"""Opt-in real-provider probes. Missing endpoints remain explicit skips."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from kdiff.inference.client import Provider
from kdiff.inference.probe import provider_check


@pytest.mark.live
@pytest.mark.parametrize('name,variable',[('local-vllm','KDIFF_LOCAL_TEST_PROFILE'),('openai','KDIFF_OPENAI_TEST_PROFILE')])
def test_live_provider_capability_suite(name,variable,tmp_path):
    if os.environ.get('KDIFF_RUN_LIVE_TESTS')!='1' or not os.environ.get(variable):
        pytest.skip(f'{name}: set KDIFF_RUN_LIVE_TESTS=1 and {variable} with an authorized bounded profile')
    profile=Provider.model_validate_json(Path(os.environ[variable]).read_text())
    result=asyncio.run(provider_check(profile))
    (tmp_path/'provider-report.json').write_text(json.dumps(result,indent=2))
    assert result['status']=='passed'
