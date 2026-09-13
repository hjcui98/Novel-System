"""N4: the stage driver fails when its work fails, and stops on evidence.

The frozen v23 driver broke out of its loop on a failed slice and then ran
successful bookkeeping commands, so the script could exit 0 after a failure.  It
also stopped when a snapshot happened to contain no READY task, which is not the
same statement as "this stage's evidence exists".

These tests run the real driver against a stub CLI.  The stub is an executable
that answers the driver's subcommands from a scripted transcript, so what is
under test is the driver's own control flow and exit status.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

DRIVER = Path(__file__).resolve().parents[2] / "scripts" / "60_run_stage.sh"


WAITING_TASK = (
    '[{"task_id":"task.1","status":"waiting_retry","task_revision":1,"block_cause":null}]'
)
WAITING_TASK_NO_CAUSE = '[{"task_id":"task.1","status":"waiting_retry","task_revision":1}]'
RETRYABLE = (
    '{"classification":{"action":"retry_under_policy",'
    '"reason":"provider_transient is transient"},"safe_to_retry":true}'
)
RECONCILE = (
    '{"classification":{"action":"reconcile_first",'
    '"reason":"a send is unresolved"},"safe_to_retry":false}'
)


def _stub_cli(tmp_path: Path, body: str) -> Path:
    """A fake `novel-agent` that answers from scripted state."""

    script = tmp_path / "novel-agent"
    script.write_text(
        "#!/usr/bin/env bash\nset -uo pipefail\n" + textwrap.dedent(body),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _environment(tmp_path: Path, cli: Path, *, python: str) -> dict[str, str]:
    """The variables `environment.sh` would provide, pointed at the stub."""

    workspace = tmp_path / "workspace"
    for name in ("state", "logs"):
        (workspace / name).mkdir(parents=True, exist_ok=True)
    environment = {
        **os.environ,
        "NOVEL_AGENT": str(cli),
        "NOVEL_PYTHON": python,
        "NOVEL_WORKSPACE": str(workspace),
        "NOVEL_DATABASE_URL": "postgresql://stub",
        "NOVEL_RUN_ID": "run.stub",
        "NOVEL_PROJECT_ID": "project.stub",
        "NOVEL_POLICY": str(workspace / "policy.json"),
        "NOVEL_MANIFEST": str(workspace / "manifest.json"),
        "NOVEL_OBJECT_STORE": str(workspace / "objects"),
        "NOVEL_ENDPOINT_PROFILE": "stub-profile",
        "NOVEL_SCHEDULING_TIMEOUT_SECONDS": "5",
        "NOVEL_RETRIEVAL_PROFILE": "stub-retrieval",
        "NOVEL_OPENSEARCH_URL": "http://127.0.0.1:9200",
        "NOVEL_EMBEDDING_URL": "http://127.0.0.1:8081",
        "NOVEL_RERANKER_URL": "http://127.0.0.1:8082",
        "NOVEL_AUTHOR_ID": "author",
        "NOVEL_STATE": str(workspace / "state"),
        "NOVEL_LOGS": str(workspace / "logs"),
        # Transcripts the stub echoes; keeping them here keeps the stub readable
        # and every line inside the project's width.
        "WAITING_RETRY": WAITING_TASK,
        "WAITING_NO_CAUSE": WAITING_TASK_NO_CAUSE,
        "RETRYABLE": RETRYABLE,
        "RECONCILE": RECONCILE,
    }
    return environment


# The environment.sh in the repository sources the worktree's own environment;
# the tests replace it with a stub so the driver can run without infrastructure.
# The driver sources `../environment.sh` for its variables.  The tests redirect
# only that one line, so everything else under test is the real script.
_ENVIRONMENT_SOURCE = re.compile(r'^source "\$SCRIPT_DIR/\.\./environment\.sh"$', re.MULTILINE)


def _driver(tmp_path: Path, environment_file: Path) -> Path:
    """The real driver with only its environment source redirected."""

    text = DRIVER.read_text(encoding="utf-8")
    patched, replacements = _ENVIRONMENT_SOURCE.subn('source "$STUB_ENVIRONMENT_FILE"', text)
    assert replacements == 1, replacements
    path = tmp_path / "60_run_stage.sh"
    path.write_text(patched, encoding="utf-8")
    path.chmod(0o755)
    return path


def _run(
    tmp_path: Path, body: str, *, stage: str = "g0", slices: str = "3"
) -> subprocess.CompletedProcess[str]:
    cli = _stub_cli(tmp_path, body)
    environment_file = tmp_path / "environment.sh"
    environment_file.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    driver = _driver(tmp_path, environment_file)
    environment = _environment(
        tmp_path,
        cli,
        python=os.environ.get("NOVEL_TEST_PYTHON", "python3"),
    )
    environment["STUB_ENVIRONMENT_FILE"] = str(environment_file)
    return subprocess.run(
        ["bash", str(driver), stage, slices],
        capture_output=True,
        text=True,
        env=environment,
        cwd=tmp_path,
        timeout=120,
    )


def test_a_failed_advance_makes_the_whole_script_fail(tmp_path: Path) -> None:
    """The v23 defect: a break after a failure, then successful bookkeeping."""

    result = _run(
        tmp_path,
        """
        case "$*" in
            *" status "*) echo "[]"; exit 0 ;;
            *" roots "*) echo '{"committed_chapters":0,"committed_volumes":0}'; exit 0 ;;
            *" advance "*) echo "advance exploded"; exit 3 ;;
        esac
        exit 0
        """,
    )

    assert result.returncode == 1, result.stdout
    assert "[fail] advance g0-01 exited 3" in result.stdout
    assert "[exit]" in result.stdout


def test_a_failing_roots_read_is_not_a_pass(tmp_path: Path) -> None:
    """Bookkeeping failure must count, not be swallowed after a break."""

    result = _run(
        tmp_path,
        """
        case "$*" in
            *" status "*) echo "[]"; exit 0 ;;
            *" roots "*) echo "roots unavailable"; exit 4 ;;
            *" advance "*) echo "{}"; exit 0 ;;
        esac
        exit 0
        """,
    )

    assert result.returncode == 1, result.stdout
    # The read failure is recorded as a failure, not absorbed into a pass, and the
    # stage is not reported as verified.
    assert "[fail] roots exited 4" in result.stdout
    assert "complete and verified" not in result.stdout


def test_a_failed_final_roots_read_is_counted_once(tmp_path: Path) -> None:
    """The gate's own failed read must not be recorded twice.

    stage_exit_satisfied() reports the failed read through stage_roots(); wrapping
    that call in a second recording `must` would log one infrastructure failure as
    two, which misstates how much went wrong.
    """

    result = _run(
        tmp_path,
        """
        marker="$NOVEL_STATE/stub-advanced"
        case "$*" in
            *" status "*) echo "[]"; exit 0 ;;
            *" advance "*) : > "$marker"; echo "{}"; exit 0 ;;
            *" roots "*)
                # Succeed while the stage still has work, then fail at the gate.
                [[ -f "$marker" ]] && { echo "roots went away"; exit 4; }
                echo '{"committed_chapters":0,"committed_volumes":0}'
                exit 0 ;;
        esac
        exit 0
        """,
        slices="1",
    )

    assert result.returncode == 1, result.stdout
    assert "[exit] 1 subcommand failure(s)" in result.stdout, result.stdout


def test_each_command_snapshot_is_captured_from_its_own_stdout(tmp_path: Path) -> None:
    """Snapshots come from each command's own output, not from the shared log.

    The earlier version scraped `tail -n 1` of the run log.  That works only while
    every command happens to leave its result as the log's last line, and it hides
    the difference between "this command reported X" and "X is the most recent
    thing anyone wrote".  The driver now captures each command's stdout separately,
    which is what these files assert.
    """

    result = _run(
        tmp_path,
        """
        marker="$NOVEL_STATE/stub-reads"
        case "$*" in
            *" status "*)
                echo "status wrote nothing to stdout" >&2
                exit 0 ;;
            *" roots "*)
                # The pre-flight gate must not pass, or the loop never runs.
                reads=$(( $(cat "$marker" 2>/dev/null || echo 0) + 1 ))
                echo "$reads" > "$marker"
                if [[ "$reads" -ge 2 ]]; then
                    echo '{"committed_chapters":0,"committed_volumes":8}'
                else
                    echo '{"committed_chapters":0,"committed_volumes":0}'
                fi
                exit 0 ;;
        esac
        exit 0
        """,
        slices="2",
    )

    state = tmp_path / "workspace" / "state"
    # Each command's own capture exists and holds what that command printed.
    roots_out = (state / "roots.out").read_text(encoding="utf-8")
    status_out = (state / "status.out").read_text(encoding="utf-8")
    assert "committed_volumes" in roots_out, roots_out
    assert "committed_volumes" not in status_out, status_out
    # The parsed snapshot is the status command's result, NOT the roots payload it
    # would have inherited from the log.
    status = (state / "status.json").read_text(encoding="utf-8")
    assert "committed_volumes" not in status, status
    # An unreadable snapshot stops the loop before it can advance on stale state:
    # with no readable task list there is nothing to act on.
    assert "no ready, retry or acceptance work remains" in result.stdout, result.stdout
    assert "[stage] g0 exit not proven" not in result.stdout, result.stdout


def test_a_stage_with_proven_evidence_exits_zero(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        """
        case "$*" in
            *" status "*) echo "[]"; exit 0 ;;
            *" roots "*) echo '{"committed_chapters":0,"committed_volumes":8}'; exit 0 ;;
        esac
        exit 0
        """,
    )

    assert result.returncode == 0, result.stdout
    assert "exit evidence already present" in result.stdout


def test_a_retryable_task_is_retried_and_the_stage_still_proves_itself(
    tmp_path: Path,
) -> None:
    """The v23 shape: waiting_retry with no block_cause, classed provider_transient."""

    result = _run(
        tmp_path,
        """
        state_file="$NOVEL_STATE/stub-advances"
        case "$*" in
            *" status "*)
                if [[ -f "$state_file" ]]; then
                    if [[ "$(cat "$state_file")" -ge 1 ]]; then
                        echo '[{"task_id":"task.1","status":"succeeded","task_revision":1}]'
                    else
                        echo "$WAITING_RETRY"
                    fi
                else
                    echo "$WAITING_RETRY"
                fi
                exit 0 ;;
            *" classify "*)
                echo "$RETRYABLE"
                exit 0 ;;
            *" retry "*)
                echo "retried"; exit 0 ;;
            *" roots "*)
                if [[ -f "$NOVEL_STATE/stub-retried" ]]; then
                    echo '{"committed_chapters":0,"committed_volumes":8}'
                else
                    echo '{"committed_chapters":0,"committed_volumes":0}'
                fi
                exit 0 ;;
            *" advance "*)
                echo "advanced"; exit 0 ;;
        esac
        exit 0
        """,
        slices="2",
    )

    assert "[classify] task.1 -> retry_under_policy" in result.stdout, result.stdout


def test_an_uncertain_task_is_never_retried(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        """
        case "$*" in
            *" status "*) echo "$WAITING_RETRY"; exit 0 ;;
            *" classify "*) echo "$RECONCILE"; exit 0 ;;
            *" retry "*) echo "SHOULD NOT RETRY"; exit 9 ;;
            *" roots "*) echo '{"committed_chapters":0,"committed_volumes":0}'; exit 0 ;;
            *" advance "*) echo "{}"; exit 0 ;;
        esac
        exit 0
        """,
        slices="1",
    )

    assert "SHOULD NOT RETRY" not in result.stdout
    assert "needs reconciliation or replay" in result.stdout
    # It stopped rather than proving the stage, so the stage itself failed.
    assert result.returncode == 1


def test_an_unreadable_classification_stops_instead_of_retrying(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        """
        case "$*" in
            *" status "*) echo "$WAITING_NO_CAUSE"; exit 0 ;;
            *" classify "*) echo "classify unavailable"; exit 5 ;;
            *" retry "*) echo "SHOULD NOT RETRY"; exit 9 ;;
            *" roots "*) echo '{"committed_chapters":0,"committed_volumes":0}'; exit 0 ;;
            *" advance "*) echo "{}"; exit 0 ;;
        esac
        exit 0
        """,
        slices="1",
    )

    assert "SHOULD NOT RETRY" not in result.stdout
    assert result.returncode == 1


def test_a_stage_that_cannot_prove_chapters_fails(tmp_path: Path) -> None:
    """G2 needs five committed chapters; eight volumes alone are not the exit."""

    result = _run(
        tmp_path,
        """
        case "$*" in
            *" status "*) echo "[]"; exit 0 ;;
            *" roots "*) echo '{"committed_chapters":3,"committed_volumes":8}'; exit 0 ;;
        esac
        exit 0
        """,
        stage="g2",
    )

    assert result.returncode == 1, result.stdout
    assert "3/5 committed chapters" in result.stdout


def test_a_stage_with_its_chapters_proven_exits_zero(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        """
        case "$*" in
            *" status "*) echo "[]"; exit 0 ;;
            *" roots "*) echo '{"committed_chapters":5,"committed_volumes":8}'; exit 0 ;;
        esac
        exit 0
        """,
        stage="g2",
    )

    assert result.returncode == 0, result.stdout


def test_an_unknown_stage_is_refused(tmp_path: Path) -> None:
    result = _run(tmp_path, "exit 0\n", stage="g9")

    assert result.returncode == 64
    assert "unknown stage" in result.stderr


def test_the_driver_does_not_pattern_match_log_words() -> None:
    """The old decision procedure must not survive anywhere in the driver."""

    text = DRIVER.read_text(encoding="utf-8")
    # The header explains what the old driver did, so these words may appear in a
    # comment; they must not appear in code.
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))

    for forbidden in ("MODEL_RUNTIME_UNAVAILABLE", "block_cause", 'case "$reason"'):
        assert forbidden not in code, forbidden
    # It reads the canonical classification instead.
    assert "classify --task-id" in code
    # And it does not swallow a failure through a pipeline or a blanket success.
    assert "| tee" not in code
    assert "|| true" not in code


@pytest.mark.parametrize(
    "stage,volumes,chapters,expected",
    [
        ("g0", 8, 0, 0),
        ("g0", 7, 0, 1),
        ("g1", 8, 2, 0),
        ("g1", 8, 1, 1),
        ("g3", 8, 20, 0),
        ("g3", 8, 19, 1),
    ],
)
def test_each_stage_gate_is_enforced(
    tmp_path: Path, stage: str, volumes: int, chapters: int, expected: int
) -> None:
    result = _run(
        tmp_path,
        f"""
        case "$*" in
            *" status "*) echo "[]"; exit 0 ;;
            *" roots "*)
                echo '{{"committed_chapters":{chapters},"committed_volumes":{volumes}}}'
                exit 0 ;;
        esac
        exit 0
        """,
        stage=stage,
    )

    assert result.returncode == expected, (stage, volumes, chapters, result.stdout)
