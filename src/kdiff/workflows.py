import subprocess
from pathlib import Path

from kdiff.agents import AgentRun
from kdiff.analysis.program import CountProgram, count_program
from kdiff.analysis.witness import (InsufficientEvidence, count_from_release, load_release, make_witness,
                                    render_count, replay)
from kdiff.construction.openalex import capture_local, parse_capture, validate_batch
from kdiff.core.contracts import Batch, CountRequest, Label, digest


async def build(graph, store, profile, allow_mock, path: Path, namespace: str,
                source='openalex', input_format='jsonl', columns=None, ledger=None, integration_lease=None,
                use_cache=False, extraction_cache=None):
    if profile.profile == "mock" and not namespace.startswith("fixture:"):
        raise ValueError("Mock build is restricted to an explicit fixture namespace")
    run = AgentRun(store, profile, allow_mock, namespace)
    cache = None
    if use_cache:
        if ledger is None:
            raise ValueError('Extraction caching requires a durable task ledger')
        from kdiff.core.cache import ExtractionCache
        cache = extraction_cache or ExtractionCache(store, ledger)

    def scope_source(source_path: str, source_namespace: str) -> dict:
        """Approve the exact bounded local file and namespace, without expansion."""
        if source_path != str(path) or source_namespace != namespace:
            raise ValueError("Tool request exceeds authorized source scope")
        return {"source_path": source_path, "namespace": namespace, "max_records": 1000, "max_bytes": 16777216}

    def ingest_source(scope_id: str) -> dict:
        """Capture the scoped native source file as immutable raw artifacts."""
        scope = store.get(scope_id)
        if scope != expected_scope:
            raise ValueError("Unknown source scope")
        if source == 'openalex':
            return capture_local(path, store, namespace)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16777216:
            raise ValueError('Source unavailable or input budget exceeded')
        return {**store.capture(path.read_bytes()), 'namespace': namespace, 'source': source,
                'format': input_format, 'suffix': '.gz' if path.suffix == '.gz' else ''}

    def extract_records(capture_id: str) -> dict:
        """Parse structured records and retain JSON paths and source hashes."""
        if capture_id != expected_capture:
            raise ValueError("Unknown capture handle")
        capture = store.get(capture_id)
        from kdiff.core.cache import extraction_key
        from kdiff.core.schema import SCHEMA_VERSION, IDENTITY_VERSION
        prompt = Path(__file__).parent / 'prompts/construction/Extraction.txt'
        from kdiff.cli import code_digest
        key = extraction_key(raw_hash=digest(capture), parser=digest([source, input_format, columns, code_digest()]),
            prompt_hash=digest(prompt.read_text()), model_revision=profile.runtime_revision or profile.model,
            schema_version=SCHEMA_VERSION, identity_version=IDENTITY_VERSION)
        cached = cache.get(key) if cache else None
        if cached is not None:
            validate_batch(Batch.model_validate(cached))
            return cached
        if source == 'openalex':
            result = parse_capture(capture, store).model_dump(mode="json")
            return cache.put(key, result) if cache else result
        import tempfile
        from kdiff.construction.sources import parse_source
        with tempfile.TemporaryDirectory(prefix='kdiff-captured-source-') as directory:
            saved = Path(directory) / ('source' + capture['suffix'])
            saved.write_bytes(store.get_bytes(capture['sha256']))
            parsed, report = parse_source(saved, store, namespace, source, format=input_format, columns=columns)
            store.put(report)
            result = parsed.model_dump(mode='json')
            return cache.put(key, result) if cache else result

    def resolve_source_ids(batch_id: str) -> dict:
        """Validate exact source-ID mappings; never merge people on names."""
        if batch_id != expected_batch:
            raise ValueError("Unknown batch handle")
        batch = validate_batch(Batch.model_validate(store.get(batch_id)))
        from kdiff.construction.resolution import resolve_identifiers
        existing = graph.export(namespace) if graph.has_namespace(namespace) else {}
        batch, decisions = resolve_identifiers(batch, existing)
        store.put({'kind': 'resolution_decisions', 'decisions': decisions})
        return batch.model_dump(mode="json")

    def integrate_records(batch_id: str) -> dict:
        """Write the validated batch using the coordinated Neo4j writer."""
        if batch_id != expected_resolved:
            raise ValueError("Unknown resolved batch handle")
        batch=Batch.model_validate(store.get(batch_id))
        if ledger is None:
            return graph.integrate(batch)
        if integration_lease is not None:
            ledger.prepare(integration_lease, batch_id)
            result = ledger.integrate(integration_lease, batch_id, lambda: graph.integrate(batch))
            return {**result, 'task_id': integration_lease['task_id']}
        task_id=ledger.enqueue({'kind':'graph-integration','batch_id':batch_id,
                               'namespace':namespace,'schema':batch.schema_version})
        prior=ledger.lookup(task_id)
        if prior['state']=='done':
            return {**prior['result'],'reused':True,'task_id':task_id}
        lease=ledger.claim(run.run_id,ttl=300,task_id=task_id)
        if lease is None:
            raise ValueError('Integration task is leased or retry budget exhausted')
        ledger.prepare(lease,batch_id)
        result=ledger.integrate(lease,batch_id,lambda:graph.integrate(batch))
        return {**result,'task_id':task_id}

    try:
        expected_scope = await run.turn("Orchestrator", scope_source, {"source_path": str(path), "source_namespace": namespace})
        scope_id = store.put(expected_scope)
        capture = await run.turn("Ingestion", ingest_source, {"scope_id": scope_id}, [scope_id])
        expected_capture = store.put(capture)
        batch = await run.turn("Extraction", extract_records, {"capture_id": expected_capture}, [expected_capture])
        expected_batch = store.put(batch)
        resolved = await run.turn("Disambiguation", resolve_source_ids, {"batch_id": expected_batch}, [expected_batch])
        expected_resolved = store.put(resolved)
        outcome = await run.turn("Integration", integrate_records, {"batch_id": expected_resolved}, [expected_resolved])
        return {**outcome, "run": run.save(outcome), 'cache': cache.hits if cache else None}
    except Exception as exc:
        run.save({"status": "failed", "error_type": type(exc).__name__})
        raise


