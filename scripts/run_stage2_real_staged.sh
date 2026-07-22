#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-/home/cuihengjia/agent/novel/NS}"
SOURCE="${SOURCE:-${ROOT}/benchmarks/private/ztj_memory_pilot_v0.1}"
OUTPUT="${OUTPUT:-${ROOT}/reports/stage2a/teacher_forced_real/author_plan_conditioned_qwen36_20260722}"
PROJECT_DIRECTORY="${PROJECT_DIRECTORY:?set PROJECT_DIRECTORY to the existing Canonical project directory}"
MODEL_BASE_URL="${MODEL_BASE_URL:-http://127.0.0.1:8002/v1}"
MODEL="${MODEL:-qwen36-27b-nvfp4}"
MODEL_MAX_OUTPUT_TOKENS="${MODEL_MAX_OUTPUT_TOKENS:-8192}"
STAGE2R_DATABASE_URL="${STAGE2R_DATABASE_URL:?set STAGE2R_DATABASE_URL to the loopback PostgreSQL project database}"
PYTHON="${PYTHON:-${ROOT}/.conda-env/bin/python}"
RUNNER="${ROOT}/scripts/run_stage2_teacher_forced_e2e.py"

mkdir -p "${OUTPUT}"

run_stage() {
  local label="$1"
  shift
  printf '[%s] START %s\n' "$(date --iso-8601=seconds)" "${label}"
  "${PYTHON}" "${RUNNER}" \
    --source "${SOURCE}" \
    --output-directory "${OUTPUT}" \
    --resume-project "${PROJECT_DIRECTORY}" \
    --information-profile author_plan_conditioned \
    --semantic-backend local_openai \
    --retrieval-backend real_hybrid \
    --database-url "${STAGE2R_DATABASE_URL}" \
    --model-base-url "${MODEL_BASE_URL}" \
    --model "${MODEL}" \
    --model-max-output-tokens "${MODEL_MAX_OUTPUT_TOKENS}" \
    --model-max-retries 1 \
    "$@"
  printf '[%s] PASS %s\n' "$(date --iso-8601=seconds)" "${label}"
}

gate_summary() {
  local checkpoint="$1"
  local expected_total="$2"
  "${PYTHON}" - "${OUTPUT}" "${PROJECT_DIRECTORY}" "${checkpoint}" "${expected_total}" <<'PY'
import json
import sys
from pathlib import Path

output = Path(sys.argv[1])
project = Path(sys.argv[2])
checkpoint = int(sys.argv[3])
expected_total = int(sys.argv[4])
summary = json.loads((output / "flow_summary.json").read_text("utf-8"))
progress = json.loads((project / "progress_manifest.json").read_text("utf-8"))

errors = []
if summary["last_revealed_chapter"] != checkpoint:
    errors.append("last_revealed_chapter mismatch")
if summary["total_commit_count"] != expected_total:
    errors.append("total_commit_count mismatch")
if progress["last_accepted_chapter"] != checkpoint:
    errors.append("progress checkpoint mismatch")
if not summary["checkpoint_chain_consistent"]:
    errors.append("checkpoint chain is inconsistent")
if summary["future_isolation_failure_count"] != 0:
    errors.append("future isolation failed")
if summary["future_leakage_count"] != 0:
    errors.append("future leakage detected")
if checkpoint >= 20 and summary["paired_results_count"] != 1:
    errors.append("checkpoint paired result is missing")
if summary["semantic_backend"] != "configured_structured_generation_model":
    errors.append("semantic backend is not the configured local model")
if not summary["semantic_quality_eligible"]:
    errors.append("run is not semantic-quality eligible")
if summary["retrieval_backend_profile"] != "real_hybrid":
    errors.append("retrieval backend is not real_hybrid")
if not summary["retrieval_quality_eligible"]:
    errors.append("run is not retrieval-quality eligible")
if not summary["curator_semantic_extraction_enabled"]:
    errors.append("semantic Curator extraction is disabled")
if checkpoint == 95 and not summary["run_complete"]:
    errors.append("C95 run is not complete")
if checkpoint < 95 and summary["run_complete"]:
    errors.append("partial run incorrectly marked complete")

if errors:
    raise SystemExit("; ".join(errors))
print(json.dumps({
    "checkpoint": checkpoint,
    "total_commit_count": summary["total_commit_count"],
    "paired_results_count": summary["paired_results_count"],
    "future_leakage_count": summary["future_leakage_count"],
    "run_complete": summary["run_complete"],
}, ensure_ascii=False, sort_keys=True))
PY
}

if [[ ! -f "${PROJECT_DIRECTORY}/progress_manifest.json" ]]; then
  printf 'missing existing project progress manifest: %s\n' \
    "${PROJECT_DIRECTORY}/progress_manifest.json" >&2
  exit 2
fi

last_chapter="$(${PYTHON} -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["last_accepted_chapter"])' "${PROJECT_DIRECTORY}/progress_manifest.json")"

if (( last_chapter < 1 )); then
  run_stage C1 --resume --max-chapter 1
  gate_summary 1 2
fi

for checkpoint in 20 40 60 80 95; do
  last_chapter="$(${PYTHON} -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["last_accepted_chapter"])' "${PROJECT_DIRECTORY}/progress_manifest.json")"
  checkpoint_done="$(${PYTHON} -c 'import json,sys,pathlib; p=pathlib.Path(sys.argv[1]); c=int(sys.argv[2]); d=json.loads(p.read_text("utf-8")) if p.exists() else {}; print(int(d.get("last_revealed_chapter") == c and d.get("paired_results_count") == 1))' "${OUTPUT}/flow_summary.json" "${checkpoint}")"
  if (( last_chapter < checkpoint )) || (( last_chapter == checkpoint && checkpoint_done == 0 )); then
    run_stage "C${checkpoint}" --resume --max-chapter "${checkpoint}"
    gate_summary "${checkpoint}" "$((checkpoint + 1))"
    cp -- "${OUTPUT}/e2e_paired_report.json" \
      "${OUTPUT}/e2e_paired_report_C${checkpoint}.json"
  fi
done

"${PYTHON}" "${ROOT}/scripts/aggregate_stage2_checkpoint_reports.py" \
  --source "${SOURCE}" \
  --output-directory "${OUTPUT}" \
  --information-profile author_plan_conditioned

printf '[%s] COMPLETE Stage 2 real teacher-forced run: %s\n' \
  "$(date --iso-8601=seconds)" "${OUTPUT}"
