#!/usr/bin/env bash
# Drive one planning stage of 《余烬九序》 to its verified stop point.
#
# This is the version-managed replacement for the frozen v23 driver.  Its
# predecessor decided everything from one `runtime status` snapshot: it read
# `block_cause` (which is only populated for BLOCKED tasks, so a retryable task
# reported nothing), matched uppercase words in that field, and drove the loop
# with a bash `case`.  It also broke out of the loop on failure and then ran
# successful bookkeeping commands, so the script could exit 0 after a failed
# slice.
#
# Three properties matter here and none of them is a log heuristic:
#
#   1. A task's recovery position comes from `runtime classify`, which reads the
#      last settled attempt's canonical failure class and that attempt's effect
#      ledger.  An unsettled send is never retried blind.
#   2. A failed subcommand fails the script.  There is no `tee`, no `|| true`
#      around work, and bookkeeping runs before the exit status is decided while
#      its own failures count.
#   3. A stage stops on its verified evidence, not on "there was no READY task".
#      G0 needs eight committed volumes; G1/G2/G3 need committed chapter text at
#      their named chapter.  A stage that cannot prove its exit fails.
#
# Usage: 60_run_stage.sh <g0|g1|g2|g3> [max-slices]
set -uo pipefail

STAGE="${1:-g0}"
MAX_SLICES="${2:-60}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/../environment.sh"

# The stage's verified exit.  G0 proves itself with committed volumes; the later
# stages prove themselves with committed chapter text.
case "$STAGE" in
    g0) CHAPTER_GATE=0;  VOLUME_GATE=8 ;;
    g1) CHAPTER_GATE=2;  VOLUME_GATE=8 ;;
    g2) CHAPTER_GATE=5;  VOLUME_GATE=8 ;;
    g3) CHAPTER_GATE=20; VOLUME_GATE=8 ;;
    *) echo "[fatal] unknown stage: $STAGE" >&2; exit 64 ;;
esac

RUN_LOG="${NOVEL_LOGS}/${STAGE}-driver.log"
FAILURES=0
slice=0
STAGE_OK=1

# Progress goes to the operator and to the log without a pipeline, because a
# pipeline's exit status is the last member's and this function must not be able
# to hide a failure behind a successful writer.
log() {
    printf '%s\n' "$*"
    printf '%s\n' "$*" >> "$RUN_LOG"
}

# A failing subcommand is recorded and returned; no caller may ignore it.
must() {
    local label="$1"; shift
    "$@" >>"$RUN_LOG" 2>&1
    local status=$?
    if [[ $status -ne 0 ]]; then
        log "[fail] $label exited $status"
        FAILURES=$((FAILURES + 1))
    fi
    return "$status"
}

# Read the result of a command's *own* output, given the command wrote it to a
# temporary file.  Scraping the shared run log instead would hand back whatever
# the previous command wrote whenever the current one printed nothing, and a
# failed command's stale payload would then be indistinguishable from a fresh
# one; the earlier version of this driver had that defect.
last_json() {  # <file>
    "$NOVEL_PYTHON" -c '
import json, sys
try:
    print(json.dumps(json.loads(open(sys.argv[1], encoding="utf-8").read().strip().splitlines()[-1])))
except (OSError, ValueError, IndexError):
    print("")
' "$1"
}

# A command whose result the driver consumes on its own: its stdout is captured
# for the caller and copied into the run log, and its exit status is recorded
# exactly once by must().
capture() {  # <out-file> <label> <command...>
    local out="$1" label="$2"; shift 2
    "$@" >"$out" 2>&1
    local status=$?
    cat "$out" >>"$RUN_LOG"
    if [[ $status -ne 0 ]]; then
        log "[fail] $label exited $status"
        FAILURES=$((FAILURES + 1))
    fi
    return "$status"
}

stage_roots() {
    capture "$NOVEL_STATE/roots.out" "roots" \
        "$NOVEL_AGENT" runtime --database-url "$NOVEL_DATABASE_URL" roots \
        --project-id "$NOVEL_PROJECT_ID" --object-store-root "$NOVEL_OBJECT_STORE"
}

status_snapshot() {
    capture "$NOVEL_STATE/status.out" "status" \
        "$NOVEL_AGENT" runtime --database-url "$NOVEL_DATABASE_URL" status \
        --run-id "$NOVEL_RUN_ID" || return $?
    last_json "$NOVEL_STATE/status.out" > "$NOVEL_STATE/status.json"
}

