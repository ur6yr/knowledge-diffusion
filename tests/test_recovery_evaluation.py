import concurrent.futures
import json
import time

import pytest

from kdiff.core.durable import TaskLedger
from kdiff.core.contracts import digest
from kdiff.evaluation.benchmark import import_benchmark, experiment_key
from kdiff.evaluation.metrics import aggregate, paired_bootstrap, Annotation
from kdiff.core.graph import load_service


@pytest.mark.parametrize('workers', [1, 2, 4, 8])
def test_duplicate_delivery_converges_and_capture_is_atomic(tmp_path, store, workers):
    ledger = TaskLedger(tmp_path / 'state.sqlite', initialize=True)
    for i in range(30):
        for _ in range(3):
            ledger.enqueue({'item': i})
    def worker(index):
        captures = []
        while True:
            lease = ledger.claim(str(index))
            if lease is None:
                return captures
            captured = store.capture(b'concurrent unchanged source')
            captures.append(captured)
            ledger.complete(lease, {'item': lease['payload']['item']})
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        captures = [x for result in pool.map(worker, range(workers)) for x in result]
    assert len(ledger.status()) == 30
    assert all(row['state'] == 'done' and row['generation'] == 1 for row in ledger.status())
    assert all(x == captures[0] for x in captures)


def test_restore_lease_fencing_and_checkpoint_cas(tmp_path):
    ledger = TaskLedger(tmp_path / 'one.sqlite', initialize=True)
    task = ledger.enqueue({'fixture': 1})
    lease = ledger.claim('old')
    ledger.prepare(lease, 'batch')
    ledger.checkpoint('dialogue', {'release_id': 'original'}, 0)
    with pytest.raises(ValueError, match='concurrently'):
        ledger.checkpoint('dialogue', {}, 0)
    ledger.export(tmp_path / 'state.json')
    restored = TaskLedger(tmp_path / 'two.sqlite', initialize=True)
    restored.restore_empty(tmp_path / 'state.json')
    assert restored.lookup(task)['state'] == 'pending'
    with pytest.raises(ValueError, match='Stale'):
        restored.complete(lease, {})
    fresh = restored.claim('new')
    assert fresh['generation'] == lease['generation'] + 1
    restored.integrate(fresh, 'batch', lambda: {'graph_commit': True})
    assert restored.lookup(task)['state'] == 'done'
    with pytest.raises(ValueError, match='empty'):
        restored.restore_empty(tmp_path / 'state.json')


def test_missing_original_and_variant_isolation(store, tmp_path):
    with pytest.raises(ValueError, match='absent'):
        import_benchmark(tmp_path / 'missing.jsonl', store, name='original', origin='original')
    assert experiment_key('b', 'i', 'full', 'model', 1) != experiment_key('b', 'i', 'no_verifier', 'model', 1)
    with pytest.raises(ValueError):
        Annotation.model_validate({'review_method': 'same_verifier'})


def annotated(item_id, correct=0):
    return dict(item_id=item_id, correct=correct, asserted=1, unsupported=1-correct, required=1, recovered=correct,
                partial_candidates=0, partial_handled=0, refusals=0, correct_refusals=0,
                identity_correct=None, temporal_correct=None, plan_valid_initial=True,
                plan_valid_after_repair=True, outcome='Answer', valid_witnesses=0, witness_ids=[])


def test_denominators_and_paired_dialogue_bootstrap():
    assert aggregate([])['claim_correctness']['value'] is None
    left = [annotated('dialogue-1'), annotated('dialogue-2')]
    right = [annotated('dialogue-1', 1), annotated('dialogue-2', 1)]
    result = paired_bootstrap(left, right, seed=42, resamples=100)
    assert result['item_count'] == 2
    assert result['interval_95_percentile'] == [1, 1]
    assert result == paired_bootstrap(left, right, seed=42, resamples=100)
    with pytest.raises(ValueError, match='identical'):
        paired_bootstrap(left, right[:1])


def test_service_manifest_rejects_stale_host_and_status(tmp_path):
    import os
    import socket
    (tmp_path / 'password').write_text('fixture-not-a-service-secret')
    (tmp_path / 'password').chmod(0o600)
    manifest = dict(purpose='kdiff-m1-development', root=str(tmp_path), owner_id='owner', generation='generation',
                    hostname=socket.gethostname(), uri='bolt://127.0.0.1:17687', expires_at=time.time()+60, status='ready')
    path = tmp_path / 'service.json'
    path.write_text(json.dumps(manifest))
    assert load_service(path)['generation'] == 'generation'
    for change in [{'hostname': 'other-node'}, {'status': 'stopped'}, {'expires_at': 0}]:
        path.write_text(json.dumps({**manifest, **change}))
        with pytest.raises(ValueError):
            load_service(path)


def test_serving_launcher_keeps_virtual_environment_interpreter(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from kdiff.deployment.vllm import python_server_command
    environment = tmp_path / 'serving-environment'
    (environment / 'bin').mkdir(parents=True)
    (environment / 'pyvenv.cfg').write_text('home = ' + str(Path(sys.base_prefix) / 'bin') + '\ninclude-system-site-packages = false\n')
    interpreter = environment / 'bin/python'
    interpreter.symlink_to(Path(sys.executable).resolve())
    command = python_server_command(interpreter)
    result = subprocess.run(command[:1] + ['-c', 'import sys; print(sys.prefix)'], capture_output=True, text=True, check=True)
    assert Path(result.stdout.strip()) == environment
    assert Path(command[0]).is_symlink()
