"""Allowlisted Neo4j operations for an owned, isolated development store."""

import json
import os
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit

from neo4j import GraphDatabase, Query, unit_of_work

from kdiff.construction.openalex import validate_batch
from .contracts import Batch, canonical, digest
from .schema import EntityType, RELATIONS

MAX_EXPORT = 20_000


def load_service(path: Path, *, starting=False) -> dict:
    meta = json.loads(path.read_text())
    uri = urlsplit(meta["uri"])
    # M1 deliberately cannot be pointed at the known active/default services.
    if uri.scheme != "bolt" or uri.hostname != "127.0.0.1" or uri.port in {7687, 7688, 7689}:
        raise ValueError("M1 requires an isolated loopback development service")
    if not meta.get("owner_id") or meta.get("purpose") != "kdiff-m1-development":
        raise ValueError("Missing owned development service manifest")
    if meta.get('hostname') != socket.gethostname():
        raise ValueError('Loopback service belongs to another host')
    if not meta.get('generation') or meta.get('expires_at', 0) <= time.time():
        raise ValueError('Expired service generation')
    if meta.get('status') not in ({'starting', 'ready'} if starting else {'ready'}):
        raise ValueError('Service is not ready')
    root = Path(meta["root"]).resolve()
    if path.resolve() != root / "service.json":
        raise ValueError("Service manifest does not belong to its root")
    secret = root / "password"
    if secret.stat().st_mode & 0o077:
        raise ValueError("Development secret must be restricted to its owner")
    return meta


