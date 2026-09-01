#!/usr/bin/env python3
"""Run the U3.5 Temporal feasibility spike against a local test server.

Creates a temporary NS object root. Does not use production endpoints, Gold,
private chapter text, or the production assembly spec.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from novel_agent.runtime.temporal_spike import run_spike


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--object-root", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    root = args.object_root
    cleanup = None
    if root is None:
        cleanup = tempfile.TemporaryDirectory(prefix="ns-u35-")
        root = Path(cleanup.name)
    try:
        report = run_spike(root)
        text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
        print(text)
        if args.report is not None:
            args.report.write_text(text + "\n", encoding="utf-8")
    finally:
        if cleanup is not None:
            cleanup.cleanup()


if __name__ == "__main__":
    main()
