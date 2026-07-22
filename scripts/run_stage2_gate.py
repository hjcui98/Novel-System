#!/usr/bin/env python3
"""Evaluate one explicit Stage 2 gate evidence document."""

from __future__ import annotations

import argparse
from pathlib import Path

from novel_agent.domain.stage2 import Stage2GateEvidence
from novel_agent.services.stage2_gate import Stage2GateEvaluator


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    evidence = Stage2GateEvidence.model_validate_json(args.evidence.read_bytes(), strict=True)
    report = Stage2GateEvaluator().evaluate(evidence)
    payload = report.model_dump_json(indent=2) + "\n"
    if args.output is None:
        print(payload, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    main()
