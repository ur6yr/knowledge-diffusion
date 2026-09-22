#!/usr/bin/env python3
"""Launch a fresh isolated Neo4j, run one bounded command, stop only our child."""

import argparse
import json
import os
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--home", type=Path, required=True, help="Existing verified Neo4j distribution (not data)")
    p.add_argument("--java-home", type=Path, required=True)
    p.add_argument("--root", type=Path, required=True, help="New development directory; existing directories refused")
    p.add_argument("--bolt-port", type=int, default=17687)
    p.add_argument('--command-timeout', type=int, default=600)
    p.add_argument('--restore', help='Verified logical release ID from --store, restored into a new store')
    p.add_argument('--store', type=Path, default=Path('artifacts/store'))
    p.add_argument('--postgres-artifacts', action='store_true')
    p.add_argument('--checkpoint-namespace', help='Freeze this namespace after the application stops')
    p.add_argument("command", nargs=argparse.REMAINDER)
    args = p.parse_args()
    if not 1 <= args.command_timeout <= 86400:
        p.error('Command timeout must be between 1 second and 24 hours')
    if args.bolt_port in {7687, 7688, 7689} or not 1024 < args.bolt_port < 65536:
        p.error("Choose a non-default development port")
    if not args.command:
        p.error("Supply the bounded command after --")
    if os.environ.get("SLURM_CLUSTER_NAME") and not os.environ.get("SLURM_JOB_ID"):
        p.error("Service requires an allocation on a Slurm cluster")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", args.bolt_port))
    root = args.root.resolve()
    if root.exists():
        p.error("Refusing existing directory; choose a fresh development root")
    home = args.home.resolve()
    if not (home / "bin/neo4j").is_file() or not (args.java_home / "bin/java").is_file():
        p.error("Neo4j/Java binaries unavailable")
    root.mkdir(parents=True, mode=0o700)
    for d in ["conf", "data", "logs", "run", "import", "plugins", "transactions"]:
        (root / d).mkdir()
    for name in ["server-logs.xml", "user-logs.xml"]:
        shutil.copyfile(home / "conf" / name, root / "conf" / name)
    conf = {
        "server.directories.data": root / "data",
        "server.directories.logs": root / "logs",
        "server.directories.run": root / "run",
        "server.directories.import": root / "import",
        "server.directories.plugins": root / "plugins",
        "server.directories.transaction.logs.root": root / "transactions",
        "server.default_listen_address": "127.0.0.1",
        "server.bolt.enabled": "true",
        "server.bolt.listen_address": f"127.0.0.1:{args.bolt_port}",
        "server.bolt.advertised_address": f"127.0.0.1:{args.bolt_port}",
        "server.http.enabled": "false", "server.https.enabled": "false",
        "dbms.security.auth_enabled": "true",
        "server.memory.heap.initial_size": "256m", "server.memory.heap.max_size": "512m",
        "server.memory.pagecache.size": "128m",
    }
    (root / "conf/neo4j.conf").write_text("".join(f"{k}={v}\n" for k, v in conf.items()))
    secret = secrets.token_urlsafe(30)
    fd = os.open(root / "password", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secret)
    env = {**os.environ, "NEO4J_HOME": str(home), "NEO4J_CONF": str(root / "conf"),
           "JAVA_HOME": str(args.java_home.resolve())}
    initialized = subprocess.run([str(home / "bin/neo4j-admin"), "dbms", "set-initial-password", secret],
                                 env=env, capture_output=True, text=True, timeout=30)
    if initialized.returncode:
        print(initialized.stderr.replace(secret, "[REDACTED]"), file=sys.stderr)
        return initialized.returncode
    manifest = {"purpose": "kdiff-m1-development", "root": str(root), "owner_id": str(uuid4()),
                "uri": f"bolt://127.0.0.1:{args.bolt_port}", "generation": str(uuid4()),
                "hostname": socket.gethostname(), "status": "starting",
                'job_id': os.environ.get('SLURM_JOB_ID'),
                'expires_at': time.time() + args.command_timeout + 180}
    service_path = root / "service.json"
    from kdiff.deployment.manifest import atomic_json
    atomic_json(service_path, manifest)
    from kdiff.core.graph import Graph
    log = (root / "logs/console.log").open("w")
    child = subprocess.Popen([str(home / "bin/neo4j"), "console"], env=env, stdout=log, stderr=subprocess.STDOUT)
    graph = None
    application = None
    interrupted_at = None
    def stop_application(signum, frame):
        nonlocal interrupted_at
        interrupted_at = interrupted_at or time.monotonic()
        if application and application.poll() is None:
            os.killpg(application.pid, signal.SIGTERM)
    previous = {sig: signal.signal(sig, stop_application) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1)}
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if interrupted_at:
                raise RuntimeError('Startup interrupted')
            if child.poll() is not None:
                raise RuntimeError(f"Owned Neo4j exited; inspect {root / 'logs/console.log'}")
            try:
                graph = Graph(service_path, starting=True)
                graph.driver.verify_connectivity()
                break
            except Exception:
                if graph:
                    graph.close()
                    graph = None
                time.sleep(0.5)
        if graph is None:
            raise RuntimeError("Owned Neo4j readiness timed out")
        graph.initialize_empty()
        graph.verify_owner()
        from kdiff.core.artifacts import ArtifactStore, exclusive_lock
        from kdiff.analysis.witness import load_release, freeze
        if args.postgres_artifacts:
            from kdiff.core.durable import PostgresArtifacts
            store = PostgresArtifacts()
        else:
            store = ArtifactStore(args.store)
        if args.restore:
            graph.restore_empty(load_release(store, args.restore))
        version = graph.read("CALL dbms.components() YIELD name, versions, edition RETURN name, versions, edition")
        manifest.update(status="ready", pid=child.pid, components=version)
        atomic_json(service_path, manifest)
        command = args.command[1:] if args.command[0] == "--" else args.command
        print(f"Owned Neo4j ready: {service_path}", flush=True)
        application = subprocess.Popen(command, env={**env, "KDIFF_SERVICE": str(service_path)}, start_new_session=True)
        deadline = time.monotonic() + args.command_timeout
        while application.poll() is None:
            if time.monotonic() >= deadline and interrupted_at is None:
                stop_application(signal.SIGTERM, None)
            if interrupted_at and time.monotonic() - interrupted_at >= 15:
                os.killpg(application.pid, signal.SIGKILL)
            time.sleep(0.2)
        application.wait()
        if args.checkpoint_namespace and graph.has_namespace(args.checkpoint_namespace):
            from kdiff.cli import code_digest
            with exclusive_lock(root / 'application.lock'):
                release_id = freeze(graph, store, args.checkpoint_namespace, code_digest())
            atomic_json(root / 'checkpoint.json', {'release_id': release_id, 'store': str(args.store.resolve()),
                'generation': manifest['generation'], 'application_exit': application.returncode,
                'interrupted': interrupted_at is not None})
        return 128 + signal.SIGTERM if interrupted_at else application.returncode
    finally:
        if application and application.poll() is None:
            os.killpg(application.pid, signal.SIGTERM)
            try:
                application.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(application.pid, signal.SIGKILL)
                application.wait()
        if graph:
            graph.close()
        # Popen handle is our child; never search for or stop other database PIDs.
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
        log.close()
        manifest["status"] = "stopped"
        atomic_json(service_path, manifest)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
