#!/usr/bin/env python3
"""Launch a fresh isolated Neo4j, run one bounded command, stop only our child."""

import argparse
import json
import os
import secrets
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
    p.add_argument("command", nargs=argparse.REMAINDER)
    args = p.parse_args()
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
                "hostname": socket.gethostname(), "status": "starting"}
    service_path = root / "service.json"
    service_path.write_text(json.dumps(manifest, indent=2))
    from kdiff.core.graph import Graph
    log = (root / "logs/console.log").open("w")
    child = subprocess.Popen([str(home / "bin/neo4j"), "console"], env=env, stdout=log, stderr=subprocess.STDOUT)
    graph = None
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError(f"Owned Neo4j exited; inspect {root / 'logs/console.log'}")
            try:
                graph = Graph(service_path)
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
        version = graph.read("CALL dbms.components() YIELD name, versions, edition RETURN name, versions, edition")
        manifest.update(status="ready", pid=child.pid, components=version)
        service_path.write_text(json.dumps(manifest, indent=2))
        command = args.command[1:] if args.command[0] == "--" else args.command
        print(f"Owned Neo4j ready: {service_path}", flush=True)
        result = subprocess.run(command, env={**env, "KDIFF_SERVICE": str(service_path)}, timeout=600)
        return result.returncode
    finally:
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
        service_path.write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
