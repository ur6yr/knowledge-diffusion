"""Build an allowlisted deployment archive without local Codex or runtime files."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "README.md", "pyproject.toml", "requirements-lock.txt",
    "configs/mock.json", "configs/local-dev.example.json", "configs/openai.example.json",
    "deploy/local/run.py", "deploy/rivanna/pilot.sbatch",
    "scripts/m1_smoke.py", "scripts/package_project.py",
    "docs/DECISIONS.md", "docs/M1_USAGE.md", "docs/SOURCES.md",
    "tests/fixtures/README.md", "tests/fixtures/m1_openalex.jsonl",
)
PATTERNS = ("src/kdiff/**/*.py", "src/kdiff/prompts/*/*.txt", "tests/test_*.py", "tests/conftest.py")


def project_files(root=ROOT):
    paths = set(FILES)
    for pattern in PATTERNS:
        paths.update(p.relative_to(root).as_posix() for p in root.glob(pattern))
    selected = []
    for name in sorted(paths):
        parts = Path(name).parts
        if any(p.startswith(".") or p == "__pycache__" or p == "AGENTS.md"
               or p.upper().startswith("CODEX") for p in parts):
            continue
        path = root / name
        if any(p.is_symlink() for p in (path, *path.parents) if p != root and root in p.parents):
            raise ValueError(f"Deployment input must not be a symlink: {name}")
        if not path.is_file():
            raise ValueError(f"Required deployment file is missing: {name}")
        selected.append((name, path))
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist/knowledge-diffusion.tar.gz"))
    parser.add_argument("--list", action="store_true", help="Review selected files without creating an archive")
    args = parser.parse_args()
    selected = project_files()
    if args.list:
        print("\n".join(name for name, _ in selected))
        return
    payloads = {name: path.read_bytes() for name, path in selected}
    manifest = {"format": 1, "files": {
        name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        for name, data in payloads.items()
    }}
    payloads["BUILD_MANIFEST.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids silently replacing an already reviewed release.
    with args.output.open("xb") as output, tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in payloads.items():
            entry = tarfile.TarInfo("knowledge-diffusion/" + name)
            entry.size, entry.mode, entry.mtime = len(data), 0o644, 0
            archive.addfile(entry, io.BytesIO(data))
    print(f"Created {args.output} ({len(selected)} project files plus BUILD_MANIFEST.json)")


if __name__ == "__main__":
    main()
