from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_raw_before_parse_recovery_reuses_durable_budget_and_provider_call(tmp_path: Path) -> None:
    repository = Path(__file__).parents[2]
    script = repository / "scripts" / "run_model_call_reparse_recovery.py"
    database = tmp_path / "runtime.db"
    objects = tmp_path / "objects"
    provider_count = tmp_path / "provider-count.txt"
    evidence = tmp_path / "reparse-evidence.json"
    environment = os.environ.copy()
    source_root = str(repository / "src")
    environment["PYTHONPATH"] = source_root + os.pathsep + environment.get("PYTHONPATH", "")
    common = [
        sys.executable,
        str(script),
        "--database",
        str(database),
        "--objects",
        str(objects),
        "--provider-count",
        str(provider_count),
    ]

    sent = subprocess.run(
        [*common, "--phase", "send"],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert sent.returncode == 37, sent.stderr

    reparsed = subprocess.run(
        [*common, "--phase", "reparse", "--output", str(evidence)],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert reparsed.returncode == 0, reparsed.stderr
    payload = json.loads(evidence.read_text(encoding="utf-8"))

    assert payload["status"] == "reparsed"
    assert payload["provider_call_count"] == 1
    assert payload["ledger_request_count"] == 1
    assert payload["parsed"] == {"answer": "durable"}
    assert payload["record_request_id"] == "request.cross-process.raw-before-parse"
    assert payload["raw_artifact_ref"] is not None
    assert payload["reasoning_included_in_completion_tokens"] is False
    budget = payload["effective_budget"]
    assert budget["budget_source"] == "explicit_request"
    assert budget["context_limit"] == 8_192
    assert budget["body_output_budget"] == 2_048
    assert budget["thinking_budget"] == 128
    assert budget["total_output_budget"] == 2_176
    assert budget["safety_allowance_tokens"] == 64
    assert budget["reserved_sequence_tokens"] == budget["estimated_input_tokens"] + 2_176 + 64
    assert budget["available_input_tokens"] == 8_192 - 2_176 - 64


def test_elastic_output_growth_resumes_from_the_durable_ledger(tmp_path: Path) -> None:
    """A second process grows a retained truncation without resending it."""

    repository = Path(__file__).parents[2]
    script = repository / "scripts" / "run_model_call_reparse_recovery.py"
    database = tmp_path / "runtime.db"
    objects = tmp_path / "objects"
    provider_count = tmp_path / "provider-count.txt"
    evidence = tmp_path / "elastic-evidence.json"
    environment = os.environ.copy()
    source_root = str(repository / "src")
    environment["PYTHONPATH"] = source_root + os.pathsep + environment.get("PYTHONPATH", "")
    common = [
        sys.executable,
        str(script),
        "--database",
        str(database),
        "--objects",
        str(objects),
        "--provider-count",
        str(provider_count),
    ]

    truncated = subprocess.run(
        [*common, "--phase", "truncate"],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert truncated.returncode == 40, truncated.stderr
    assert provider_count.read_text(encoding="utf-8") == "1\n"

    resumed = subprocess.run(
        [*common, "--phase", "resume", "--output", str(evidence)],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert resumed.returncode == 0, resumed.stderr
    payload = json.loads(evidence.read_text(encoding="utf-8"))

    # The retained attempt was replayed from its raw artifact: only the larger
    # allowance reached the provider, in a process that never saw the first one.
    assert payload["provider_call_count"] == 2
    assert payload["ledger_request_count"] == 2
    assert payload["attempt_request_ids"] == [
        "request.cross-process.elastic-output",
        "request.cross-process.elastic-output.output-retry1",
    ]
    assert payload["attempt_output_tokens"] == [2_048, 10]
    assert payload["record_request_id"] == "request.cross-process.elastic-output.output-retry1"
    assert payload["parsed"] == {"answer": "grown"}
