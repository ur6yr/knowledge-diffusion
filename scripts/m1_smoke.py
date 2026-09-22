"""Persistent M1 CLI demonstration against the owned service supplied by launcher."""

import json
import subprocess
import sys
from pathlib import Path

from kdiff.core.contracts import stable_id


def main():
    out = Path("artifacts/m1")
    out.mkdir(parents=True, exist_ok=True)
    commands = []

    def run(*args):
        command = [sys.executable, "-m", "kdiff", "--store", str(out / "store"), *args]
        executed = subprocess.run(command, text=True, capture_output=True, timeout=120)
        commands.append({"argv": command, "returncode": executed.returncode, "stdout": executed.stdout, "stderr": executed.stderr})
        (out / "commands.json").write_text(json.dumps(commands, indent=2))
        if executed.returncode:
            raise RuntimeError(f"M1 command failed; inspect {out / 'commands.json'}")
        return json.loads(executed.stdout)

    namespace = "fixture:m1-demo"
    args = ("build", "--input", "tests/fixtures/m1_openalex.jsonl", "--namespace", namespace,
            "--profile", "configs/mock.json", "--allow-mock")
    first = run(*args)
    second = run(*args)
    assert not first["reused"] and second["reused"] and first["counts"] == second["counts"]
    release = run("snapshot", "--namespace", namespace)["release_id"]
    answer = run("ask", "--release", release, "--author", stable_id(namespace, "Author", "fixture:openalex:A1"),
                 "--start", "2016-01-01", "--end", "2020-12-31", "--reference-date", "2025-01-01",
                 "--profile", "configs/mock.json", "--allow-mock")
    assert answer["type"] == "Answer", answer
    replayed = run("replay", "--run", answer["run"])
    assert replayed["replays"][0]["count"] == 2
    report = {"synthetic_fixture": True, "mock_inference": True, "first_build": first, "repeat_build": second,
              "release_id": release, "answer": answer, "replay": replayed}
    (out / "result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
