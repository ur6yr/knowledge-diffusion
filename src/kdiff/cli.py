import argparse
import asyncio
import importlib.metadata
import json
import os
import sys
from contextlib import nullcontext
from pathlib import Path
from pydantic import ValidationError

from kdiff.analysis.program import OPERATORS
from kdiff.analysis.witness import freeze, replay
from kdiff.core.artifacts import ArtifactStore, exclusive_lock
from kdiff.core.contracts import CountRequest, Window, digest
from kdiff.core.graph import Graph
from kdiff.inference.client import Provider
from kdiff.workflows import ask, build


def code_digest():
    root = Path(__file__).parent
    files = [*root.rglob('*.py'), *(root / 'prompts').rglob('*.txt')]
    return digest({str(p.relative_to(root)): p.read_text() for p in sorted(files)})


def doctor():
    versions = {}
    for name in ["autogen-agentchat", "autogen-core", "autogen-ext", "neo4j", "pydantic", "openai"]:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not_installed"
    return {"application": "Bounded temporal knowledge graph construction and analysis", "python": sys.version.split()[0], "versions": versions,
            "openai_key": "configured" if os.environ.get("OPENAI_API_KEY") else "not_configured",
            "slurm_allocation": bool(os.environ.get("SLURM_JOB_ID")),
            "live_profiles": "Require explicit authorization, budget and matching capability report",
            "operators": OPERATORS,
            "hardware_validation": "Consult docs/STATUS.md for actual execution evidence"}


def source_inventory():
    return {name: {"implementation": {"OpenAlex": "local native JSONL/JSONL.gz", "ORCID": "local public v3 JSON",
                   "ROR": "local v2 JSON", "USPTO": "bounded bulk XML", "PatCit": "versioned CSV/JSON column mapping",
                   "targeted_web": "public HTTPS capture and exact-span validation"}[name],
                   "live_status": "not_configured"}
            for name in ["OpenAlex", "ORCID", "ROR", "USPTO", "PatCit", "targeted_web"]}


