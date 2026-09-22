import pytest
from kdiff.core.cache import ExtractionCache, extraction_key
from kdiff.core.durable import TaskLedger
from kdiff.construction.worker import enqueue_builds
from kdiff.inference.client import Provider


def key(raw='one', revision='one'):
    return extraction_key(raw_hash=raw, parser='parser', prompt_hash='prompt', model_revision=revision,
                          schema_version='schema', identity_version='identity')


def test_cache_invalidation_and_eviction_preserve_evidence(store, tmp_path):
    ledger = TaskLedger(tmp_path / 'state.sqlite', initialize=True)
    cache = ExtractionCache(store, ledger, capacity=1)
    cache.put(key(), {'facts': [1, 2]})
    cache.put(key('two'), {'facts': [3]})
    assert cache.get(key()) == {'facts': [1, 2]}
    assert cache.hits['durable'] == 1
    assert cache.get(key(revision='changed')) is None
    assert cache.get(key('changed')) is None
    with pytest.raises(ValueError, match='changed'):
        cache.put(key(), {'facts': [999]})
    assert cache.get(key()) == {'facts': [1, 2]}


def test_queue_duplicate_sources_have_stable_versioned_tasks(store, tmp_path):
    ledger = TaskLedger(tmp_path / 'state.sqlite', initialize=True)
    source = tmp_path / 'source.jsonl'
    source.write_text('{}\n')
    profile = Provider(profile='mock', model='scripted-fixture-v1')
    entry = {'input': str(source), 'namespace': 'fixture:queue'}
    first = enqueue_builds([entry, entry], store, ledger, profile)
    assert first['tasks'][0] == first['tasks'][1]
    assert len(ledger.status()) == 1
    source.write_text('{"changed":true}\n')
    changed = enqueue_builds([entry], store, ledger, profile)
    assert changed['tasks'][0] != first['tasks'][0]