class Graph:
    def __init__(self, service: Path, *, starting=False):
        self.meta = load_service(service, starting=starting)
        self.service_path = service
        self.starting = starting
        secret = (Path(self.meta["root"]) / "password").read_text().strip()
        self.driver = GraphDatabase.driver(self.meta["uri"], auth=("neo4j", secret),
                                          connection_timeout=5, max_transaction_retry_time=5,
                                          max_connection_pool_size=2)

    def close(self):
        self.driver.close()

    def read(self, cypher, **params):
        with self.driver.session(database="neo4j", default_access_mode="READ") as s:
            return [r.data() for r in s.run(Query(cypher, timeout=30), **params)]

    def verify_owner(self):
        current = load_service(self.service_path, starting=self.starting)
        if current['generation'] != self.meta['generation']:
            raise ValueError('Service generation changed')
        rows = self.read("MATCH (n:KDStore) RETURN n.owner_id AS owner")
        if rows != [{"owner": self.meta["owner_id"]}]:
            raise ValueError("Database ownership mismatch; refusing access")

    def initialize_empty(self):
        """Only the launcher may initialize a newly created empty store."""
        if self.read("MATCH (n) RETURN count(n) AS count")[0]["count"] != 0:
            raise ValueError("Refusing to initialize a populated graph")
        with self.driver.session(database="neo4j") as s:
            s.run("CREATE (:KDStore {owner_id: $owner})", owner=self.meta["owner_id"]).consume()
            for label, prop in [("KDEntity", "canonical_id"), ("KDObservation", "observation_id"),
                                ("KDIdentity", "mapping_id"), ("KDSource", "sha256"), ("KDBatch", "batch_id")]:
                s.run(f"CREATE CONSTRAINT {label}_unique IF NOT EXISTS FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE").consume()

    def integrate(self, batch: Batch) -> dict:
        self.verify_owner()
        validate_batch(batch)
        body = batch.model_dump(mode="json")
        batch_id = digest(body)
        if self.read("MATCH (b:KDBatch {batch_id: $id}) RETURN b.batch_id AS id", id=batch_id):
            return {"batch_id": batch_id, "reused": True, "counts": self.counts()}

        @unit_of_work(timeout=30)
        def write(tx):
            self._coordinate(tx)
            if tx.run('MATCH (b:KDBatch {batch_id:$id}) RETURN b.batch_id AS id', id=batch_id).single():
                return True
            for e in body["entities"]:
                if e["kind"] not in {x.value for x in EntityType}:
                    raise ValueError("Unregistered label")
                tx.run(f"MERGE (n:KDEntity:{e['kind']} {{canonical_id: $id}}) "
                       "ON CREATE SET n.payload=$payload, n.namespace=$namespace",
                       id=e["canonical_id"], payload=canonical(e).decode(), namespace=e["namespace"]).consume()
            for collection, label, key in [("observations", "KDObservation", "observation_id"),
                                           ("identities", "KDIdentity", "mapping_id"),
                                           ("sources", "KDSource", "sha256")]:
                rows = [{"id": digest([batch.namespace, r[key]]) if collection == "sources" else r[key],
                         "payload": canonical(r).decode()} for r in body[collection]]
                tx.run(f"UNWIND $rows AS row MERGE (n:{label} {{{key}: row.id}}) "
                       "ON CREATE SET n.payload=row.payload, n.namespace=$namespace",
                       rows=rows, namespace=batch.namespace).consume()
            for relation in sorted({a["relation"] for a in body["assertions"]}):
                if relation not in RELATIONS:
                    raise ValueError("Unregistered relation")
                rows = [{"head": a["head_id"], "tail": a["tail_id"], "id": a["assertion_id"],
                         "lower": a["valid_time"]["lower"], "upper": a["valid_time"]["upper"],
                         "payload": canonical(a).decode()} for a in body["assertions"] if a["relation"] == relation]
                tx.run(f"UNWIND $rows AS row MATCH (h:KDEntity {{canonical_id: row.head}}), "
                       f"(t:KDEntity {{canonical_id: row.tail}}) MERGE (h)-[r:{relation} {{assertion_id: row.id}}]->(t) "
                       "ON CREATE SET r.payload=row.payload, r.lower=row.lower, r.upper=row.upper, r.namespace=$namespace",
                       rows=rows, namespace=batch.namespace).consume()
            tx.run("CREATE (:KDBatch {batch_id: $id, namespace:$namespace, payload:$payload})",
                   id=batch_id, namespace=batch.namespace,
                   payload=canonical({"batch_id": batch_id, "namespace": batch.namespace,
                                      "source_hashes": sorted(s["sha256"] for s in body["sources"])}).decode()).consume()
            return False
        with self.driver.session(database="neo4j") as s:
            reused = s.execute_write(write)
        return {"batch_id": batch_id, "reused": reused, "counts": self.counts()}

    def _coordinate(self, tx):
        # Writers and exports acquire the same server-side lock. This also waits
        # for an in-flight commit after a killed client releases its file lock.
        row = tx.run('MATCH (s:KDStore {owner_id:$owner}) '
            'SET s.coordination_epoch=coalesce(s.coordination_epoch,0)+1 '
            'RETURN s.owner_id AS owner', owner=self.meta['owner_id']).single()
        if row is None:
            raise ValueError('Database ownership changed during transaction')

    def counts(self):
        return {"entities": self.read("MATCH (n:KDEntity) RETURN count(n) AS n")[0]["n"],
                "assertions": self.read("MATCH ()-[r]->() WHERE r.assertion_id IS NOT NULL RETURN count(r) AS n")[0]["n"]}

    def restore_empty(self, release):
        """Restore a verified logical release only into a freshly initialized store.

        The launcher publishes readiness only after records and batch receipts
        are restored and their original logical hash has been verified.
        """
        self.verify_owner()
        if self.read('MATCH (n) WHERE NOT n:KDStore RETURN count(n) AS n')[0]['n']:
            raise ValueError('Logical restore requires an empty owned database')
        exported = release['graph']
        if digest(exported) != release['graph_hash']:
            raise ValueError('Restore graph hash mismatch')
        batch = Batch.model_validate({'namespace': release['namespace'],
                                     **{k: v for k, v in exported.items() if k != 'batches'}})
        validate_batch(batch)
        # This method is only called before publishing readiness. A failed
        # restore is never exposed as a ready service or reused as an empty one.
        self.integrate(batch)
        with self.driver.session(database='neo4j') as session:
            @unit_of_work(timeout=30)
            def catalog(tx):
                self._coordinate(tx)
                tx.run('MATCH (b:KDBatch) DELETE b').consume()
                tx.run('UNWIND $rows AS row CREATE (:KDBatch {batch_id:row.id, namespace:$namespace, payload:row.payload})',
                       rows=[{'id': b['batch_id'], 'payload': canonical(b).decode()} for b in exported['batches']],
                       namespace=release['namespace']).consume()
            session.execute_write(catalog)
        if digest(self.export(release['namespace'])) != release['graph_hash']:
            raise ValueError('Restored graph does not match the frozen release')
        return {'status': 'restored', 'graph_hash': release['graph_hash'], 'counts': self.counts()}

    def has_namespace(self, namespace):
        self.verify_owner()
        return bool(self.read('MATCH (b:KDBatch {namespace:$namespace}) RETURN b.batch_id AS id LIMIT 1', namespace=namespace))

    def export(self, namespace):
        self.verify_owner()
        with self.driver.session(database='neo4j') as session:
            @unit_of_work(timeout=30)
            def snapshot(tx):
                self._coordinate(tx)
                def read(query, **params):
                    return [row.data() for row in tx.run(query, **params)]
                return self._export(namespace, read)
            return session.execute_write(snapshot)

    def _export(self, namespace, read):
        output = {}
        for key, label, id_key in [("entities", "KDEntity", "canonical_id"),
                                   ("observations", "KDObservation", "observation_id"),
                                   ("identities", "KDIdentity", "mapping_id"),
                                   ("sources", "KDSource", "sha256"), ("batches", "KDBatch", "batch_id")]:
            rows = read(f"MATCH (n:{label} {{namespace: $namespace}}) RETURN n.payload AS payload, properties(n) AS props, labels(n) AS labels "
                             f"ORDER BY n.{id_key} LIMIT $limit", namespace=namespace, limit=MAX_EXPORT + 1)
            if len(rows) > MAX_EXPORT:
                raise ValueError("Export limit exceeded; snapshot not created")
            output[key] = []
            for row in rows:
                value = json.loads(row["payload"])
                expected_id = digest([namespace, value[id_key]]) if key == "sources" else value[id_key]
                if row["props"][id_key] != expected_id:
                    raise ValueError("Graph identifier and stored record disagree")
                if key == "entities" and value["kind"] not in row["labels"]:
                    raise ValueError("Graph entity type and stored record disagree")
                output[key].append(value)
        rows = read("MATCH (h)-[r]->(t) WHERE r.namespace=$namespace AND r.assertion_id IS NOT NULL "
                         "RETURN r.payload AS payload, properties(r) AS props, type(r) AS relation, "
                         "h.canonical_id AS head, t.canonical_id AS tail ORDER BY r.assertion_id LIMIT $limit",
                         namespace=namespace, limit=MAX_EXPORT + 1)
        if len(rows) > MAX_EXPORT or sum(map(len, output.values())) + len(rows) > MAX_EXPORT:
            raise ValueError("Export limit exceeded; snapshot not created")
        output["assertions"] = []
        for row in rows:
            a = json.loads(row["payload"])
            if (row["head"] != a["head_id"] or row["tail"] != a["tail_id"] or row["relation"] != a["relation"]
                    or row["props"]["assertion_id"] != a["assertion_id"]
                    or row["props"].get("lower") != a["valid_time"]["lower"]
                    or row["props"].get("upper") != a["valid_time"]["upper"]):
                raise ValueError("Graph fact and frozen record disagree")
            output["assertions"].append(a)
        if not output["batches"]:
            raise ValueError("No imported sources in requested namespace")
        return output

    def count_papers(self, request):
        # Query is fixed by code. Only parameters come from the typed program.
        return self.read(COUNT_CYPHER, author=request.author_id,
                         start=request.window.start.isoformat(), end=request.window.end.isoformat())[0]


COUNT_CYPHER = """MATCH (a:KDEntity:Author {canonical_id: $author})-[r:authorOf]->(p:KDEntity:Paper)
WHERE r.lower >= $start AND r.upper <= $end
RETURN count(DISTINCT p.canonical_id) AS count,
       collect(DISTINCT p.canonical_id) AS paper_ids,
       collect(DISTINCT r.assertion_id) AS assertion_ids"""
