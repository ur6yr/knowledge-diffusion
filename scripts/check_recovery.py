"""Local real-Neo4j shutdown/restore check in two fresh owned directories."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def payload(args):
    from kdiff.core.artifacts import ArtifactStore, exclusive_lock
    from kdiff.core.graph import Graph
    from kdiff.construction.openalex import capture_local, parse_capture
    from kdiff.analysis.witness import freeze, load_release
    from kdiff.core.contracts import digest
    store = ArtifactStore(args.artifacts / 'store')
    graph = Graph(Path(os.environ['KDIFF_SERVICE']))
    try:
        with exclusive_lock(Path(graph.meta['root']) / 'application.lock'):
            if args.phase == 'create':
                batch = parse_capture(capture_local(Path('tests/fixtures/m1_openalex.jsonl'), store, 'fixture:recovery'), store)
                graph.integrate(batch)
                release_id = freeze(graph, store, 'fixture:recovery', 'recovery-check')
                (args.artifacts / 'release.txt').write_text(release_id)
            else:
                release_id = (args.artifacts / 'release.txt').read_text()
                release = load_release(store, release_id)
                assert digest(graph.export('fixture:recovery')) == release['graph_hash']
                batch = parse_capture(capture_local(Path('tests/fixtures/m1_openalex.jsonl'), store, 'fixture:recovery'), store)
                assert graph.integrate(batch)['reused']
                (args.artifacts / 'verified.json').write_text(json.dumps({'status': 'verified', 'release_id': release_id,
                    'graph_hash': release['graph_hash'], 'counts': graph.counts()}))
    finally:
        graph.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path)
    parser.add_argument('--java-home', type=Path)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--phase', choices=['create', 'verify'])
    args = parser.parse_args()
    if args.phase:
        payload(args)
        return
    args.root.mkdir(parents=True, exist_ok=False)
    args.artifacts.mkdir(parents=True, exist_ok=False)
    for phase in ['create', 'verify']:
        command = [sys.executable, 'deploy/local/run.py', '--home', str(args.home), '--java-home', str(args.java_home),
            '--root', str(args.root / phase), '--store', str(args.artifacts / 'store'),
            '--checkpoint-namespace', 'fixture:recovery']
        if phase == 'verify':
            command += ['--restore', (args.artifacts / 'release.txt').read_text()]
        command += ['--', sys.executable, __file__, '--phase', phase,
                    '--root', str(args.root), '--artifacts', str(args.artifacts)]
        with (args.artifacts / (phase + '.log')).open('w') as stream:
            result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=180)
        if result.returncode:
            raise RuntimeError('Recovery check failed. Inspect ' + str(args.artifacts / (phase + '.log')))
        meta = json.loads((args.root / phase / 'service.json').read_text())
        assert meta['status'] == 'stopped'
    before = json.loads((args.root / 'create/service.json').read_text())
    after = json.loads((args.root / 'verify/service.json').read_text())
    assert before['generation'] != after['generation']
    print((args.artifacts / 'verified.json').read_text())


if __name__ == '__main__':
    main()
