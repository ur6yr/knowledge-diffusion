#!/bin/bash
#SBATCH --job-name=kdiff
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=48G
#SBATCH --time=02:00:00
#SBATCH --signal=B:USR1@180
#SBATCH --no-requeue
# Supply account, partition and (for local mode) GPU type/count to sbatch.
set -euo pipefail
umask 077
: "${SLURM_JOB_ID:?Submit with sbatch}"
: "${KDIFF_REPO:?Absolute checkout path}"
: "${KDIFF_ENV:?Separate application environment}"
: "${KDIFF_NEO4J_HOME:?Verified Neo4j distribution}"
: "${KDIFF_JAVA_HOME:?Verified Java installation}"
: "${KDIFF_ARCHIVE:?Persistent application results directory}"
: "${KDIFF_WORK:?Scratch working directory}"
: "${KDIFF_JOB:?Absolute path to one bounded job JSON file}"
: "${KDIFF_MODE:?Choose local or openai}"
mkdir -p "$KDIFF_ARCHIVE" "$KDIFF_WORK"
KDIFF_RESULTS=$(mktemp -d "$KDIFF_ARCHIVE/job-${SLURM_JOB_ID}-XXXXXX")
KDIFF_RUN=$(mktemp -d "$KDIFF_WORK/job-${SLURM_JOB_ID}-XXXXXX")
"$KDIFF_ENV/bin/python" "$KDIFF_REPO/scripts/package_project.py" --output "$KDIFF_RUN/project.tar.gz"
tar -xzf "$KDIFF_RUN/project.tar.gz" -C "$KDIFF_RUN"
cd "$KDIFF_RUN/knowledge-diffusion"
export PYTHONPATH="$PWD/src"
cp BUILD_MANIFEST.json "$KDIFF_RESULTS/"
# Persistent logical artifacts are independent of the temporary database root.
KDIFF_APP=("$KDIFF_ENV/bin/python" scripts/run_job.py --job "$KDIFF_JOB"
    --store "$KDIFF_RESULTS/store" --ledger "$KDIFF_RESULTS/tasks.sqlite" --output "$KDIFF_RESULTS/result.json")
KDIFF_DB=("$KDIFF_ENV/bin/python" deploy/local/run.py --home "$KDIFF_NEO4J_HOME" --java-home "$KDIFF_JAVA_HOME"
    --root "$KDIFF_RUN/neo4j" --command-timeout 6000 --store "$KDIFF_RESULTS/store")
if [[ -n "${KDIFF_RESTORE_RELEASE:-}" ]]; then
    : "${KDIFF_RESTORE_STORE:?Restore requires the prior persistent artifact store}"
    cp -R "$KDIFF_RESTORE_STORE" "$KDIFF_RESULTS/store"
    KDIFF_DB+=(--restore "$KDIFF_RESTORE_RELEASE")
fi
KDIFF_NAMESPACE=$("$KDIFF_ENV/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["namespace"])' "$KDIFF_JOB")
KDIFF_DB+=(--checkpoint-namespace "$KDIFF_NAMESPACE")
if [[ "${KDIFF_METADATA:-files}" == postgres ]]; then
    KDIFF_DB+=(--postgres-artifacts)
    KDIFF_APP+=(--postgres)
elif [[ "${KDIFF_METADATA:-files}" != files ]]; then
    printf '%s\n' 'KDIFF_METADATA must be files or postgres' >&2
    exit 2
fi
case "$KDIFF_MODE" in
    openai)
        : "${KDIFF_PROFILE:?Authorized profile with a total job cost budget}"
        : "${OPENAI_API_KEY:?Set the key in the job environment}"
        KDIFF_COMMAND=("${KDIFF_DB[@]}" -- "${KDIFF_APP[@]}" --profile "$KDIFF_PROFILE")
        ;;
    local)
        : "${KDIFF_MODEL:?Existing model checkpoint directory}"
        : "${KDIFF_CHAT_TEMPLATE:?Verified model-specific chat template file}"
        : "${KDIFF_TOOL_PARSER:?Verified vLLM tool parser name}"
        KDIFF_SERVE=("$KDIFF_ENV/bin/python" -m kdiff.deployment.vllm --model "$KDIFF_MODEL"
            --chat-template "$KDIFF_CHAT_TEMPLATE" --tool-parser "$KDIFF_TOOL_PARSER" --root "$KDIFF_RUN/vllm"
            --tensor-parallel "${KDIFF_TP:-1}" --context "${KDIFF_CONTEXT:-16384}" --command-timeout 6300)
        if [[ -n "${KDIFF_VLLM_SIF:-}" ]]; then
            KDIFF_SERVE+=(--sif "$KDIFF_VLLM_SIF")
        else
            : "${KDIFF_GPU_ENV:?Separate verified GPU environment or KDIFF_VLLM_SIF}"
            KDIFF_SERVE+=(--python "$KDIFF_GPU_ENV/bin/python")
        fi
        KDIFF_COMMAND=("${KDIFF_SERVE[@]}" -- "${KDIFF_DB[@]}" -- "${KDIFF_APP[@]}")
        ;;
    *) printf '%s\n' 'KDIFF_MODE must be local or openai' >&2; exit 2 ;;
esac
if [[ "${KDIFF_METADATA:-files}" == postgres ]]; then
    : "${KDIFF_POSTGRES_BIN:?Verified user-space PostgreSQL binary directory}"
    : "${KDIFF_REDIS_SERVER:?Verified user-space Redis executable}"
    KDIFF_META=("$KDIFF_ENV/bin/python" -m kdiff.deployment.metadata --postgres-bin "$KDIFF_POSTGRES_BIN"
        --redis-server "$KDIFF_REDIS_SERVER" --root "$KDIFF_RUN/metadata" --command-timeout 6600 --export "$KDIFF_RESULTS")
    if [[ -n "${KDIFF_RESTORE_STATE:-}" ]]; then
        KDIFF_META+=(--restore "$KDIFF_RESTORE_STATE")
    fi
    KDIFF_COMMAND=("${KDIFF_META[@]}" -- "${KDIFF_COMMAND[@]}")
fi
KDIFF_CHILD=
forward_signal() {
    if [[ -n "$KDIFF_CHILD" ]]; then
        kill -USR1 "$KDIFF_CHILD" 2>/dev/null || true
    fi
}
trap forward_signal USR1 TERM INT
"${KDIFF_COMMAND[@]}" > "$KDIFF_RESULTS/run.log" 2>&1 &
KDIFF_CHILD=$!
KDIFF_EXIT=0
# wait can be interrupted by a trapped signal before the child finishes.
while true; do
    wait "$KDIFF_CHILD" && KDIFF_EXIT=0 || KDIFF_EXIT=$?
    kill -0 "$KDIFF_CHILD" 2>/dev/null || break
done
if [[ -f "$KDIFF_RUN/neo4j/checkpoint.json" ]]; then
    cp "$KDIFF_RUN/neo4j/checkpoint.json" "$KDIFF_RESULTS/"
fi
for file in "$KDIFF_RUN/neo4j/service.json" "$KDIFF_RUN/vllm/service.json"; do
    if [[ -f "$file" ]]; then
        name=$(basename "$(dirname "$file")")
        cp "$file" "$KDIFF_RESULTS/${name}-service.json"
    fi
done
printf '%s\n' "$KDIFF_EXIT" > "$KDIFF_RESULTS/exit-code.txt"
printf 'Results: %s\n' "$KDIFF_RESULTS"
exit "$KDIFF_EXIT"
