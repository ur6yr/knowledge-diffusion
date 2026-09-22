"""Launch fresh authenticated PostgreSQL and Redis using verified user binaries.

No existing server, database or configuration is altered. All endpoints are
loopback and only this launcher's children are stopped. Cluster validation is
separate from importing or unit-testing this module.
"""

import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import time
from uuid import uuid4

from kdiff.deployment.manifest import atomic_json


def secret_file(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
        stream.write(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres-bin', type=Path, required=True)
    parser.add_argument('--redis-server', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--postgres-port', type=int, default=25432)
    parser.add_argument('--redis-port', type=int, default=26379)
    parser.add_argument('--command-timeout', type=int, default=1800)
    parser.add_argument('--export', type=Path, required=True, help='Persistent offline artifact and task export directory')
    parser.add_argument('--restore', type=Path, help='Prior exported state directory, restored only into new dedicated tables')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not os.environ.get('SLURM_JOB_ID'):
        parser.error('Metadata services require a Slurm allocation')
    if not args.command or not 1 <= args.command_timeout <= 86400:
        parser.error('Supply a bounded command')
    pg = args.postgres_bin.resolve()
    for binary in [pg / 'initdb', pg / 'postgres', args.redis_server]:
        if not binary.is_file() or not os.access(binary, os.X_OK):
            parser.error('Verified PostgreSQL/Redis executables are required')
    for port in [args.postgres_port, args.redis_port]:
        if not 1024 < port < 65536 or port in {5432, 6379}:
            parser.error('Choose non-default development service ports')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
    if args.postgres_port == args.redis_port:
        parser.error('Service ports must differ')
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    args.export.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(32)
    secret_file(root / 'pg-password', password + '\n')
    secret_file(root / 'redis.conf', '\n'.join([
        'bind 127.0.0.1', f'port {args.redis_port}', 'protected-mode yes',
        'requirepass ' + password, 'daemonize no', 'appendonly yes', 'appendfsync everysec',
        f'dir "{root}"', 'maxmemory 256mb', 'maxmemory-policy noeviction', 'save ""', '']))
    env = {**os.environ,
           'KDIFF_METADATA_SERVICE': str(root / 'service.json'),
           'KDIFF_POSTGRES_DSN': f'host=127.0.0.1 port={args.postgres_port} dbname=postgres user=kdiff password={password}',
           'KDIFF_REDIS_URL': f'redis://:{password}@127.0.0.1:{args.redis_port}/0'}
    manifest = {'purpose': 'kdiff-owned-metadata', 'generation': str(uuid4()), 'hostname': socket.gethostname(),
                'job_id': os.environ['SLURM_JOB_ID'], 'status': 'starting',
                'postgres_port': args.postgres_port, 'redis_port': args.redis_port,
                'expires_at': time.time() + args.command_timeout + 180}
    atomic_json(root / 'service.json', manifest)
    children, application, interrupted = [], None, None
    def stop(signum, frame):
        nonlocal interrupted
        interrupted = interrupted or time.monotonic()
        if application and application.poll() is None:
            os.killpg(application.pid, signal.SIGTERM)
    handlers = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1)}
    store = ledger = None
    try:
        with ExitStack() as stack:
            initlog = stack.enter_context((root / 'init.log').open('w'))
            result = subprocess.run([str(pg / 'initdb'), '-D', str(root / 'postgres'), '--username=kdiff',
                '--pwfile=' + str(root / 'pg-password'), '--auth=scram-sha-256', '--no-locale', '--encoding=UTF8'],
                stdout=initlog, stderr=subprocess.STDOUT, timeout=60)
            if result.returncode:
                raise RuntimeError('Fresh PostgreSQL initialization failed. Inspect restricted init.log')
            commands = [([str(pg / 'postgres'), '-D', str(root / 'postgres'), '-h', '127.0.0.1',
                          '-p', str(args.postgres_port), '-c', 'unix_socket_directories=',
                          '-c', 'shared_buffers=128MB', '-c', 'max_connections=24'], 'postgres.log'),
                        ([str(args.redis_server.resolve()), str(root / 'redis.conf')], 'redis.log')]
            for command, name in commands:
                log = stack.enter_context((root / name).open('w'))
                children.append(subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True))
            import psycopg
            import redis
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not interrupted:
                if any(child.poll() is not None for child in children):
                    raise RuntimeError('Owned metadata process exited during startup')
                try:
                    with psycopg.connect(env['KDIFF_POSTGRES_DSN'], connect_timeout=2) as connection:
                        connection.execute('SELECT 1')
                    client = redis.Redis.from_url(env['KDIFF_REDIS_URL'], socket_timeout=2)
                    client.ping()
                    break
                except (psycopg.OperationalError, redis.RedisError):
                    time.sleep(.5)
            else:
                raise RuntimeError('Metadata readiness timed out or was interrupted')
            # The child inherits credentials. They are never written to a public
            # manifest or added to the application's command-line arguments.
            os.environ.update({k: env[k] for k in ('KDIFF_POSTGRES_DSN', 'KDIFF_REDIS_URL')})
            from kdiff.core.durable import PostgresArtifacts, TaskLedger
            store, ledger = PostgresArtifacts(initialize=True), TaskLedger(dsn_env='KDIFF_POSTGRES_DSN', initialize=True)
            if args.restore:
                store.restore_files_empty(args.restore / 'store')
                ledger.restore_empty(args.restore / 'state.json')
            manifest.update(status='ready', pids=[child.pid for child in children])
            atomic_json(root / 'service.json', manifest)
            command = args.command[1:] if args.command[0] == '--' else args.command
            application = subprocess.Popen(command, env=env, start_new_session=True)
            deadline = time.monotonic() + args.command_timeout
            while application.poll() is None:
                if time.monotonic() >= deadline and interrupted is None:
                    stop(signal.SIGTERM, None)
                if interrupted and time.monotonic() - interrupted > 30:
                    os.killpg(application.pid, signal.SIGKILL)
                time.sleep(.2)
            return 143 if interrupted else application.returncode
    finally:
        if application and application.poll() is None:
            os.killpg(application.pid, signal.SIGTERM)
            try:
                application.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(application.pid, signal.SIGKILL)
                application.wait()
        try:
            if store:
                store.export(args.export / 'store')
            if ledger:
                ledger.export(args.export / 'state.json')
        finally:
            for child in reversed(children):
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
            manifest['status'] = 'stopped'
            atomic_json(root / 'service.json', manifest)
            for signum, handler in handlers.items():
                signal.signal(signum, handler)


if __name__ == '__main__':
    raise SystemExit(main())
