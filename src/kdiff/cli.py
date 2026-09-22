import argparse
import asyncio
import importlib.metadata
import json
import os
import sys
from pathlib import Path

from kdiff.analysis.program import IMPLEMENTED, OPERATORS
from kdiff.analysis.witness import freeze, replay
from kdiff.core.artifacts import ArtifactStore, exclusive_lock
from kdiff.core.contracts import CountRequest, Window, digest
from kdiff.core.graph import Graph
from kdiff.inference.client import Provider
from kdiff.workflows import ask, build


def code_digest():
    root = Path(__file__).parent
    return digest({str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("*.py"))})


def doctor():
    versions = {}
    for name in ["autogen-agentchat", "autogen-core", "autogen-ext", "neo4j", "pydantic", "openai"]:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not_installed"
    return {"application": "M1 bounded development slice", "python": sys.version.split()[0], "versions": versions,
            "openai_key": "configured" if os.environ.get("OPENAI_API_KEY") else "not_configured",
            "slurm_allocation": bool(os.environ.get("SLURM_JOB_ID")),
            "live_profiles": "blocked: capability/budget tests pending M5",
            "operators_implemented_for_count_grammar": IMPLEMENTED,
            "operators_pending_M3": [op for op in OPERATORS if op not in IMPLEMENTED]}


def source_inventory():
    return {name: {"implementation": "bounded local native JSONL/JSONL.gz" if name == "OpenAlex" else "pending_M2",
                   "live_status": "not_configured"}
            for name in ["OpenAlex", "ORCID", "ROR", "USPTO", "PatCit", "targeted_web"]}


def main(argv=None):
    p = argparse.ArgumentParser(description="M1 temporal graph construction and witnessed counts")
    p.add_argument("--store", type=Path, default=Path("artifacts/store"))
    p.add_argument("--service", type=Path, default=os.environ.get("KDIFF_SERVICE"))
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    commands.add_parser("sources")
    b = commands.add_parser("build", help="Bounded local OpenAlex construction")
    b.add_argument("--input", type=Path, required=True)
    b.add_argument("--namespace", required=True)
    a = commands.add_parser("ask", help="Explicit-ID paper count; broader natural language QA pending M3")
    a.add_argument("--release", required=True)
    a.add_argument("--author", required=True)
    a.add_argument("--start", required=True)
    a.add_argument("--end", required=True)
    a.add_argument("--reference-date", required=True)
    for sub in [b, a]:
        sub.add_argument("--profile", type=Path, required=True)
        sub.add_argument("--allow-mock", action="store_true")
    s = commands.add_parser("snapshot", help="Freeze a complete bounded export under the writer lock")
    s.add_argument("--namespace", required=True)
    r = commands.add_parser("replay", help="Replay saved complete operands without graph, network or model")
    group = r.add_mutually_exclusive_group(required=True)
    group.add_argument("--witness")
    group.add_argument("--run")
    for name, milestone in [("reconcile", "M4"), ("maintain", "M4"), ("chat", "M3"),
                            ("seed", "M2"), ("evaluate", "M7"), ("benchmark-build", "M7")]:
        sub = commands.add_parser(name, help=f"Not implemented; scheduled for {milestone}")
        sub.set_defaults(pending=milestone)
    args = p.parse_args(argv)
    try:
        if hasattr(args, "pending"):
            raise NotImplementedError(f"{args.command} is not implemented; pending {args.pending}")
        if args.command == "doctor":
            result = doctor()
        elif args.command == "sources":
            result = source_inventory()
        else:
            store = ArtifactStore(args.store)
            if args.command == "replay":
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
                    with exclusive_lock(Path(graph.meta["root"]) / "application.lock"):
                        if args.command == "snapshot":
                            result = {"release_id": freeze(graph, store, args.namespace, code_digest())}
                        else:
                            profile = Provider.model_validate_json(args.profile.read_text())
                            if args.command == "build":
                                result = asyncio.run(build(graph, store, profile, args.allow_mock, args.input, args.namespace))
                            else:
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
                          "reason": str(exc) if isinstance(exc, (ValueError, NotImplementedError)) else "Execution failed; inspect local receipts"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
