#!/bin/bash
# Bounded CPU fixture pilot. Cluster execution/recovery remains unvalidated.
# Supply the actual account and partition to sbatch; neither is assumed here.
#SBATCH --job-name=kdiff-pilot
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --time=00:20:00
#SBATCH --no-requeue

set -euo pipefail
umask 077
: "${SLURM_JOB_ID:?Submit this script with sbatch; do not run on a login node}"
: "${KDIFF_REPO:?Set the absolute path to the unpacked project bundle}"
: "${KDIFF_ENV:?Set the absolute path to the separate application environment}"
: "${KDIFF_NEO4J_HOME:?Set the verified Neo4j binary distribution path}"
: "${KDIFF_JAVA_HOME:?Set the verified Java 21 installation path}"
: "${KDIFF_WORK:?Set the scratch work directory}"
: "${KDIFF_ARCHIVE:?Set a persistent results directory}"
KDIFF_PILOT_MODE=${KDIFF_PILOT_MODE:-demo}
case "$KDIFF_PILOT_MODE" in
    demo|tests) ;;
    *) printf '%s\n' 'KDIFF_PILOT_MODE must be demo or tests' >&2; exit 2 ;;
esac
for path in "$KDIFF_REPO" "$KDIFF_ENV" "$KDIFF_NEO4J_HOME" "$KDIFF_JAVA_HOME" "$KDIFF_WORK" "$KDIFF_ARCHIVE"; do
    case "$path" in /*) ;; *) printf '%s\n' 'All KDIFF paths must be absolute' >&2; exit 2 ;; esac
done
test -x "$KDIFF_ENV/bin/python"
test -x "$KDIFF_NEO4J_HOME/bin/neo4j"
test -x "$KDIFF_JAVA_HOME/bin/java"
mkdir -p "$KDIFF_WORK" "$KDIFF_ARCHIVE"
KDIFF_RUN_DIR=$(mktemp -d "$KDIFF_WORK/pilot-${SLURM_JOB_ID}-XXXXXX")
KDIFF_RESULTS=$(mktemp -d "$KDIFF_ARCHIVE/pilot-${SLURM_JOB_ID}-${KDIFF_PILOT_MODE}-XXXXXX")
printf 'Work directory: %s\nResults directory: %s\n' "$KDIFF_RUN_DIR" "$KDIFF_RESULTS"

# Copy only allowlisted application files even if KDIFF_REPO is a full checkout.
"$KDIFF_ENV/bin/python" "$KDIFF_REPO/scripts/package_project.py" --output "$KDIFF_RUN_DIR/project.tar.gz"
tar -xzf "$KDIFF_RUN_DIR/project.tar.gz" -C "$KDIFF_RUN_DIR"
cd "$KDIFF_RUN_DIR/knowledge-diffusion"
export PYTHONPATH="$PWD/src"
mkdir -p artifacts
cp BUILD_MANIFEST.json "$KDIFF_RESULTS/"
"$KDIFF_ENV/bin/python" -c 'import importlib.metadata as m, json, platform; print(json.dumps({"python": platform.python_version(), "platform": platform.platform(), "packages": {d.metadata["Name"]: d.version for d in m.distributions()}}, indent=2))' > "$KDIFF_RESULTS/environment.json"
"$KDIFF_JAVA_HOME/bin/java" -version > "$KDIFF_RESULTS/java-version.txt" 2>&1

if [[ "$KDIFF_PILOT_MODE" == tests ]]; then
    command=("$KDIFF_ENV/bin/python" -m pytest -q --junitxml=artifacts/tests.xml)
else
    command=("$KDIFF_ENV/bin/python" scripts/m1_smoke.py)
fi
result=0
"$KDIFF_ENV/bin/python" deploy/local/run.py \
    --home "$KDIFF_NEO4J_HOME" --java-home "$KDIFF_JAVA_HOME" \
    --root "$PWD/.runtime/neo4j" --bolt-port "${KDIFF_BOLT_PORT:-17687}" \
    -- "${command[@]}" > "$KDIFF_RESULTS/run.log" 2>&1 || result=$?

# The launcher has returned and stopped its own child. Preserve logical evidence,
# never copy the database directory or its generated password into the archive.
cp -R artifacts "$KDIFF_RESULTS/"
if [[ -f .runtime/neo4j/logs/console.log ]]; then
    cp .runtime/neo4j/logs/console.log "$KDIFF_RESULTS/neo4j-console.log"
fi
printf '%s\n' "$result" > "$KDIFF_RESULTS/exit-code.txt"
printf 'Pilot exit code: %s\nResults: %s\n' "$result" "$KDIFF_RESULTS"
exit "$result"
