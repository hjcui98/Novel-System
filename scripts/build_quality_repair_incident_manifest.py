#!/usr/bin/env python3
"""WP0: freeze immutable incident artifact manifest for C20/C21 quality repair."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
from pathlib import Path

DEFAULT_RELATIVE_PATHS = (
    "e2e_paired_report.json",
    "memory_write_pause_trace.json",
    "c20_c95.log",
    "progress_manifest.json",
    "experiment_manifest.json",
    "source_state_manifest.json",
    "flow_summary.json",
    "scenario_run.json",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--incident-dir",
        type=Path,
        required=True,
        help="Report directory containing C20/C21 accident artifacts",
    )
    parser.add_argument("--code-commit", required=True)
    parser.add_argument("--configuration-fingerprint", default="")
    parser.add_argument("--base-commit", default="")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Defaults to <incident-dir>/quality_repair_incident_manifest.json",
    )
    args = parser.parse_args()
    root: Path = args.incident_dir
    if not root.is_dir():
        raise SystemExit(f"incident dir not found: {root}")

    entries = []
    for relative in DEFAULT_RELATIVE_PATHS:
        path = root / relative
        if not path.is_file():
            continue
        media, _ = mimetypes.guess_type(path.name)
        entries.append(
            {
                "relative_path": relative,
                "media_type": media or "application/octet-stream",
                "byte_length": path.stat().st_size,
                "sha256": sha256_file(path),
                "code_commit": args.code_commit,
                "configuration_fingerprint": args.configuration_fingerprint or None,
                "base_commit": args.base_commit or None,
            }
        )
    # Include nested freeze/proposal artifacts when present.
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in DEFAULT_RELATIVE_PATHS:
            continue
        if not any(
            token in rel
            for token in (
                "paired",
                "proposal",
                "rejection",
                "feedback",
                "checkpoint",
                "raw_response",
                "freeze",
            )
        ):
            continue
        if path.stat().st_size > 32 * 1024 * 1024:
            continue
        media, _ = mimetypes.guess_type(path.name)
        entries.append(
            {
                "relative_path": rel,
                "media_type": media or "application/octet-stream",
                "byte_length": path.stat().st_size,
                "sha256": sha256_file(path),
                "code_commit": args.code_commit,
                "configuration_fingerprint": args.configuration_fingerprint or None,
                "base_commit": args.base_commit or None,
            }
        )

    payload = {
        "manifest_version": "quality-repair-incident-v1",
        "incident_dir": str(root),
        "entry_count": len(entries),
        "entries": entries,
    }
    output = args.output or (root / "quality_repair_incident_manifest.json")
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(output)


if __name__ == "__main__":
    main()
