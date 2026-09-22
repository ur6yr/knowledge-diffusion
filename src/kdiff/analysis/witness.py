"""Frozen releases and deterministic, network-free count witness replay."""

from kdiff.core.artifacts import ArtifactStore
from kdiff.core.contracts import Assertion, CountRequest, Label, digest, membership, now
from kdiff.core.schema import IDENTITY_VERSION, SCHEMA_VERSION
from .program import CountProgram

REPLAY_VERSION = "author-count-replay-v1"
PROOF_OBLIGATIONS = ["resolved_source_identity", "inclusive_window", "all_authorship_assertions",
                     "distinct_canonical_papers", "source_provenance", "complete_query"]


class InsufficientEvidence(ValueError):
    pass


def freeze(graph, store: ArtifactStore, namespace: str, code_revision: str) -> str:
    # Caller must hold the same exclusive lock as construction and ask.
    exported = graph.export(namespace)
    for source in exported["sources"]:
        store.get_bytes(source["sha256"])
    release = {"kind": "kdiff-logical-release-v1", "namespace": namespace,
               "graph": exported, "graph_hash": digest(exported), "created_at": now(),
               "schema_version": SCHEMA_VERSION, "identity_map_version": IDENTITY_VERSION,
               "code_revision": code_revision, "replay_version": REPLAY_VERSION,
               "coverage": "Complete only for the supplied imported records; not a population census",
               "counts": {k: len(v) for k, v in exported.items()},
               "synthetic": namespace.startswith("fixture:")}
    return store.put(release)


def load_release(store: ArtifactStore, release_id: str):
    release = store.get(release_id)
    if release["kind"] != "kdiff-logical-release-v1" or release["replay_version"] != REPLAY_VERSION:
        raise ValueError("Unsupported release/replay version")
    if digest(release["graph"]) != release["graph_hash"]:
        raise ValueError("Frozen graph hash mismatch")
    sources = {s["sha256"] for s in release["graph"]["sources"]}
    for sha in sources:
        store.get_bytes(sha)
    for record in release["graph"]["observations"] + release["graph"]["assertions"]:
        if record["provenance"]["raw_artifact_pointer"] not in sources:
            raise ValueError("Missing source provenance operand")
    return release


def count_from_release(release: dict, request: CountRequest):
    entities = {e["canonical_id"]: e for e in release["graph"]["entities"]}
    if entities.get(request.author_id, {}).get("kind") != "Author":
        raise InsufficientEvidence("No Author with the requested canonical ID; not a zero count")
    by_paper = {}
    operands = []
    for row in release["graph"]["assertions"]:
        a = Assertion.model_validate(row)
        if a.relation != "authorOf" or a.head_id != request.author_id:
            continue
        if entities.get(a.tail_id, {}).get("kind") != "Paper":
            raise InsufficientEvidence("Missing Paper endpoint")
        by_paper.setdefault(a.tail_id, set()).add(membership(a.valid_time, request.window))
        operands.append(a.assertion_id)
    papers = []
    for paper, statuses in by_paper.items():
        if "indeterminate" in statuses or len(statuses) > 1:
            raise InsufficientEvidence("Unknown, imprecise boundary or conflicting publication dates affect exact count")
        if statuses == {"inside"}:
            papers.append(paper)
    return {"count": len(papers), "paper_ids": sorted(papers), "input_assertion_ids": sorted(operands),
            "policy": request.policy, "complete": True,
            "scope": "Recorded source-entity authorships in the frozen release only"}


def make_witness(release_id, program: CountProgram, evidence, claim):
    if claim["label"] != Label.SUPPORTED or claim["value"] != evidence["count"]:
        raise ValueError("Unverified claim cannot receive a witness")
    return {"kind": "aggregate_bundle", "release_id": release_id,
            "replay_version": REPLAY_VERSION, "claim_id": claim["claim_id"],
            "program": program.model_dump(mode="json"), "result": evidence,
            "proof_obligations": PROOF_OBLIGATIONS,
            "claim": claim}


def render_count(claim, request: CountRequest):
    return (f"The frozen release records {claim['value']} distinct papers for source identity "
            f"{request.author_id} published within {request.window.start.isoformat()}–"
            f"{request.window.end.isoformat()} (inclusive). This is a count within the imported scope.")


def replay(store: ArtifactStore, witness_id: str):
    witness = store.get(witness_id)
    if witness["kind"] != "aggregate_bundle" or witness["replay_version"] != REPLAY_VERSION:
        raise ValueError("Unsupported witness format")
    program = CountProgram.model_validate(witness["program"])
    if witness["release_id"] != program.release_id:
        raise ValueError("Program and witness release mismatch")
    release = load_release(store, witness["release_id"])
    result = count_from_release(release, program.request)
    if result != witness["result"]:
        raise ValueError("Witness result/complete operand set does not replay")
    claim = witness["claim"]
    evidence_id = digest(result)
    expected_claim = {"claim_id": digest([program.release_id, program.request.model_dump(mode="json"), evidence_id]),
                      "predicate": "recorded_paper_count", "value": result["count"], "evidence_id": evidence_id,
                      "label": Label.SUPPORTED.value, "original_label": Label.SUPPORTED.value, "rewrite": None}
    if claim != expected_claim or claim["claim_id"] != witness["claim_id"]:
        raise ValueError("Claim is not supported by recomputation")
    if witness["proof_obligations"] != PROOF_OBLIGATIONS:
        raise ValueError("Incomplete proof obligations")
    return {"status": "verified", "witness_id": witness_id, "release_id": witness["release_id"],
            "count": result["count"], "input_assertions": len(result["input_assertion_ids"]),
            "text": render_count(claim, program.request)}