def main(argv=None):
    p = argparse.ArgumentParser(description="AutoGen temporal graph construction, analysis and replay")
    p.add_argument("--store", type=Path, default=Path("artifacts/store"))
    p.add_argument("--service", type=Path, default=os.environ.get("KDIFF_SERVICE"))
    p.add_argument('--postgres-artifacts', action='store_true', help='Use KDIFF_POSTGRES_DSN for initialized JSONB artifacts')
    p.add_argument('--ledger', type=Path, default=Path('artifacts/tasks.sqlite'))
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    preflight = commands.add_parser('preflight', help='Read-only allocation, software and path inventory')
    preflight.add_argument('--path', action='append', default=[])
    preflight.add_argument('--gpu', action='store_true')
    commands.add_parser("sources")
    probe = commands.add_parser('provider-check', help='Run explicitly authorized live capability round trips')
    probe.add_argument('--profile', type=Path, required=True)
    probe.add_argument('--output', type=Path, required=True)
    capture = commands.add_parser('capture-web', help='Capture one explicitly allowlisted public document')
    capture.add_argument('--url', required=True)
    capture.add_argument('--policy', type=Path, required=True)
    web = commands.add_parser('build-web', help='Extract a captured document through five AutoGen roles')
    web.add_argument('--document', required=True)
    web.add_argument('--namespace', required=True)
    web.add_argument('--fixture-extraction', type=Path)
    queued = commands.add_parser('queue-build', help='Queue a versioned manifest of bounded source files')
    queued.add_argument('--manifest', type=Path, required=True)
    worker = commands.add_parser('worker', help='Process bounded construction tasks under the coordinated writer')
    worker.add_argument('--max-tasks', type=int, default=1)
    worker.add_argument('--redis', action='store_true')
    worker.add_argument('--extraction-cache', action='store_true', help='Opt into measured extraction caching')
    seed = commands.add_parser('seed', help='Import a bounded versioned discovery cohort')
    seed.add_argument('--input', type=Path, required=True)
    seed.add_argument('--release', required=True)
    seed.add_argument('--name-column', required=True)
    seed.add_argument('--id-column')
    seed.add_argument('--sheet', help='Required worksheet name for XLSX input')
    seed.add_argument('--limit', type=int, default=1000, help='Explicit XLSX prefix scope, at most 1000 rows')
    commands.add_parser('tasks', help='Inspect the durable development task ledger')
    state = commands.add_parser('init-state', help='Initialize dedicated application artifact/task tables')
    state.add_argument('--postgres', action='store_true')
    b = commands.add_parser("build", help="Bounded source construction through five AutoGen roles")
    b.add_argument("--input", type=Path, required=True)
    b.add_argument("--namespace", required=True)
    b.add_argument('--source', choices=['openalex', 'orcid', 'ror', 'uspto', 'patcit'], default='openalex')
    b.add_argument('--format', choices=['jsonl', 'json', 'json-array', 'csv', 'xml'], default='jsonl')
    b.add_argument('--columns', type=Path, help='PatCit release column mapping JSON')
    b.add_argument('--extraction-cache', action='store_true', help='Opt into measured extraction caching')
    a = commands.add_parser("ask", help="Typed temporal question with claim-local witnesses")
    a.add_argument("--release", required=True)
    a.add_argument("--author")
    a.add_argument("--start")
    a.add_argument("--end")
    a.add_argument("--reference-date")
    a.add_argument('--request', type=Path, help='Typed multi-operator analysis request JSON')
    a.add_argument('--question', help='Natural-language question, with explicit window flags and a live profile')
    a.add_argument('--before-start')
    a.add_argument('--before-end')
    chat = commands.add_parser('chat', help='One persisted dialogue turn against a pinned release')
    chat.add_argument('--request', type=Path, required=True)
    chat.add_argument('--release', required=True)
    chat.add_argument('--dialogue', required=True)
    for sub in [b, a, chat, web, queued, worker]:
        sub.add_argument("--profile", type=Path, required=True)
        sub.add_argument("--allow-mock", action="store_true")
    s = commands.add_parser("snapshot", help="Freeze a complete bounded export under the writer lock")
    s.add_argument("--namespace", required=True)
    inspect = commands.add_parser('inspect', help='Inspect a saved release and its canonical identity catalog')
    inspect.add_argument('--release', required=True)
    r = commands.add_parser("replay", help="Replay saved complete operands without graph, network or model")
    group = r.add_mutually_exclusive_group(required=True)
    group.add_argument("--witness")
    group.add_argument("--run")
    for name in ['reconcile', 'maintain']:
        command = commands.add_parser(name)
        command.add_argument('--namespace', required=True)
    benchmark = commands.add_parser('benchmark-build', help='Import supplied original or separately labeled benchmark JSONL')
    benchmark.add_argument('--input', type=Path, required=True)
    benchmark.add_argument('--name', required=True)
    benchmark.add_argument('--origin', choices=['original', 'synthetic', 'independent'], required=True)
    evaluate = commands.add_parser('evaluate', help='Score stored runs using independent annotations')
    evaluate.add_argument('--benchmark', required=True)
    evaluate.add_argument('--annotations', type=Path, required=True)
    evaluate.add_argument('--compare', nargs=2, metavar=('LEFT_VARIANT', 'RIGHT_VARIANT'))
    evaluate.add_argument('--bootstrap-seed', type=int, default=2026)
    args = p.parse_args(argv)
    try:
        if args.command == "doctor":
            result = doctor()
        elif args.command == 'preflight':
            from kdiff.deployment.preflight import inspect_environment
            result = inspect_environment(args.path, gpu=args.gpu)
        elif args.command == "sources":
            result = source_inventory()
        elif args.command == 'provider-check':
            from kdiff.inference.probe import provider_check
            profile=Provider.model_validate_json(args.profile.read_text())
            args.output.parent.mkdir(parents=True,exist_ok=True)
            with args.output.open('x') as output:
                # Reserve the destination before any potentially paid request.
                result=asyncio.run(provider_check(profile))
                json.dump(result,output,indent=2)
            if result['status']!='passed':
                raise ValueError('Provider capability tests failed. Inspect the saved report')
        elif args.command == 'capture-web':
            from kdiff.construction.web import FetchPolicy, PublicFetcher, capture_document
            policy=FetchPolicy.model_validate_json(args.policy.read_text())
            if args.postgres_artifacts:
                from kdiff.core.durable import PostgresArtifacts
                store = PostgresArtifacts()
            else:
                store = ArtifactStore(args.store)
            response=PublicFetcher(policy).get(args.url)
            document=capture_document(response['data'],response['content_type'],response['url'],store)
            result={'document_id':store.put(document),'source':document['url'],'bytes':document['bytes']}
        elif args.command in {'seed', 'tasks', 'init-state'}:
            from kdiff.core.durable import PostgresArtifacts, TaskLedger
            pg = args.postgres_artifacts or getattr(args, 'postgres', False)
            ledger = TaskLedger(args.ledger, dsn_env='KDIFF_POSTGRES_DSN' if pg else None, initialize=True)
            if args.command == 'init-state':
                if pg:
                    PostgresArtifacts(initialize=True)
                result = {'status': 'initialized', 'backend': 'postgresql' if pg else 'sqlite-development'}
            elif args.command == 'tasks':
                result = {'tasks': ledger.status()}
            else:
                from kdiff.construction.seeds import import_csv, import_xlsx
                store = PostgresArtifacts() if pg else ArtifactStore(args.store)
                if args.input.suffix == '.xlsx':
                    if not args.sheet:
                        raise ValueError('XLSX seed import requires an inspected worksheet name')
                    result = import_xlsx(args.input, store, ledger, release=args.release, sheet=args.sheet,
                        name_column=args.name_column, id_column=args.id_column, limit=args.limit)
                else:
                    result = import_csv(args.input, store, ledger, release=args.release,
                                        name_column=args.name_column, id_column=args.id_column, max_records=args.limit)
        else:
            if args.postgres_artifacts:
                from kdiff.core.durable import PostgresArtifacts
                store = PostgresArtifacts()
            else:
                store = ArtifactStore(args.store)
            if args.command == 'inspect':
                from kdiff.analysis.witness import load_release
                release = load_release(store, args.release)
                result = {k: release[k] for k in ['namespace', 'graph_hash', 'counts', 'coverage', 'synthetic']}
                result['identities'] = release['graph']['identities']
            elif args.command == 'queue-build':
                from kdiff.construction.worker import enqueue_builds
                from kdiff.core.durable import TaskLedger
                if args.manifest.stat().st_size > 1024 * 1024:
                    raise ValueError('Construction manifest exceeds budget')
                ledger = TaskLedger(args.ledger, dsn_env='KDIFF_POSTGRES_DSN' if args.postgres_artifacts else None, initialize=True)
                profile = Provider.model_validate_json(args.profile.read_text())
                result = enqueue_builds(json.loads(args.manifest.read_text()), store, ledger, profile)
            elif args.command == 'benchmark-build':
                from kdiff.evaluation.benchmark import import_benchmark
                result = import_benchmark(args.input, store, name=args.name, origin=args.origin)
            elif args.command == 'evaluate':
                from kdiff.evaluation.metrics import evaluate, paired_bootstrap
                if args.annotations.stat().st_size > 16 * 1024 * 1024:
                    raise ValueError('Annotation file exceeds budget')
                rows = [json.loads(line) for line in args.annotations.read_text().splitlines() if line.strip()]
                result = evaluate(store, args.benchmark, rows)
                if args.compare:
                    left, right = args.compare
                    result['paired_bootstrap'] = paired_bootstrap(
                        [r for r in result['rows'] if r['variant'] == left],
                        [r for r in result['rows'] if r['variant'] == right], seed=args.bootstrap_seed)
                    result['comparison_id'] = store.put(result['paired_bootstrap'])
            elif args.command == "replay":
                if args.run:
                    run = store.get(args.run)
                    witnesses = run["outcome"].get("witnesses", [])
                    if not witnesses:
                        raise ValueError("Run has no final witnessed claims")
                    result = {"run": args.run, "replays": [replay(store, w) for w in witnesses]}
                else:
                    result = replay(store, args.witness)
            else:
                if args.service is None:
                    raise ValueError("An owned development --service manifest is required")
                graph = Graph(Path(args.service))
                try:
                    lock = nullcontext() if args.command == 'worker' else exclusive_lock(Path(graph.meta['root']) / 'application.lock')
                    with lock:
                        if args.command == 'worker':
                            from kdiff.construction.worker import work
                            from kdiff.core.durable import TaskLedger
                            ledger = TaskLedger(args.ledger, dsn_env='KDIFF_POSTGRES_DSN' if args.postgres_artifacts else None, initialize=True)
                            profile = Provider.model_validate_json(args.profile.read_text())
                            result = asyncio.run(work(graph, store, ledger, profile, allow_mock=args.allow_mock,
                                max_tasks=args.max_tasks, use_redis=args.redis, use_cache=args.extraction_cache))
                        elif args.command == "snapshot":
                            result = {"release_id": freeze(graph, store, args.namespace, code_digest())}
                        elif args.command in {'reconcile', 'maintain'}:
                            from kdiff.construction.resolution import conflict_view
                            current = graph.export(args.namespace)
                            if args.command == 'reconcile':
                                view = {'kind': 'conflict-view-v1', 'graph_hash': digest(current),
                                        'policy': 'retain_alternatives', 'conflicts': conflict_view(current['assertions'])}
                                result = {'view_id': store.put(view), **view}
                            else:
                                result = {'release_id': freeze(graph, store, args.namespace, code_digest()),
                                          'graph_counts': graph.counts(), 'scope': args.namespace}
                        else:
                            profile = Provider.model_validate_json(args.profile.read_text())
                            if args.command == 'build-web':
                                from kdiff.construction.web_workflow import build_document
                                result=asyncio.run(build_document(graph,store,profile,args.allow_mock,args.document,args.namespace,
                                    json.loads(args.fixture_extraction.read_text()) if args.fixture_extraction else None))
                            elif args.command == "build":
                                from kdiff.core.durable import TaskLedger
                                ledger=TaskLedger(args.ledger,dsn_env='KDIFF_POSTGRES_DSN' if args.postgres_artifacts else None,initialize=True)
                                result = asyncio.run(build(graph, store, profile, args.allow_mock, args.input, args.namespace,
                                    source=args.source, input_format=args.format,
                                    columns=json.loads(args.columns.read_text()) if args.columns else None,ledger=ledger,
                                    use_cache=args.extraction_cache))
                            elif args.command == 'ask' and args.question:
                                from kdiff.analysis.questions import interpret
                                from kdiff.analysis.requests import AnalysisRequest
                                from kdiff.analysis.workflow import analyze
                                from kdiff.inference.limits import budget_scope
                                if args.request or not all([args.start, args.end, args.reference_date]):
                                    raise ValueError('A natural-language question requires explicit start, end and reference date')
                                window = Window(start=args.start, end=args.end, reference_date=args.reference_date)
                                before = None
                                if args.before_start or args.before_end:
                                    before = Window(start=args.before_start, end=args.before_end, reference_date=args.reference_date)
                                with budget_scope(profile):
                                    parsed = asyncio.run(interpret(store, profile, args.release, args.question, window, before=before))
                                    if parsed['clarification']:
                                        result = {'type': 'Clarify', 'reason': parsed['clarification'], 'run': parsed['interpretation_run']}
                                    else:
                                        result = asyncio.run(analyze(graph, store, profile, False, args.release,
                                            AnalysisRequest.model_validate(parsed['request'])))
                                        result['interpretation_run'] = parsed['interpretation_run']
                            elif args.command == 'chat' or args.request:
                                from kdiff.analysis.requests import AnalysisRequest
                                from kdiff.analysis.workflow import analyze
                                from kdiff.core.durable import TaskLedger
                                request = AnalysisRequest.model_validate_json(args.request.read_text())
                                state, revision = None, 0
                                if args.command == 'chat':
                                    ledger = TaskLedger(args.ledger, dsn_env='KDIFF_POSTGRES_DSN' if args.postgres_artifacts else None, initialize=True)
                                    state, revision = ledger.read_checkpoint('dialogue:' + args.dialogue)
                                result = asyncio.run(analyze(graph, store, profile, args.allow_mock, args.release, request, state))
                                if args.command == 'chat':
                                    ledger.checkpoint('dialogue:' + args.dialogue, result['state'], revision)
                            else:
                                if not all([args.author, args.start, args.end, args.reference_date]):
                                    raise ValueError('Supply --request or all explicit author/window arguments')
                                request = CountRequest(author_id=args.author, window=Window(start=args.start, end=args.end,
                                                                                          reference_date=args.reference_date))
                                result = asyncio.run(ask(graph, store, profile, args.allow_mock, args.release, request))
                finally:
                    graph.close()
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        # Never print raw provider/network exceptions which might include URLs or keys.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "reason": 'Invalid typed input. Inspect the field schema and command help' if isinstance(exc, ValidationError)
                          else str(exc) if isinstance(exc, (ValueError, NotImplementedError)) else "Execution failed; inspect local receipts"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
