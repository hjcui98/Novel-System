from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_runtime_model_replay_returns_to_stage4_without_memory_or_provider_repeat(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[2]
    script = repository / "scripts" / "run_runtime_model_replay_recovery.py"
    database = tmp_path / "runtime.db"
    objects = tmp_path / "objects"
    provider_count = tmp_path / "provider-count.txt"
    memory_count = tmp_path / "memory-count.txt"
    evidence = tmp_path / "recovery-evidence.json"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository / "src"), str(repository), environment.get("PYTHONPATH", ""))
    )
    common = [
        sys.executable,
        str(script),
        "--database",
        str(database),
        "--objects",
        str(objects),
        "--provider-count",
        str(provider_count),
        "--memory-count",
        str(memory_count),
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
    recovered = subprocess.run(
        [*common, "--phase", "recover", "--output", str(evidence)],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert recovered.returncode == 0, recovered.stderr
    payload = json.loads(evidence.read_text(encoding="utf-8"))

    assert payload == {
        "attempt_no": 2,
        "checkpoint_id": "checkpoint.cross-process.runtime-replay",
        "logical_phase": "plan_revision",
        "memory_call_count": 1,
        "parsed": {"value": "replayed"},
        "provider_call_count": 1,
        "response_consumed": True,
        "status": "recovered",
    }