first_task_in_status() {  # one status per line per task; first match wins
    "$NOVEL_PYTHON" - "$NOVEL_STATE/status.json" "$@" <<'PY'
import json, sys
path, wanted = sys.argv[1], set(sys.argv[2:])
try:
    tasks = json.load(open(path, encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(0)
for task in tasks:
    if task.get("status") in wanted:
        print(task["task_id"]); break
PY
}

task_field() {
    "$NOVEL_PYTHON" - "$NOVEL_STATE/status.json" "$1" "$2" <<'PY'
import json, sys
path, task_id, field = sys.argv[1:4]
try:
    tasks = json.load(open(path, encoding="utf-8"))
except (OSError, ValueError):
    print(""); raise SystemExit(0)
task = next((t for t in tasks if t.get("task_id") == task_id), {})
print(task.get(field) or "")
PY
}

classify_task() {
    "$NOVEL_AGENT" runtime --database-url "$NOVEL_DATABASE_URL" \
        classify --task-id "$1" 2>>"$RUN_LOG"
}

classification_field() {
    "$NOVEL_PYTHON" -c '
import json, sys
try:
    payload = json.loads(sys.stdin.read().strip().splitlines()[-1])
except (ValueError, IndexError):
    print(""); raise SystemExit(0)
print(payload.get("classification", {}).get(sys.argv[1], ""))
' "$1"
}

# The stage's own exit evidence, read from the committed roots.  The gate
# messages are suppressed on the pre-flight call, which runs in the normal
# "not there yet" case and must not log a failure that has not happened.
stage_exit_satisfied() {
    local quiet="${1:-}" json chapters volumes
    stage_roots || return 1
    json="$(last_json "$NOVEL_STATE/roots.out")"
    chapters="$("$NOVEL_PYTHON" -c '
import json,sys
try: print(int(json.loads(sys.stdin.read().strip().splitlines()[-1]).get("committed_chapters") or 0))
except (ValueError, IndexError): print(-1)
' <<<"$json")"
    volumes="$("$NOVEL_PYTHON" -c '
import json,sys
try: print(int(json.loads(sys.stdin.read().strip().splitlines()[-1]).get("committed_volumes") or 0))
except (ValueError, IndexError): print(-1)
' <<<"$json")"
    if [[ "$volumes" -lt "$VOLUME_GATE" ]]; then
        [[ -n "$quiet" ]] || log "[stage] ${STAGE} exit not proven: ${volumes}/${VOLUME_GATE} committed volumes"
        return 1
    fi
    if [[ "$CHAPTER_GATE" -gt 0 && "$chapters" -lt "$CHAPTER_GATE" ]]; then
        [[ -n "$quiet" ]] || log "[stage] ${STAGE} exit not proven: ${chapters}/${CHAPTER_GATE} committed chapters"
        return 1
    fi
    return 0
}

log "[stage] ${STAGE}: max ${MAX_SLICES} slices, gates ${VOLUME_GATE} volumes / ${CHAPTER_GATE} chapters"

# Already at the stage's exit?  Verify it before spending a slice.  A roots call
# that fails here is reported by stage_roots() itself and not counted twice.
if stage_exit_satisfied quiet; then
    log "[exit] ${STAGE} exit evidence already present"
    exit 0
fi

while [[ $slice -lt $MAX_SLICES ]]; do
    status_snapshot || break
    slice=$((slice + 1))
    label="$(printf '%s-%02d' "$STAGE" "$slice")"

    if ! must "advance $label" "$NOVEL_AGENT" runtime --database-url "$NOVEL_DATABASE_URL" \
        advance \
        --project-id "$NOVEL_PROJECT_ID" --run-id "$NOVEL_RUN_ID" \
        --policy "$NOVEL_POLICY" --manifest "$NOVEL_MANIFEST" \
        --object-store-root "$NOVEL_OBJECT_STORE" \
        --endpoint-profile "$NOVEL_ENDPOINT_PROFILE" \
        --max-tasks 1 \
        --scheduling-timeout-seconds "$NOVEL_SCHEDULING_TIMEOUT_SECONDS" \
        --retrieval-backend-profile "$NOVEL_RETRIEVAL_PROFILE" \
        --opensearch-url "$NOVEL_OPENSEARCH_URL" \
        --embedding-url "$NOVEL_EMBEDDING_URL" \
        --reranker-url "$NOVEL_RERANKER_URL"; then
        break
    fi

    status_snapshot || break

    waiting="$(first_task_in_status waiting_retry blocked)"
    if [[ -n "$waiting" ]]; then
        verdict="$(classify_task "$waiting")"
        action="$(classification_field action <<<"$verdict")"
        reason="$(classification_field reason <<<"$verdict")"
        log "[classify] $waiting -> ${action:-unreadable} (${reason:-no reason reported})"
        case "$action" in
            retry_under_policy)
                revision="$(task_field "$waiting" task_revision)"
                [[ -n "$revision" ]] || revision=1
                must "retry $waiting" "$NOVEL_AGENT" runtime --database-url "$NOVEL_DATABASE_URL" \
                    retry --project-id "$NOVEL_PROJECT_ID" --run-id "$NOVEL_RUN_ID" \
                    --task-id "$waiting" --observed-revision "$revision" \
                    --command-id "retry.${waiting}.${revision}.${slice}" \
                    --actor-id "$NOVEL_AUTHOR_ID" \
                    --reason "confirmed transient failure, nothing in flight" || break
                ;;
            reconcile_first|replay_completed)
                log "[stop] $waiting needs reconciliation or replay, not a blind retry"
                break
                ;;
            escalate_budget)
                log "[stop] $waiting is budget-exhausted; escalate explicitly, do not auto-top-up"
                break
                ;;
            "")
                log "[stop] $waiting: the classification could not be read"
                break
                ;;
            *)
                log "[stop] $waiting: $action is not an automatic step"
                break
                ;;
        esac
        continue
    fi

    acceptance="$(first_task_in_status waiting_input)"
    if [[ -n "$acceptance" ]]; then
        # Author rulings are a separate owner; this driver never decides them.
        log "[author] $acceptance is waiting for its owner's ruling"
        break
    fi

    if [[ -n "$(first_task_in_status ready pending)" ]]; then
        continue
    fi
    log "[stage] no ready, retry or acceptance work remains in this snapshot"
    break
done

# Bookkeeping runs before the exit status is decided, and its own failures count.
if stage_exit_satisfied; then
    STAGE_OK=0
fi

if [[ $FAILURES -gt 0 ]]; then
    log "[exit] ${FAILURES} subcommand failure(s); see $RUN_LOG"
    exit 1
fi
if [[ $STAGE_OK -ne 0 ]]; then
    log "[exit] ${STAGE} stopped without proving its exit evidence"
    exit 1
fi
log "[exit] ${STAGE} complete and verified"
exit 0
