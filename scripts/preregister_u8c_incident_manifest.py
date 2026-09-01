#!/usr/bin/env python3
"""Freeze the U8-C development/held-out incident identity split.

The manifest is deliberately an execution boundary, not a recovery reasoner.  It
records complete run identities before either run is evaluated, fixes the source
and policy inputs, and rejects split leakage (including a crash/retry chain split
between development and held-out).  The output is write-once so a later result
cannot be relabeled as pre-registered evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

Split = Literal["development", "held_out"]
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SPLITS: tuple[Split, ...] = ("development", "held_out")
DEFAULT_CHAPTER_SETTLEMENT_TIMEOUT_SECONDS = 120.0
DEFAULT_SETTLEMENT_OUTPUT_TOKENS = 8_000
DEFAULT_SETTLEMENT_TOKEN_BUDGET = 128_000
DEFAULT_SETTLEMENT_MAX_TOTAL_MODEL_CALLS = 12
MAX_CHAPTER_SETTLEMENT_TIMEOUT_SECONDS = 900.0
MAX_SETTLEMENT_OUTPUT_TOKENS = 131_072


@dataclass(frozen=True)
class IncidentSpec:
    """One complete incident identity reserved before a run starts."""

    split: Split
    incident_id: str
    project_id: str
    run_id: str
    database_descriptor: str
    basis_commit: str
    crash_chain_id: str


def _require_sha256(value: str, field: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a sha256:<64 lowercase hex> identity")
    return value


def _require_non_empty(value: str, field: str) -> str:
    if not value.strip():
        raise ValueError(f"{field} must be non-empty")
    return value


def _validate_problem_identity(
    value: object,
    *,
    source_commit: str,
    source_text_root: str,
    cutoff_chapter: int,
) -> dict[str, object]:
    """Validate the optional source-bound problem identity before any run."""

    if not isinstance(value, dict):
        raise ValueError("problem_identity must be a JSON object")
    required = ("need_id", "question_id", "need_query", "semantic_question", "facet")
    for field in required:
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError(f"problem_identity.{field} must be non-empty")
    if not str(value["need_id"]).startswith("need."):
        raise ValueError("problem_identity.need_id must use the Need namespace")
    if value["need_query"] != str(value["need_query"]).strip():
        raise ValueError("problem_identity.need_query must not have surrounding whitespace")
    if value["semantic_question"] != str(value["semantic_question"]).strip():
        raise ValueError("problem_identity.semantic_question must not have surrounding whitespace")
    if value["facet"] not in {
        "current_state",
        "relation_state",
        "causal_history",
        "knowledge_boundary",
        "setup",
        "commitment",
        "unresolved_status",
    }:
        raise ValueError(f"unsupported problem_identity.facet: {value['facet']!r}")
    if value.get("source_commit") != source_commit:
        raise ValueError("problem_identity.source_commit must match manifest source commit")
    if value.get("source_text_root") != source_text_root:
        raise ValueError("problem_identity.source_text_root must match manifest TextRoot")
    if value.get("cutoff_chapter") != cutoff_chapter:
        raise ValueError("problem_identity.cutoff_chapter must match manifest cutoff")
    requirement = value.get("source_evidence_requirement")
    if requirement is not None:
        if not isinstance(requirement, dict):
            raise ValueError("problem_identity.source_evidence_requirement must be an object")
        required = (
            "source_artifact_id",
            "source_chapter_index",
            "source_chapter_id",
            "required_span",
            "required_consequence_markers",
        )
        for field in required:
            if field not in requirement:
                raise ValueError(f"problem_identity.source_evidence_requirement is missing {field}")
        if requirement["source_artifact_id"] != source_text_root:
            raise ValueError(
                "source_evidence_requirement.source_artifact_id must match manifest TextRoot"
            )
        chapter_index = requirement["source_chapter_index"]
        if (
            not isinstance(chapter_index, int)
            or chapter_index < 0
            or chapter_index > cutoff_chapter
        ):
            raise ValueError(
                "source_evidence_requirement.source_chapter_index must be within cutoff"
            )
        if (
            not isinstance(requirement["source_chapter_id"], str)
            or not requirement["source_chapter_id"].strip()
        ):
            raise ValueError("source_evidence_requirement.source_chapter_id must be non-empty")
        span = requirement["required_span"]
        if not isinstance(span, dict):
            raise ValueError("source_evidence_requirement.required_span must be an object")
        if not isinstance(span.get("block_id"), str) or not span["block_id"].strip():
            raise ValueError("source_evidence_requirement.required_span.block_id must be non-empty")
        if span.get("range_unit", "unicode_codepoint") != "unicode_codepoint":
            raise ValueError("source_evidence_requirement.required_span must use codepoints")
        start, end = span.get("start"), span.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
            raise ValueError("source_evidence_requirement.required_span has invalid range")
        markers = requirement["required_consequence_markers"]
        if not isinstance(markers, list | tuple) or not markers:
            raise ValueError(
                "source_evidence_requirement.required_consequence_markers must be non-empty"
            )
        if any(
            not isinstance(marker, str) or not marker.strip() or marker != marker.strip()
            for marker in markers
        ):
            raise ValueError("source-bound consequence markers must be non-empty and trimmed")
        if len(set(markers)) != len(markers):
            raise ValueError("source-bound consequence markers must be unique")
    return dict(value)


def parse_incident_spec(raw: str) -> IncidentSpec:
    """Parse ``split|incident|project|run|database|basis|crash-chain``."""

    fields = raw.split("|")
    if len(fields) != 7:
        raise ValueError(
            "--incident must be "
            "split|incident_id|project_id|run_id|database|basis_commit|crash_chain_id"
        )
    split, incident_id, project_id, run_id, database, basis_commit, crash_chain_id = fields
    if split not in _SPLITS:
        raise ValueError(f"incident split must be one of {_SPLITS}, got {split!r}")
    return IncidentSpec(
        split=split,
        incident_id=_require_non_empty(incident_id, "incident_id"),
        project_id=_require_non_empty(project_id, "project_id"),
        run_id=_require_non_empty(run_id, "run_id"),
        database_descriptor=_require_non_empty(database, "database_descriptor"),
        basis_commit=_require_sha256(basis_commit, "basis_commit"),
        crash_chain_id=_require_non_empty(crash_chain_id, "crash_chain_id"),
    )


def _stream_file_count(stream_root: Path) -> int:
    if not stream_root.is_dir():
        raise ValueError(f"benchmark stream root is not a directory: {stream_root}")
    names = {path.name for path in stream_root.glob("*.txt") if path.is_file()}
    expected = {
        "000_prologue_and_frontmatter.txt",
        *(f"{index:03d}.txt" for index in range(1, 301)),
    }
    if names != expected:
        missing = sorted(expected - names)
        unexpected = sorted(names - expected)
        raise ValueError(
            "benchmark stream must contain exactly chapters 000..300; "
            f"missing={missing[:5]}, unexpected={unexpected[:5]}"
        )
    return len(names)


def _validate_incidents(incidents: tuple[IncidentSpec, ...]) -> None:
    if len(incidents) < 2:
        raise ValueError(
            "U8-C manifest requires at least one development and one held_out incident"
        )
    if {incident.split for incident in incidents} != set(_SPLITS):
        raise ValueError("U8-C manifest requires both development and held_out incidents")
    for field in ("incident_id", "project_id", "run_id", "database_descriptor", "crash_chain_id"):
        values = [getattr(incident, field) for incident in incidents]
        if len(values) != len(set(values)):
            raise ValueError(f"incident {field} values must be unique across the split")


def _validate_runtime_budget(
    *,
    chapter_settlement_timeout_seconds: float,
    settlement_output_tokens: int,
    settlement_token_budget: int,
    settlement_max_total_model_calls: int,
) -> None:
    """Validate the campaign-local resource tranche before writing the manifest."""

    if (
        isinstance(chapter_settlement_timeout_seconds, bool)
        or not math.isfinite(chapter_settlement_timeout_seconds)
        or chapter_settlement_timeout_seconds <= 0
        or chapter_settlement_timeout_seconds > MAX_CHAPTER_SETTLEMENT_TIMEOUT_SECONDS
    ):
        raise ValueError("chapter_settlement_timeout_seconds must be in (0, 900]")
    if (
        isinstance(settlement_output_tokens, bool)
        or settlement_output_tokens < 1
        or settlement_output_tokens > MAX_SETTLEMENT_OUTPUT_TOKENS
    ):
        raise ValueError("settlement_output_tokens must be in [1, 131072]")
    if isinstance(settlement_token_budget, bool) or settlement_token_budget < 1:
        raise ValueError("settlement_token_budget must be positive")
    if isinstance(settlement_max_total_model_calls, bool) or settlement_max_total_model_calls < 1:
        raise ValueError("settlement_max_total_model_calls must be positive")


def build_manifest(
    *,
    campaign_id: str,
    preregistered_at: str,
    source_project_id: str,
    source_commit: str,
    source_text_root: str,
    benchmark_stream_root: Path,
    code_source_fingerprint: str,
    configuration_fingerprint: str,
    model_profile: str,
    cutoff_chapter: int,
    target_chapter: int,
    access_scope: str,
    incidents: tuple[IncidentSpec, ...],
    memory_write_validation_only: bool = False,
    problem_identity: dict[str, object] | None = None,
    chapter_settlement_timeout_seconds: float = DEFAULT_CHAPTER_SETTLEMENT_TIMEOUT_SECONDS,
    settlement_output_tokens: int = DEFAULT_SETTLEMENT_OUTPUT_TOKENS,
    settlement_token_budget: int = DEFAULT_SETTLEMENT_TOKEN_BUDGET,
    settlement_max_total_model_calls: int = DEFAULT_SETTLEMENT_MAX_TOTAL_MODEL_CALLS,
) -> dict[str, object]:
    """Build and validate one immutable U8-C pre-registration payload."""

    _require_non_empty(campaign_id, "campaign_id")
    _require_non_empty(preregistered_at, "preregistered_at")
    _require_non_empty(source_project_id, "source_project_id")
    _require_sha256(source_commit, "source_commit")
    _require_sha256(source_text_root, "source_text_root")
    _require_sha256(code_source_fingerprint, "code_source_fingerprint")
    _require_sha256(configuration_fingerprint, "configuration_fingerprint")
    _require_non_empty(model_profile, "model_profile")
    _require_non_empty(access_scope, "access_scope")
    _validate_runtime_budget(
        chapter_settlement_timeout_seconds=chapter_settlement_timeout_seconds,
        settlement_output_tokens=settlement_output_tokens,
        settlement_token_budget=settlement_token_budget,
        settlement_max_total_model_calls=settlement_max_total_model_calls,
    )
    if cutoff_chapter < 0 or target_chapter != cutoff_chapter + 1:
        raise ValueError(
            "U8-C incident runs must target exactly one chapter after a non-negative cutoff"
        )
    _validate_incidents(incidents)
    normalized_problem_identity = (
        None
        if problem_identity is None
        else _validate_problem_identity(
            problem_identity,
            source_commit=source_commit,
            source_text_root=source_text_root,
            cutoff_chapter=cutoff_chapter,
        )
    )

    payload: dict[str, object] = {
        "schema": "u8c-incident-preregistration.v1",
        "status": "PREREGISTERED",
        "campaign_id": campaign_id,
        "preregistered_at": preregistered_at,
        "source": {
            "project_id": source_project_id,
            "commit": source_commit,
            "text_root": source_text_root,
            "benchmark_stream_root": str(benchmark_stream_root.resolve()),
            "benchmark_stream_file_count": _stream_file_count(benchmark_stream_root),
            "future_gold_or_evaluator_inputs": False,
        },
        "fixed_runtime": {
            "code_source_fingerprint": code_source_fingerprint,
            "configuration_fingerprint": configuration_fingerprint,
            "model_profile": model_profile,
            "cutoff_chapter": cutoff_chapter,
            "target_chapter": target_chapter,
            "access_scope": access_scope,
            "failure_class": "canon_extraction_gap",
            "budget": {
                "chapter_settlement_timeout_seconds": chapter_settlement_timeout_seconds,
                "settlement_output_tokens": settlement_output_tokens,
                "settlement_token_budget": settlement_token_budget,
                "settlement_max_total_model_calls": settlement_max_total_model_calls,
                "max_tasks": 1,
                "runtime_parallelism": 1,
                "memory_write_validation_only": memory_write_validation_only,
            },
            "online_policy_mutation": False,
            "evaluator_feedback_writeback": False,
        },
        "identity_split": [asdict(incident) for incident in incidents],
        "problem_identity": normalized_problem_identity,
        "split_rules": {
            "unit": "complete_incident_identity",
            "crash_retry_chain_must_stay_whole": True,
            "same_identity_reuse_forbidden": True,
            "held_out_is_not_read_until_development_is_frozen": True,
            "results_may_not_change_this_manifest": True,
        },
        "u8c_comparison": {
            "baseline": "existing deterministic FailurePolicy",
            "reasoner_enabled": False,
            "candidate_action_allowlist": [
                "graph_curator",
                "ordinary_curator",
                "review_required",
            ],
            "admission_requires": [
                "same typed failure and same safety boundary have multiple "
                "validator-accepted actions",
                "receipt/state cannot uniquely select the action",
                "held_out comparison beats deterministic baseline without Canon or Skill mutation",
            ],
        },
    }
    return payload


def _write_once(path: Path, payload: object) -> None:
    if path.exists():
        raise RuntimeError(f"U8-C manifest refuses to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--preregistered-at", required=True)
    parser.add_argument("--source-project-id", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-text-root", required=True)
    parser.add_argument("--benchmark-stream-root", type=Path, required=True)
    parser.add_argument("--code-source-fingerprint", required=True)
    parser.add_argument("--configuration-fingerprint", required=True)
    parser.add_argument("--model-profile", required=True)
    parser.add_argument("--cutoff-chapter", type=int, required=True)
    parser.add_argument("--target-chapter", type=int, required=True)
    parser.add_argument("--access-scope", required=True)
    parser.add_argument(
        "--memory-write-validation-only",
        action="store_true",
        help="pin the isolated U8-C maintenance runs to validation-only mode",
    )
    parser.add_argument(
        "--chapter-settlement-timeout-seconds",
        type=float,
        default=DEFAULT_CHAPTER_SETTLEMENT_TIMEOUT_SECONDS,
        help="freeze the isolated Curator transport timeout in the campaign manifest",
    )
    parser.add_argument(
        "--settlement-output-tokens",
        type=int,
        default=DEFAULT_SETTLEMENT_OUTPUT_TOKENS,
        help="freeze the isolated Curator output cap in the campaign manifest",
    )
    parser.add_argument(
        "--settlement-token-budget",
        type=int,
        default=DEFAULT_SETTLEMENT_TOKEN_BUDGET,
        help="freeze the isolated cumulative Curator token tranche",
    )
    parser.add_argument(
        "--settlement-max-total-model-calls",
        type=int,
        default=DEFAULT_SETTLEMENT_MAX_TOTAL_MODEL_CALLS,
        help="freeze the isolated cumulative Curator model-call cap",
    )
    parser.add_argument(
        "--problem-identity",
        type=Path,
        help="optional JSON file containing a source-bound pre-registered problem identity",
    )
    parser.add_argument(
        "--incident",
        action="append",
        required=True,
        help="split|incident_id|project_id|run_id|database|basis_commit|crash_chain_id",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        problem_identity = (
            None
            if args.problem_identity is None
            else json.loads(args.problem_identity.read_text(encoding="utf-8"))
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        parser.error(f"cannot read --problem-identity: {exc}")
    incidents = tuple(parse_incident_spec(raw) for raw in args.incident)
    payload = build_manifest(
        campaign_id=args.campaign_id,
        preregistered_at=args.preregistered_at,
        source_project_id=args.source_project_id,
        source_commit=args.source_commit,
        source_text_root=args.source_text_root,
        benchmark_stream_root=args.benchmark_stream_root,
        code_source_fingerprint=args.code_source_fingerprint,
        configuration_fingerprint=args.configuration_fingerprint,
        model_profile=args.model_profile,
        cutoff_chapter=args.cutoff_chapter,
        target_chapter=args.target_chapter,
        access_scope=args.access_scope,
        incidents=incidents,
        memory_write_validation_only=args.memory_write_validation_only,
        problem_identity=problem_identity,
        chapter_settlement_timeout_seconds=args.chapter_settlement_timeout_seconds,
        settlement_output_tokens=args.settlement_output_tokens,
        settlement_token_budget=args.settlement_token_budget,
        settlement_max_total_model_calls=args.settlement_max_total_model_calls,
    )
    _write_once(args.output, payload)
    print(json.dumps({"output": str(args.output.resolve()), "sha256": _sha256_file(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
