"""One bounded source-to-witness job with a shared live-inference budget."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from kdiff.analysis.requests import AnalysisRequest
from kdiff.analysis.workflow import analyze
from kdiff.analysis.witness import freeze, replay
from kdiff.cli import code_digest
from kdiff.core.artifacts import ArtifactStore, exclusive_lock
from kdiff.core.durable import PostgresArtifacts, TaskLedger
from kdiff.core.graph import Graph
from kdiff.deployment.manifest import atomic_json
from kdiff.inference.client import Provider
from kdiff.inference.limits import budget_scope
from kdiff.inference.probe import provider_check
from kdiff.workflows import build


async def execute(args):
    profile_path = args.profile or Path(os.environ['KDIFF_LIVE_PROFILE'])
    profile = Provider.model_validate_json(profile_path.read_text())
    store = PostgresArtifacts() if args.postgres else ArtifactStore(args.store)
    ledger = TaskLedger(args.ledger, dsn_env='KDIFF_POSTGRES_DSN' if args.postgres else None, initialize=True)
    job = json.loads(args.job.read_text())
    allowed = {'input', 'namespace', 'source', 'format', 'columns', 'request'}
    if set(job) - allowed or not {'input', 'namespace', 'request'} <= set(job):
        raise ValueError('Invalid job fields. Supply one source, namespace and typed request')
    report = {'kind': 'bounded-job-v1', 'job': job, 'status': 'starting'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise ValueError('Job result already exists. Choose a new result path')
    with budget_scope(profile) as budget:
        try:
            if profile.profile != 'mock':
                if not profile.capability_report:
                    raise ValueError('Set the output path for the required capability report in the profile')
                Path(profile.capability_report).parent.mkdir(parents=True, exist_ok=True)
                capability = await provider_check(profile)
                atomic_json(Path(profile.capability_report), capability)
                if capability['status'] != 'passed':
                    raise ValueError('Provider capability check failed')
            graph = Graph(Path(os.environ['KDIFF_SERVICE']))
            try:
                with exclusive_lock(Path(graph.meta['root']) / 'application.lock'):
                    report['build'] = await build(graph, store, profile, args.allow_mock, Path(job['input']), job['namespace'],
                        source=job.get('source', 'openalex'), input_format=job.get('format', 'jsonl'),
                        columns=job.get('columns'), ledger=ledger)
                    release_id = freeze(graph, store, job['namespace'], code_digest())
                    report['release_id'] = release_id
                    report['analysis'] = await analyze(graph, store, profile, args.allow_mock, release_id,
                                                      AnalysisRequest.model_validate(job['request']))
                    report['replay'] = [replay(store, wid) for wid in report['analysis']['witnesses']]
                    report['status'] = report['analysis']['type']
            finally:
                graph.close()
        except BaseException as exc:
            report.update(status='failed', error_type=type(exc).__name__)
            raise
        finally:
            report['budget'] = budget.report()
            atomic_json(args.output, report)
    print(json.dumps({'status': report['status'], 'release_id': report['release_id'], 'result': str(args.output)}, indent=2))
    return 0 if report['status'] == 'Answer' else 3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', type=Path, required=True)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--allow-mock', action='store_true')
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--store', type=Path, default=Path('artifacts/store'))
    parser.add_argument('--ledger', type=Path, default=Path('artifacts/tasks.sqlite'))
    parser.add_argument('--output', type=Path, default=Path('artifacts/job.json'))
    try:
        return asyncio.run(execute(parser.parse_args()))
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__, 'detail': 'Inspect the restricted job result and run receipts'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