async def ask(graph, store, profile, allow_mock, release_id: str, request: CountRequest):
    run = AgentRun(store, profile, allow_mock, release_id)
    release = load_release(store, release_id)
    if profile.profile == "mock" and not release["synthetic"]:
        raise ValueError("Mock analysis is restricted to synthetic releases")
    if digest(graph.export(release["namespace"])) != release["graph_hash"]:
        raise ValueError("Working graph changed; create a new release before new analysis")

    def resolve_entity(author_id: str) -> dict:
        """Resolve only the requested canonical source identity in this frozen release."""
        if author_id != request.author_id:
            raise ValueError("Cannot change requested author")
        matches = [e for e in release["graph"]["entities"] if e["canonical_id"] == author_id and e["kind"] == "Author"]
        if len(matches) != 1:
            raise InsufficientEvidence("Identity unavailable; supply a canonical Author ID")
        return {"author_id": author_id, "release_id": release_id, "identity_policy": "exact_source_id"}

    def plan_count(count_request: CountRequest) -> dict:
        """Validate and compile the explicit author-count request into a typed program."""
        if count_request != request:
            raise ValueError("Planner changed the requested scope")
        return count_program(count_request, release_id).model_dump(mode="json")

    def retrieve_count(program_id: str) -> dict:
        """Execute fixed parameterized Cypher and compare with complete frozen operands."""
        if program_id != expected_program:
            raise ValueError("Unknown program")
        program = CountProgram.model_validate(store.get(program_id))
        computed = count_from_release(release, program.request)
        actual = graph.count_papers(program.request)
        if actual["count"] != computed["count"] or sorted(actual["paper_ids"]) != computed["paper_ids"]:
            raise ValueError("Neo4j and replay disagree")
        return computed

    def draft_count(evidence_id: str) -> dict:
        """Create an atomic count candidate from tool evidence only."""
        if evidence_id != expected_evidence:
            raise ValueError("Unknown evidence")
        evidence = store.get(evidence_id)
        return {"claim_id": digest([release_id, request.model_dump(mode="json"), evidence_id]),
                "predicate": "recorded_paper_count", "value": evidence["count"], "evidence_id": evidence_id}

    def verify_count(claim_id: str) -> dict:
        """Recompute count from existing evidence; cannot fetch or add graph facts."""
        if claim_id != expected_claim:
            raise ValueError("Unknown candidate")
        candidate = store.get(claim_id)
        computed = count_from_release(release, request)
        label = Label.SUPPORTED if candidate["value"] == computed["count"] else Label.CONTRADICTED
        return {**candidate, "label": label.value, "original_label": label.value, "rewrite": None}

    def assign_witness(verified_id: str) -> dict:
        """Assign every aggregate operand and re-verify the claim-local witness."""
        if verified_id != expected_verified:
            raise ValueError("Unknown verified candidate")
        witness = make_witness(release_id, CountProgram.model_validate(store.get(expected_program)),
                               store.get(expected_evidence), store.get(verified_id))
        witness_id = store.put(witness)
        checked = replay(store, witness_id)
        return {"witness_id": witness_id, "verification": checked}

    def synthesize_count(witness_id: str) -> dict:
        """Render the verified count using the constrained M1 sentence template."""
        if witness_id != expected_witness:
            raise ValueError("Unknown witness assignment")
        checked = replay(store, witness_id)
        return {"type": "Answer", "text": checked["text"], "witnesses": [witness_id],
                "claim_to_witness": {store.get(witness_id)["claim_id"]: witness_id},
                "release_id": release_id, "post_write_check": "exact_verified_template", "label": Label.SUPPORTED.value,
                "synthetic": release["synthetic"]}

    try:
        await run.turn("Manager", resolve_entity, {"author_id": request.author_id}, [release_id])
        # One planner repair, then fail closed; no unrestricted fallback.
        for attempt in range(2):
            try:
                program = await run.turn("Planner", plan_count, {"count_request": request.model_dump(mode="json")}, [release_id])
                break
            except ValueError:
                if attempt:
                    raise
        expected_program = store.put(program)
        evidence = await run.turn("Retriever", retrieve_count, {"program_id": expected_program}, [expected_program])
        expected_evidence = store.put(evidence)
        candidate = await run.turn("Drafting_Bridge", draft_count, {"evidence_id": expected_evidence}, [expected_evidence])
        expected_claim = store.put(candidate)
        verified = await run.turn("Verifier", verify_count, {"claim_id": expected_claim}, [expected_claim, expected_evidence])
        expected_verified = store.put(verified)
        assigned = await run.turn("Evidence_Builder", assign_witness, {"verified_id": expected_verified}, [expected_verified])
        expected_witness = assigned["witness_id"]
        outcome = await run.turn("Synthesizer", synthesize_count, {"witness_id": expected_witness}, [expected_witness])
    except Exception as exc:
        outcome = {"type": "Abstain", "reason": type(exc).__name__,
                   "detail": "The requested count could not be established; inspect failed tool receipts.", "witnesses": []}
    return {**outcome, "run": run.save(outcome)}
