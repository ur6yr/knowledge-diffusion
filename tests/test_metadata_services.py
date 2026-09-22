"""Opt-in tests for launcher-owned PostgreSQL and Redis, never default services."""
import json
import os
from pathlib import Path
import socket
import time
from uuid import uuid4

import pytest

from kdiff.core.durable import PostgresArtifacts, RedisDelivery, TaskLedger


def owned_metadata():
    path = os.environ.get('KDIFF_METADATA_SERVICE')
    if os.environ.get('KDIFF_RUN_METADATA_TESTS') != '1' or not path:
        pytest.skip('Owned PostgreSQL/Redis live services not explicitly configured')
    manifest = json.loads(Path(path).read_text())
    assert manifest['purpose'] == 'kdiff-owned-metadata'
    assert manifest['status'] == 'ready' and manifest['hostname'] == socket.gethostname()
    assert manifest['expires_at'] > time.time()
    from psycopg.conninfo import conninfo_to_dict
    from urllib.parse import urlsplit
    pg = conninfo_to_dict(os.environ['KDIFF_POSTGRES_DSN'])
    redis = urlsplit(os.environ['KDIFF_REDIS_URL'])
    assert pg['host'] == redis.hostname == '127.0.0.1'
    assert int(pg['port']) == manifest['postgres_port']
    assert redis.port == manifest['redis_port']


def test_live_postgres_artifacts_and_fences():
    owned_metadata()
    store = PostgresArtifacts()
    raw = ('fixture:' + str(uuid4())).encode()
    first, second = store.capture(raw), store.capture(raw)
    assert first == second and store.get_bytes(first['sha256']) == raw
    ledger = TaskLedger(dsn_env='KDIFF_POSTGRES_DSN')
    task = ledger.enqueue({'kind': 'metadata-test', 'nonce': str(uuid4())})
    lease = ledger.claim('fixture-owner', task_id=task)
    ledger.prepare(lease, first['sha256'])
    ledger.integrate(lease, first['sha256'], lambda: {'artifact': first['sha256']})
    assert ledger.lookup(task)['state'] == 'done'
    with pytest.raises(ValueError, match='Stale'):
        ledger.release(lease)


def test_live_redis_reconstruction_and_acknowledgment():
    owned_metadata()
    ledger = TaskLedger(dsn_env='KDIFF_POSTGRES_DSN')
    namespace = 'fixture:' + str(uuid4())
    task = ledger.enqueue({'kind': namespace})
    queue = RedisDelivery(ledger, namespace=namespace)
    queue.reconstruct(kind=namespace)
    queue.reconstruct(kind=namespace)
    lease = queue.claim('fixture-owner')
    assert lease['task_id'] == task
    ledger.complete(lease, {'done': True})
    queue.acknowledge(lease)
    assert queue.claim('next-owner') is None
