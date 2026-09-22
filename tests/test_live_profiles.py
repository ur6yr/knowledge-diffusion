import pytest


@pytest.mark.live
@pytest.mark.parametrize("profile", ["local-vllm", "openai"])
def test_live_provider_capability_suite_pending(profile):
    pytest.skip(f"{profile}: M5 live capability suite not implemented; no authorized endpoint/budget in this session")
