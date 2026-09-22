"""Bounded coordinated W1 workers using durable state and optional Redis delivery."""

import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import time
from uuid import uuid4

from kdiff.core.artifacts import exclusive_lock
from kdiff.core.contracts import digest
from kdiff.core.durable import RedisDelivery
from kdiff.inference.client import profile_fingerprint
from kdiff.inference.limits import budget_scope
from kdiff.workflows import build


def enqueue_builds(manifest, store, ledger, profile):
    if not 1 <= len(manifest) <= 1000:
        raise ValueError('Construction manifest must contain 1 to 1000 bounded inputs')
    from kdiff.cli import code_digest
    from kdiff.core.schema import SCHEMA_VERSION
    staged = []
    for entry in manifest:
        if set(entry) - {'input', 'namespace', 'source', 'format', 'columns'}:
            raise ValueError('Unknown construction manifest fields')
        path = Path(entry['input']).resolve()
        if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('Construction input unavailable or exceeds byte budget')
        capture = store.capture(path.read_bytes())
        staged.append({'kind': 'construction', 'input': str(path), 'raw_hash': capture['sha256'],
                       'namespace': entry['namespace'], 'source': entry.get('source', 'openalex'),
                       'format': entry.get('format', 'jsonl'), 'columns': entry.get('columns'),
                       'profile': profile_fingerprint(profile), 'code': code_digest(), 'schema': SCHEMA_VERSION})
    ids = [ledger.enqueue(payload) for payload in staged]
    return {'tasks': ids, 'records': len(ids), 'input_bytes': sum(len(store.get_bytes(x['raw_hash'])) for x in staged)}


@contextmanager
def wait_for_writer(path, timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        lock = exclusive_lock(path)
        try:
            lock.__enter__()
            break
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise ValueError('Coordinated writer wait exceeded budget') from None
            time.sleep(.1)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


async def work(graph, store, ledger, profile, *, allow_mock=False, max_tasks=1, use_redis=False, use_cache=False):
    if not 1 <= max_tasks <= 3:
        raise ValueError('A bounded worker handles at most three batches per invocation')
    from kdiff.cli import code_digest
    owner = str(uuid4())
    delivery = RedisDelivery(ledger, namespace='kdiff:construction') if use_redis else None
    if delivery:
        delivery.reconstruct(kind='construction')
    from kdiff.core.cache import ExtractionCache
    cache = ExtractionCache(store, ledger, redis=delivery.redis if delivery else None) if use_cache else None
    results = []
    with budget_scope(profile) as budget:
        for _ in range(max_tasks):
            if delivery:
                lease = delivery.claim(owner, ttl=900)
            else:
                lease = None
                for row in ledger.status():
                    candidate = ledger.lookup(row['task_id'])
                    if candidate['payload'].get('kind') != 'construction':
                        continue
                    lease = ledger.claim(owner, ttl=900, task_id=row['task_id'])
                    if lease:
                        break
            if lease is None:
                break
            task = lease['payload']
            try:
                if task['profile'] != profile_fingerprint(profile) or task['code'] != code_digest():
                    raise ValueError('Task runtime changed. Submit a new versioned task')
                path = Path(task['input'])
                if path.stat().st_size > 16*1024*1024 or digest_bytes(path.read_bytes()) != task['raw_hash']:
                    raise ValueError('Source changed after task submission')
                with wait_for_writer(Path(graph.meta['root']) / 'application.lock'):
                    result = await build(graph, store, profile, allow_mock, path, task['namespace'], source=task['source'],
                        input_format=task['format'], columns=task['columns'], ledger=ledger, integration_lease=lease,
                        use_cache=use_cache, extraction_cache=cache)
                if delivery:
                    delivery.acknowledge(lease)
                results.append({'task_id': lease['task_id'], 'status': 'done', 'result': result})
            except BaseException as exc:
                try:
                    ledger.release(lease, error=type(exc).__name__)
                except ValueError:
                    pass  # A committed or reclaimed task cannot be released by this worker.
                raise
    return {'owner': owner, 'tasks': results, 'budget': budget.report(),
            'write_policy': 'coordinated single writer, at-least-once task delivery'}


def digest_bytes(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()
