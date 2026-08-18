#!/usr/bin/env bash
# Start the native Stage 2M infrastructure, retrieval models, and Qwen endpoint
# used by the semantic-closure real run.
#
# Default behavior only starts services and waits for their health endpoints.
# Set RUN_REAL_TEST=1 to run the five frozen checkpoints after all services are
# healthy.  The test output root is never deleted or overwritten.

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT}"

# A worktree has its own source and tmp tree, but native service leases and
# ports should have one owner.  Resolve the common checkout root for those
# services while keeping ROOT as the code/output root for this run.
COMMON_GIT_DIR="$(git -C "${ROOT}" rev-parse --git-common-dir 2>/dev/null || true)"
if [[ "${COMMON_GIT_DIR}" != /* ]]; then
    COMMON_GIT_DIR="${ROOT}/${COMMON_GIT_DIR}"
fi
SERVICE_ROOT="${SERVICE_ROOT:-$(cd -- "$(dirname -- "${COMMON_GIT_DIR}")" && pwd)}"
PYTHON="${PYTHON:-${SERVICE_ROOT}/.conda-env/bin/python}"
RUN_REAL_TEST="${RUN_REAL_TEST:-0}"

export NOVEL_AGENT_EMBEDDING_MODEL_PORT="${NOVEL_AGENT_EMBEDDING_MODEL_PORT:-8081}"
export NOVEL_AGENT_RERANKER_MODEL_PORT="${NOVEL_AGENT_RERANKER_MODEL_PORT:-8082}"

EMBEDDING_PORT="${NOVEL_AGENT_EMBEDDING_MODEL_PORT}"
RERANKER_PORT="${NOVEL_AGENT_RERANKER_MODEL_PORT}"
QWEN_HOST="${QWEN_HOST:-127.0.0.1}"
QWEN_PORT="${QWEN_PORT:-8005}"
QWEN_MODEL_DIR="${QWEN_MODEL_DIR:-/data1/users/cuihengjia/qwen3.8}"
QWEN_VLLM="${QWEN_VLLM:-/data1/users/cuihengjia/qwen3.6/.venv-vllm-cu128-src/bin/vllm}"
QWEN_GPU_IDS="${QWEN_GPU_IDS:-5,6}"
QWEN_LOG_PATH="${QWEN_LOG_PATH:-${QWEN_MODEL_DIR}/logs/vllm-qwen38-fp8-gpu5-6-8005-semantic.log}"

SOURCE_PROJECT="${SOURCE_PROJECT:-${ROOT}/tmp/ns-stage2m-genesis-8005-20260815-v10}"
CHECKPOINT_INDEX="${CHECKPOINT_INDEX:-${SOURCE_PROJECT}/combined_checkpoint_index.json}"
OUTPUT_ROOT="${OUTPUT_ROOT:-${ROOT}/tmp/ns-stage2m-evidence-semantic-closure-20260818-v1}"
DATABASE_ENV_FILE="${DATABASE_ENV_FILE:-${ROOT}/tmp/database_url_v10.env}"
EXPERIMENT_ID="${EXPERIMENT_ID:-stage2m-evidence-semantic-closure-20260818-v1}"

log() {
    printf '[stage2m-start] %s\n' "$*"
}

die() {
    printf '[stage2m-start] ERROR: %s\n' "$*" >&2
    exit 1
}

require_file() {
    test -f "$1" || die "required file is missing: $1"
}

require_dir() {
    test -d "$1" || die "required directory is missing: $1"
}

wait_http() {
    local url="$1"
    local timeout_seconds="${2:-300}"
    local deadline=$((SECONDS + timeout_seconds))
    while (( SECONDS < deadline )); do
        if curl --fail --silent --show-error --max-time 3 "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

model_identity() {
    local key="$1"
    "${PYTHON}" - "${SERVICE_ROOT}/infra/retrieval-models.lock" "${key}" <<'PY'
import json
import sys

lock_path, model_key = sys.argv[1:]
with open(lock_path, encoding="utf-8") as handle:
    model = json.load(handle)["models"][model_key]
print(model["model_id"], model["revision"])
PY
}

listener_pids() {
    local port="$1"
    local line
    command -v ss >/dev/null 2>&1 || die 'ss is required to inspect an occupied model port'
    line="$(ss -ltnpH "sport = :${port}" 2>/dev/null || true)"
    if test -z "${line}"; then
        return 0
    fi
    grep -oE 'pid=[0-9]+' <<<"${line}" | cut -d= -f2 | sort -u
}

reconcile_unmanaged_listener() {
    local key="$1"
    local port="$2"
    local model_id revision pid owner command_line
    local -a pids

    mapfile -t pids < <(listener_pids "${port}")
    if (( ${#pids[@]} == 0 )); then
        return 0
    fi
    if (( ${#pids[@]} != 1 )); then
        die "model port ${port} has multiple listener PIDs; refusing takeover"
    fi

    pid="${pids[0]}"
    owner="$(ps -p "${pid}" -o uid= | tr -d ' ')"
    [[ "${owner}" == "$(id -u)" ]] \
        || die "model port ${port} is owned by UID ${owner}; refusing takeover"
    command_line="$(ps -p "${pid}" -o args=)"
    IFS=' ' read -r model_id revision < <(model_identity "${key}")
    case "${command_line}" in
        *retrieval_model_service.py*"--kind ${key}"*"--model-id ${model_id}"*"--revision ${revision}"*"--port ${port}"*)
            log "verified unmanaged ${key} service PID ${pid}; stopping it to recreate its owner lease"
            ;;
        *)
            die "loopback port ${port} is occupied by an unknown process (PID ${pid}); refusing takeover"
            ;;
    esac

    kill -TERM "${pid}"
    for _ in {1..30}; do
        if ! kill -0 "${pid}" 2>/dev/null; then
            break
        fi
        sleep 1
    done
    if kill -0 "${pid}" 2>/dev/null; then
        die "verified ${key} process PID ${pid} did not exit after SIGTERM"
    fi
    mapfile -t pids < <(listener_pids "${port}")
    (( ${#pids[@]} == 0 )) \
        || die "model port ${port} remains occupied after stopping PID ${pid}"
}

start_infrastructure() {
    log 'starting native infrastructure'
    "${PYTHON}" "${SERVICE_ROOT}/scripts/native_infra.py" up
    "${PYTHON}" "${SERVICE_ROOT}/scripts/native_infra.py" health
}

start_retrieval_model() {
    local key="$1"
    local pid_file="${SERVICE_ROOT}/tmp/native-models/run/${key}.json"
    local status port managed=0

    if [[ "${key}" == embedding ]]; then
        port="${EMBEDDING_PORT}"
    else
        port="${RERANKER_PORT}"
    fi

    # The owner script removes a dead lease and refuses to signal an
    # unverified live process.  This avoids hand-editing PID records.
    if test -e "${pid_file}"; then
        status="$("${PYTHON}" "${SERVICE_ROOT}/scripts/native_models.py" status --model "${key}")"
        if grep -q '"running": false' <<<"${status}"; then
            log "removing dead ${key} PID lease through native_models.py"
            "${PYTHON}" "${SERVICE_ROOT}/scripts/native_models.py" down --model "${key}"
        else
            managed=1
        fi
    fi

    if (( managed == 0 )); then
        reconcile_unmanaged_listener "${key}" "${port}"
    fi
    log "starting ${key} retrieval model"
    "${PYTHON}" "${SERVICE_ROOT}/scripts/native_models.py" up --model "${key}"
}

start_retrieval_models() {
    start_retrieval_model embedding
    start_retrieval_model reranker
    "${PYTHON}" "${SERVICE_ROOT}/scripts/native_models.py" health
    wait_http "http://127.0.0.1:${EMBEDDING_PORT}/health" 30 \
        || die "embedding health endpoint did not become ready"
    wait_http "http://127.0.0.1:${RERANKER_PORT}/health" 30 \
        || die "reranker health endpoint did not become ready"
}

start_qwen() {
    local models_url="http://${QWEN_HOST}:${QWEN_PORT}/v1/models"

    if curl --fail --silent --show-error --max-time 3 "${models_url}" >/dev/null 2>&1; then
        log "Qwen endpoint already healthy at ${models_url}; reusing it"
        return 0
    fi

    require_dir "${QWEN_MODEL_DIR}"
    test -x "${QWEN_VLLM}" || die "vLLM executable is missing or not executable: ${QWEN_VLLM}"
    mkdir -p "$(dirname -- "${QWEN_LOG_PATH}")"

    log "starting Qwen qwen38-27b-fp8 on CUDA_VISIBLE_DEVICES=${QWEN_GPU_IDS}"
    log "Qwen log: ${QWEN_LOG_PATH}"
    nohup env CUDA_VISIBLE_DEVICES="${QWEN_GPU_IDS}" \
        "${QWEN_VLLM}" serve "${QWEN_MODEL_DIR}" \
        --host "${QWEN_HOST}" \
        --port "${QWEN_PORT}" \
        --served-model-name qwen38-27b-fp8 \
        --tensor-parallel-size 2 \
        --max-model-len 131072 \
        --enforce-eager \
        --attention-backend triton_attn \
        --reasoning-parser qwen3 \
        --disable-custom-all-reduce \
        --gpu-memory-utilization 0.90 \
        --kv-cache-dtype fp8_e4m3 \
        --max-num-batched-tokens 8192 \
        --max-num-seqs 4 \
        >"${QWEN_LOG_PATH}" 2>&1 < /dev/null &
    log "Qwen process started with shell PID $!; waiting up to 900 seconds"

    if ! wait_http "${models_url}" 900; then
        tail -80 "${QWEN_LOG_PATH}" >&2 || true
        die "Qwen endpoint did not become ready; inspect ${QWEN_LOG_PATH}"
    fi
}

run_real_test() {
    require_file "${DATABASE_ENV_FILE}"
    require_dir "${SOURCE_PROJECT}/objects"
    require_file "${CHECKPOINT_INDEX}"
    if test -e "${OUTPUT_ROOT}"; then
        die "output identity already exists; choose a new OUTPUT_ROOT: ${OUTPUT_ROOT}"
    fi

    # DATABASE_URL is supplied by the existing read-only v10 environment file;
    # its contents are never printed by this script.
    # shellcheck disable=SC1090
    source "${DATABASE_ENV_FILE}"
    test -n "${DATABASE_URL:-}" || die "DATABASE_URL is missing from ${DATABASE_ENV_FILE}"

    log "running five frozen checkpoints"
    # Keep the semantic implementation from this worktree first, while the
    # scripts package resolves native-model leases from SERVICE_ROOT.
    PYTHONPATH="${ROOT}/src:${SERVICE_ROOT}" "${PYTHON}" \
        "${ROOT}/scripts/run_evidence_first_frozen_checkpoints.py" \
        --source-project "${SOURCE_PROJECT}" \
        --checkpoint-index "${CHECKPOINT_INDEX}" \
        --output-root "${OUTPUT_ROOT}" \
        --database-url "${DATABASE_URL}" \
        --model-base-url "http://${QWEN_HOST}:${QWEN_PORT}/v1" \
        --embedding-url "http://127.0.0.1:${EMBEDDING_PORT}/v1/embeddings" \
        --reranker-url "http://127.0.0.1:${RERANKER_PORT}/rerank" \
        --model qwen38-27b-fp8 \
        --experiment-id "${EXPERIMENT_ID}" \
        --case P001 \
        --case P002 \
        --case P003 \
        --case P004 \
        --case P005
}

main() {
    require_file "${PYTHON}"
    command -v curl >/dev/null 2>&1 || die 'curl is required'
    start_infrastructure
    start_retrieval_models
    start_qwen
    log 'all requested services are healthy'
    printf '  service root: %s\n' "${SERVICE_ROOT}"
    printf '  infra: native\n'
    printf '  embedding: http://127.0.0.1:%s/health\n' "${EMBEDDING_PORT}"
    printf '  reranker: http://127.0.0.1:%s/health\n' "${RERANKER_PORT}"
    printf '  qwen: http://%s:%s/v1/models\n' "${QWEN_HOST}" "${QWEN_PORT}"
    if [[ "${RUN_REAL_TEST}" == 1 ]]; then
        run_real_test
    else
        log 'services only; set RUN_REAL_TEST=1 to run the five-case real test'
    fi
}

main "$@"
