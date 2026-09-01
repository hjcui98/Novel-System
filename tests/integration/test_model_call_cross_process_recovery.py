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
