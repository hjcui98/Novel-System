"""Canonical serialization and content-address helpers shared by all runtimes."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from novel_agent.domain.benchmark import (
    ChapterSummaryRootDocument,
    PlanRootDocument,
    TextRootDocument,
)
from novel_agent.domain.ids import ArtifactId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.text import QuoteHash
from novel_agent.services.artifacts import sha256_id


def canonical_json_bytes(value: Any) -> bytes:
    """Encode JSON-compatible data with a process-independent representation."""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def content_id(value: Any) -> ArtifactId:
    """Return the canonical SHA-256 identity of JSON-compatible data."""

    return sha256_id(canonical_json_bytes(value))


def text_root_content_id(root: TextRootDocument) -> ArtifactId:
    return content_id(root.model_dump(mode="json", exclude={"root_hash"}))


def plan_root_content_id(root: PlanRootDocument) -> ArtifactId:
    return content_id(root.model_dump(mode="json", exclude={"root_hash"}))


def summary_root_content_id(root: ChapterSummaryRootDocument) -> ArtifactId:
    return content_id(root.model_dump(mode="json", exclude={"root_hash"}))


def world_root_content_id(root: WorldRootDocument) -> ArtifactId:
    return content_id(root.model_dump(mode="json", exclude={"root_hash"}))


def quote_hash(text: str) -> QuoteHash:
    return QuoteHash(f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}")
