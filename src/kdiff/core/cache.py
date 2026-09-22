"""Version-keyed L1/L2 extraction caches. Cached bytes never establish evidence."""

from collections import OrderedDict
from kdiff.core.contracts import digest


def extraction_key(*, raw_hash, parser, prompt_hash, model_revision, schema_version, identity_version):
    return digest(dict(raw_hash=raw_hash, parser=parser, prompt_hash=prompt_hash, model_revision=model_revision,
                       schema_version=schema_version, identity_version=identity_version))


class ExtractionCache:
    def __init__(self, store, ledger, *, capacity=128, redis=None, ttl=86400):
        if not 1 <= capacity <= 10000 or not 1 <= ttl <= 604800:
            raise ValueError('Invalid cache bounds')
        self.store, self.ledger, self.redis = store, ledger, redis
        self.capacity, self.ttl = capacity, ttl
        self.local = OrderedDict()
        self.hits = {'l1': 0, 'l2': 0, 'durable': 0, 'miss': 0}

    def get(self, key):
        pointer, tier = self.local.get(key), 'l1'
        if pointer is None and self.redis is not None:
            try:
                pointer, tier = self.redis.get('kdiff:extraction:' + key), 'l2'
                if isinstance(pointer, bytes):
                    pointer = pointer.decode()
            except Exception:
                pointer = None
        if pointer is None:
            checkpoint, _ = self.ledger.read_checkpoint('extraction:' + key)
            pointer, tier = (checkpoint or {}).get('artifact'), 'durable'
        if pointer is None:
            self.hits['miss'] += 1
            return None
        value = self.store.get(pointer)
        if value.get('cache_key') != key:
            raise ValueError('Cache value does not match its complete dependency key')
        self.hits[tier] += 1
        self._remember(key, pointer)
        return value['result']

    def put(self, key, result):
        pointer = self.store.put({'cache_key': key, 'result': result})
        previous, revision = self.ledger.read_checkpoint('extraction:' + key)
        if previous and previous['artifact'] != pointer:
            raise ValueError('Deterministic extraction changed under the same dependency key')
        if previous is None:
            try:
                self.ledger.checkpoint('extraction:' + key, {'artifact': pointer}, revision)
            except ValueError:
                current, _ = self.ledger.read_checkpoint('extraction:' + key)
                if current != {'artifact': pointer}:
                    raise
        if self.redis is not None:
            try:
                self.redis.set('kdiff:extraction:' + key, pointer, ex=self.ttl)
            except Exception:
                pass  # Durable evidence remains authoritative during cache outage.
        self._remember(key, pointer)
        return result

    def _remember(self, key, pointer):
        self.local[key] = pointer
        self.local.move_to_end(key)
        while len(self.local) > self.capacity:
            self.local.popitem(last=False)
