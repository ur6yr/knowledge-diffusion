"""Durable task/outbox state and PostgreSQL artifacts, isolated from the graph."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from contextlib import contextmanager
from uuid import uuid4

from kdiff.core.contracts import canonical, digest, now


class PostgresArtifacts:
    """Dedicated application schema with immutable bytes and JSONB extraction records."""

    def __init__(self, dsn_env='KDIFF_POSTGRES_DSN', *, initialize=False):
        import psycopg
        self.dsn = os.environ.get(dsn_env)
        if not self.dsn:
            raise ValueError('PostgreSQL connection environment variable is not configured')
        self.connect = lambda: psycopg.connect(self.dsn, connect_timeout=5)
        if initialize:
            with self.connect() as conn:
                conn.execute('CREATE TABLE IF NOT EXISTS kdiff_artifact (sha TEXT PRIMARY KEY, data BYTEA NOT NULL, payload JSONB, captured_at TEXT NOT NULL)')
        with self.connect() as conn:
            conn.execute('SELECT sha FROM kdiff_artifact LIMIT 0')

    def put_bytes(self, data):
        from psycopg.types.json import Jsonb
        if len(data) > 64 * 1024 * 1024:
            raise ValueError('Artifact exceeds 64 MiB limit')
        sha = hashlib.sha256(data).hexdigest()
        try:
            payload = Jsonb(json.loads(data))
        except (ValueError, UnicodeDecodeError):
            payload = None
        with self.connect() as conn:
            conn.execute('INSERT INTO kdiff_artifact VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                         (sha, data, payload, now()))
            saved = bytes(conn.execute('SELECT data FROM kdiff_artifact WHERE sha=%s', (sha,)).fetchone()[0])
        if saved != data:
            raise ValueError('Artifact integrity mismatch')
        return sha

    def get_bytes(self, sha):
        with self.connect() as conn:
            row = conn.execute('SELECT data FROM kdiff_artifact WHERE sha=%s', (sha,)).fetchone()
        if row is None:
            raise ValueError('Missing durable artifact')
        data = bytes(row[0])
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError('Durable artifact integrity failure')
        return data

    def put(self, value):
        return self.put_bytes(canonical(value))

    def get(self, sha):
        return json.loads(self.get_bytes(sha))

    def capture(self, data):
        sha = self.put_bytes(data)
        with self.connect() as conn:
            row = conn.execute('SELECT captured_at FROM kdiff_artifact WHERE sha=%s', (sha,)).fetchone()
        return {'sha256': sha, 'bytes': len(data), 'retrieved_at': row[0]}

    def export(self, target, *, max_bytes=1024 * 1024 * 1024, max_records=100000):
        from kdiff.core.artifacts import ArtifactStore
        store = ArtifactStore(target)
        with self.connect() as conn, conn.cursor(name='artifact_export') as cursor:
            size = conn.execute('SELECT count(*), coalesce(sum(octet_length(data)),0) FROM kdiff_artifact').fetchone()
            if size[0] > max_records or size[1] > max_bytes:
                raise ValueError('Artifact export budget exceeded')
            cursor.execute('SELECT sha, data, captured_at FROM kdiff_artifact ORDER BY sha')
            total = 0
            for index, (sha, data, captured_at) in enumerate(cursor, 1):
                total += len(data)
                if index > max_records or total > max_bytes:
                    raise ValueError('Artifact export grew beyond its budget')
                if store.put_bytes(bytes(data)) != sha:
                    raise ValueError('Artifact export integrity failure')
                from kdiff.deployment.manifest import atomic_json
                atomic_json(store.root / f'capture-{sha}.json',
                            {'sha256': sha, 'bytes': len(data), 'retrieved_at': captured_at})
        return store

    def restore_files_empty(self, path, *, max_bytes=1024 * 1024 * 1024, max_records=100000):
        from kdiff.core.artifacts import ArtifactStore
        from psycopg.types.json import Jsonb
        source = ArtifactStore(path)
        files = list(source.root.glob('[a-f0-9][a-f0-9]/*'))
        if len(files) > max_records or sum(p.stat().st_size for p in files) > max_bytes:
            raise ValueError('Artifact restore budget exceeded')
        with self.connect() as conn:
            conn.execute('LOCK TABLE kdiff_artifact IN EXCLUSIVE MODE')
            if conn.execute('SELECT count(*) FROM kdiff_artifact').fetchone()[0]:
                raise ValueError('Artifact restore requires an empty dedicated table')
            for file in files:
                data = source.get_bytes(file.name)
                metadata = json.loads((source.root / f'capture-{file.name}.json').read_text())
                if metadata['sha256'] != file.name or metadata['bytes'] != len(data):
                    raise ValueError('Artifact capture metadata mismatch')
                try:
                    value = Jsonb(json.loads(data))
                except (ValueError, UnicodeDecodeError):
                    value = None
                conn.execute('INSERT INTO kdiff_artifact VALUES (%s,%s,%s,%s)',
                             (file.name, data, value, metadata['retrieved_at']))


class TaskLedger:
    """Fenced task leases. PostgreSQL is required for shared-node operation.

    SQLite is available only for a single-host development coordinator. The
    ledger is authoritative. Redis is a delivery accelerator and can be rebuilt.
    """

    def __init__(self, path=None, *, dsn_env=None, initialize=False):
        self.postgres = dsn_env is not None
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row
            dsn = os.environ.get(dsn_env)
            if not dsn:
                raise ValueError('Task database is not configured')
            self.connect = lambda: psycopg.connect(dsn, connect_timeout=5, row_factory=dict_row)
        else:
            if path is None:
                raise ValueError('Development task database path is required')
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.connect = lambda: sqlite3.connect(path, timeout=5, isolation_level=None)
        if initialize:
            with self.transaction() as conn:
                self.sql(conn, '''CREATE TABLE IF NOT EXISTS kdiff_task (
                    task_id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL,
                    owner TEXT, token TEXT, generation INTEGER NOT NULL DEFAULT 0,
                    expires DOUBLE PRECISION, attempts INTEGER NOT NULL DEFAULT 0,
                    result TEXT, error TEXT, created DOUBLE PRECISION NOT NULL)''')
                self.sql(conn, '''CREATE TABLE IF NOT EXISTS kdiff_intent (
                    task_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, state TEXT NOT NULL,
                    receipt TEXT)''')
                self.sql(conn, '''CREATE TABLE IF NOT EXISTS kdiff_checkpoint (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL, revision INTEGER NOT NULL)''')

    def sql(self, conn, query, args=()):
        return conn.execute(query.replace('?', '%s') if self.postgres else query, args)

    @contextmanager
    def transaction(self):
        conn = self.connect()
        if not self.postgres:
            conn.row_factory = sqlite3.Row
            conn.execute('BEGIN IMMEDIATE')
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def enqueue(self, payload):
        task_id = digest(payload)
        with self.transaction() as conn:
            self.sql(conn, 'INSERT INTO kdiff_task(task_id,payload,state,created) VALUES (?,?,?,?) ON CONFLICT DO NOTHING',
                     (task_id, canonical(payload).decode(), 'pending', time.time()))
        return task_id

    def claim(self, owner, ttl=120, max_attempts=3, task_id=None):
        if not 1 <= ttl <= 3600 or not 1 <= max_attempts <= 10:
            raise ValueError('Invalid lease/retry limit')
        with self.transaction() as conn:
            t = time.time()
            self.sql(conn, "UPDATE kdiff_task SET state='failed',error='retry_limit' WHERE attempts>=? AND (state='pending' OR (state='leased' AND expires<?))", (max_attempts,t))
            query = "SELECT * FROM kdiff_task WHERE attempts<? AND (state='pending' OR (state='leased' AND expires<?))"
            args = [max_attempts, t]
            if task_id:
                query += ' AND task_id=?'
                args.append(task_id)
            query += ' ORDER BY created,task_id LIMIT 1'
            if self.postgres:
                query += ' FOR UPDATE SKIP LOCKED'
            row = self.sql(conn, query, args).fetchone()
            if row is None:
                return None
            token = str(uuid4())
            self.sql(conn, "UPDATE kdiff_task SET state='leased',owner=?,token=?,generation=generation+1,expires=?,attempts=attempts+1 WHERE task_id=?",
                     (owner, token, t+ttl, row['task_id']))
            return {'task_id': row['task_id'], 'owner': owner, 'token': token,
                    'generation': row['generation']+1, 'payload': json.loads(row['payload'])}

    @contextmanager
    def fenced(self, lease):
        with self.transaction() as conn:
            row = self.sql(conn, 'SELECT * FROM kdiff_task WHERE task_id=?' + (' FOR UPDATE' if self.postgres else ''),
                           (lease['task_id'],)).fetchone()
            if (row is None or row['state'] != 'leased' or row['token'] != lease['token']
                    or row['owner'] != lease['owner'] or row['generation'] != lease['generation']
                    or row['expires'] < time.time()):
                raise ValueError('Stale or expired task lease')
            yield conn

    def heartbeat(self, lease, ttl=120):
        if not 1 <= ttl <= 3600:
            raise ValueError('Invalid lease duration')
        with self.fenced(lease) as conn:
            self.sql(conn, 'UPDATE kdiff_task SET expires=? WHERE task_id=?', (time.time()+ttl, lease['task_id']))

    def prepare(self, lease, batch_id):
        with self.fenced(lease) as conn:
            self.sql(conn, "INSERT INTO kdiff_intent(task_id,batch_id,state) VALUES (?,?,'prepared') ON CONFLICT DO NOTHING", (lease['task_id'],batch_id))
            row = self.sql(conn, 'SELECT batch_id FROM kdiff_intent WHERE task_id=?', (lease['task_id'],)).fetchone()
            if row['batch_id'] != batch_id:
                raise ValueError('Task input changed after durable preparation')

    def integrate(self, lease, batch_id, callback):
        # Hold the authoritative task-row lock across graph commit. Reapers cannot
        # replace its fencing token in this critical section. If this process dies
        # after Neo4j commits, its PostgreSQL transaction rolls back and the next
        # owner retries the exact content-addressed batch idempotently.
        with self.fenced(lease) as conn:
            intent = self.sql(conn, 'SELECT * FROM kdiff_intent WHERE task_id=?', (lease['task_id'],)).fetchone()
            if not intent or intent['batch_id'] != batch_id:
                raise ValueError('Graph write requires a durable prepared intent')
            result = callback()
            encoded = canonical(result).decode()
            self.sql(conn, "UPDATE kdiff_intent SET state='committed',receipt=? WHERE task_id=?", (encoded,lease['task_id']))
            self.sql(conn, "UPDATE kdiff_task SET state='done',result=?,expires=NULL WHERE task_id=?", (encoded,lease['task_id']))
        return result

    def complete(self, lease, result):
        with self.fenced(lease) as conn:
            self.sql(conn, "UPDATE kdiff_task SET state='done',result=?,expires=NULL WHERE task_id=?", (canonical(result).decode(),lease['task_id']))

    def release(self, lease, error='interrupted'):
        with self.fenced(lease) as conn:
            self.sql(conn, "UPDATE kdiff_task SET state='pending',owner=NULL,token=NULL,expires=NULL,error=? WHERE task_id=?", (error,lease['task_id']))

    def status(self):
        with self.transaction() as conn:
            return [dict(row) for row in self.sql(conn, 'SELECT task_id,state,owner,generation,attempts,error FROM kdiff_task ORDER BY created,task_id')]

    def lookup(self, task_id):
        with self.transaction() as conn:
            row=self.sql(conn,'SELECT * FROM kdiff_task WHERE task_id=?',(task_id,)).fetchone()
        if row is None:
            return None
        value=dict(row)
        value['payload']=json.loads(value['payload'])
        value['result']=json.loads(value['result']) if value['result'] else None
        return value

    def checkpoint(self, name, value, expected_revision=None):
        with self.transaction() as conn:
            if self.postgres:
                # Serialize the absent-row case as well as existing revisions.
                key = int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], 'big', signed=True)
                conn.execute('SELECT pg_advisory_xact_lock(%s)', (key,))
            row = self.sql(conn, 'SELECT revision FROM kdiff_checkpoint WHERE name=?' + (' FOR UPDATE' if self.postgres else ''), (name,)).fetchone()
            revision = row['revision'] if row else 0
            if expected_revision is not None and revision != expected_revision:
                raise ValueError('Checkpoint was concurrently updated')
            self.sql(conn, 'INSERT INTO kdiff_checkpoint(name,value,revision) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,revision=excluded.revision',
                     (name,canonical(value).decode(),revision+1))
            return revision+1

    def read_checkpoint(self, name):
        with self.transaction() as conn:
            row = self.sql(conn, 'SELECT value,revision FROM kdiff_checkpoint WHERE name=?', (name,)).fetchone()
        return (json.loads(row['value']),row['revision']) if row else (None,0)

    @contextmanager
    def coordinator(self, name):
        """Fail-fast transaction-scoped leader lock, automatically released on death."""
        with self.transaction() as conn:
            if self.postgres:
                key = int.from_bytes(hashlib.sha256(('coordinator:' + name).encode()).digest()[:8], 'big', signed=True)
                row = conn.execute('SELECT pg_try_advisory_xact_lock(%s) AS acquired', (key,)).fetchone()
                if not row['acquired']:
                    raise ValueError('Another coordinator holds this maintenance lease')
            yield

    def export(self, path, *, max_records=100000):
        from kdiff.deployment.manifest import atomic_json
        with self.transaction() as conn:
            tables = {}
            for name in ('kdiff_task', 'kdiff_intent', 'kdiff_checkpoint'):
                rows = [dict(r) for r in self.sql(conn, f'SELECT * FROM {name} LIMIT ?', (max_records + 1,))]
                if len(rows) > max_records:
                    raise ValueError('Task export budget exceeded')
                tables[name] = rows
        body = {'kind': 'task-checkpoint-v1', 'tables': tables}
        atomic_json(Path(path), {'body': body, 'sha256': digest(body)})

    def restore_empty(self, path):
        if Path(path).stat().st_size > 64 * 1024 * 1024:
            raise ValueError('Task checkpoint exceeds budget')
        value = json.loads(Path(path).read_text())
        body = value['body']
        if digest(body) != value['sha256'] or body['kind'] != 'task-checkpoint-v1':
            raise ValueError('Task checkpoint integrity failure')
        expected = {'kdiff_task': {'task_id','payload','state','owner','token','generation','expires','attempts','result','error','created'},
                    'kdiff_intent': {'task_id','batch_id','state','receipt'},
                    'kdiff_checkpoint': {'name','value','revision'}}
        if set(body['tables']) != set(expected):
            raise ValueError('Unexpected task checkpoint table')
        with self.transaction() as conn:
            for table, columns in expected.items():
                if self.sql(conn, f'SELECT 1 FROM {table} LIMIT 1').fetchone():
                    raise ValueError('Task restore requires empty dedicated tables')
                for row in body['tables'][table]:
                    if set(row) != columns:
                        raise ValueError('Unexpected task checkpoint columns')
                    if table == 'kdiff_task':
                        if digest(json.loads(row['payload'])) != row['task_id']:
                            raise ValueError('Task payload hash mismatch')
                        if row['state'] == 'leased':
                            row = {**row, 'state': 'pending', 'owner': None, 'token': None, 'expires': None}
                    names = sorted(columns)
                    self.sql(conn, f"INSERT INTO {table}({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                             tuple(row[name] for name in names))


class RedisDelivery:
    """BLMOVE delivery with durable-ledger fencing, never an exactly-once claim."""

    def __init__(self, ledger, *, url_env='KDIFF_REDIS_URL', namespace='kdiff'):
        import redis
        url = os.environ.get(url_env)
        if not url:
            raise ValueError('Redis URL is not configured')
        self.redis = redis.Redis.from_url(url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
        self.ledger, self.prefix = ledger, namespace

    def reconstruct(self, kind=None):
        # Duplicate deliveries are harmless because the durable ledger grants one
        # current fencing token and completed tasks cannot be claimed again.
        for row in self.ledger.status():
            if row['state'] in {'pending','leased'}:
                if kind and self.ledger.lookup(row['task_id'])['payload'].get('kind') != kind:
                    continue
                self.redis.lpush(self.prefix+':pending', row['task_id'])

    def claim(self, owner, ttl=120):
        pending, processing = self.prefix+':pending', self.prefix+':processing:'+owner
        task_id = self.redis.blmove(pending, processing, 1, src='RIGHT', dest='LEFT')
        if task_id is None:
            return None
        lease = self.ledger.claim(owner, ttl=ttl, task_id=task_id)
        if lease is None:
            self.redis.lrem(processing, 1, task_id)
            return None
        lease['delivery_list'] = processing
        return lease

    def acknowledge(self, lease):
        rows = {r['task_id']: r for r in self.ledger.status()}
        row = rows.get(lease['task_id'])
        if not row or row['state'] != 'done' or row['owner'] != lease['owner'] or row['generation'] != lease['generation']:
            raise ValueError('Stale delivery acknowledgment')
        self.redis.lrem(lease['delivery_list'], 1, lease['task_id'])
