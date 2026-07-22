from __future__ import annotations

import json

from pytest import CaptureFixture

from novel_agent.cli import main


def test_doctor_reports_non_secret_configuration(capsys: CaptureFixture[str]) -> None:
    assert main(["doctor"]) == 0

    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["environment"] == "development"
    assert report["log_level"] == "INFO"
    assert report["python"].startswith("3.12.")
