"""Deterministic Stage 2 bootstrap ingestion, parsing, and source classification."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import ClassVar

import yaml
from pydantic import JsonValue, TypeAdapter, ValidationError

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ProjectId, SchemaVersion, StableId
from novel_agent.domain.stage2 import (
    BootstrapSource,
    ProjectBootstrapBundle,
    SourceClass,
    SourceClassification,
    SourceDestination,
)
from novel_agent.services.artifacts import ArtifactRepository, sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes

JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
STAGE2_SCHEMA_VERSION = SchemaVersion("2.0.0")


class BootstrapIngestionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RawBootstrapSource:
    source_id: StableId
    source_class: SourceClass
    media_type: str
    data: bytes
    chapter_index: int | None = None


@dataclass(frozen=True, slots=True)
class IngestedBootstrapSource:
    source: BootstrapSource
    classification: SourceClassification
    parsed: JsonValue
    reference_candidate: ArtifactRef | None


class BootstrapIngestionService:
    """Persists unmodified source bytes before exposing parsed, taint-aware inputs."""

    _POLICY: ClassVar[
        dict[SourceClass, tuple[tuple[SourceDestination, ...], tuple[SourceDestination, ...]]]
    ] = {
        SourceClass.AUTHOR_INITIAL_BRIEF: (
            (
                SourceDestination.PLAN,
                SourceDestination.WORLD,
                SourceDestination.REFERENCE,
                SourceDestination.PROJECT_PROFILE,
            ),
            (SourceDestination.EVALUATION,),
        ),
        SourceClass.AUTHOR_KNOWN_FUTURE_PLAN: (
            (SourceDestination.PLAN, SourceDestination.REFERENCE),
            (SourceDestination.TEXT, SourceDestination.WORLD, SourceDestination.EVALUATION),
        ),
        SourceClass.BASELINE_SETTING: (
            (SourceDestination.WORLD, SourceDestination.REFERENCE),
            (SourceDestination.TEXT, SourceDestination.EVALUATION),
        ),
        SourceClass.CHAPTER_TEXT: (
            (SourceDestination.TEXT, SourceDestination.REFERENCE),
            (SourceDestination.PLAN, SourceDestination.EVALUATION),
        ),
        SourceClass.RETROSPECTIVE_SUMMARY: (
            (SourceDestination.EVALUATION,),
            (
                SourceDestination.TEXT,
                SourceDestination.PLAN,
                SourceDestination.WORLD,
                SourceDestination.REFERENCE,
                SourceDestination.PROJECT_PROFILE,
            ),
        ),
        SourceClass.FUTURE_TEXT_PRIVATE: (
            (SourceDestination.EVALUATION,),
            (
                SourceDestination.TEXT,
                SourceDestination.PLAN,
                SourceDestination.WORLD,
                SourceDestination.REFERENCE,
                SourceDestination.PROJECT_PROFILE,
            ),
        ),
        SourceClass.READ_GOLD: (
            (SourceDestination.EVALUATION,),
            (
                SourceDestination.TEXT,
                SourceDestination.PLAN,
                SourceDestination.WORLD,
                SourceDestination.REFERENCE,
                SourceDestination.PROJECT_PROFILE,
            ),
        ),
        SourceClass.REPLAY_GOLD: (
            (SourceDestination.EVALUATION,),
            (
                SourceDestination.TEXT,
                SourceDestination.PLAN,
                SourceDestination.WORLD,
                SourceDestination.REFERENCE,
                SourceDestination.PROJECT_PROFILE,
            ),
        ),
        SourceClass.STYLE_GUIDE: (
            (SourceDestination.PROJECT_PROFILE, SourceDestination.REFERENCE),
            (SourceDestination.TEXT, SourceDestination.WORLD, SourceDestination.EVALUATION),
        ),
        SourceClass.EXTERNAL_REFERENCE: (
            (SourceDestination.REFERENCE,),
            (SourceDestination.TEXT, SourceDestination.WORLD, SourceDestination.EVALUATION),
        ),
    }
    _EVALUATOR_ONLY: ClassVar[frozenset[SourceClass]] = frozenset(
        {
            SourceClass.RETROSPECTIVE_SUMMARY,
            SourceClass.FUTURE_TEXT_PRIVATE,
            SourceClass.READ_GOLD,
            SourceClass.REPLAY_GOLD,
        }
    )
    _TEXT_MEDIA_TYPES: ClassVar[frozenset[str]] = frozenset({"text/plain", "text/markdown"})
    _JSON_MEDIA_TYPES: ClassVar[frozenset[str]] = frozenset({"application/json"})
    _YAML_MEDIA_TYPES: ClassVar[frozenset[str]] = frozenset(
        {"application/yaml", "application/x-yaml", "text/yaml"}
    )

    def __init__(self, artifacts: ArtifactRepository) -> None:
        self._artifacts = artifacts

    def ingest(
        self,
        project_id: ProjectId,
        bundle_id: StableId,
        raw_sources: tuple[RawBootstrapSource, ...],
        schema_version: SchemaVersion = STAGE2_SCHEMA_VERSION,
    ) -> tuple[ProjectBootstrapBundle, tuple[IngestedBootstrapSource, ...]]:
        source_ids = tuple(raw.source_id for raw in raw_sources)
        if len(source_ids) != len(set(source_ids)):
            raise BootstrapIngestionError("raw bootstrap source ids must be unique")
        ingested = tuple(self._ingest_one(raw, schema_version) for raw in raw_sources)
        sources = tuple(item.source for item in ingested)
        manifest = {
            "bundle_id": bundle_id.root,
            "project_id": project_id.root,
            "schema_version": schema_version.root,
            "sources": [
                {
                    "source_id": source.source_id.root,
                    "source_class": source.source_class.value,
                    "content_hash": source.content_hash.root,
                    "earliest_visible_chapter": source.earliest_visible_chapter,
                    "evaluator_only": source.evaluator_only,
                }
                for source in sources
            ],
        }
        bundle = ProjectBootstrapBundle(
            bundle_id=bundle_id,
            project_id=project_id,
            schema_version=schema_version,
            sources=sources,
            bundle_hash=sha256_id(canonical_json_bytes(manifest)),
        )
        return bundle, ingested

    def _ingest_one(
        self, raw: RawBootstrapSource, schema_version: SchemaVersion
    ) -> IngestedBootstrapSource:
        parsed = self._parse(raw)
        artifact = self._artifacts.put(raw.data, raw.media_type, schema_version)
        evaluator_only = raw.source_class in self._EVALUATOR_ONLY
        earliest = raw.chapter_index if raw.source_class is SourceClass.CHAPTER_TEXT else 0
        source = BootstrapSource(
            source_id=raw.source_id,
            source_class=raw.source_class,
            media_type=raw.media_type,
            content_hash=artifact.artifact_id,
            byte_length=artifact.byte_length,
            artifact_ref=artifact,
            earliest_visible_chapter=earliest,
            chapter_index=raw.chapter_index,
            evaluator_only=evaluator_only,
        )
        allowed, forbidden = self._POLICY[raw.source_class]
        classification = SourceClassification(
            source_id=raw.source_id,
            source_class=raw.source_class,
            allowed_destinations=allowed,
            forbidden_destinations=forbidden,
            classification_reason=f"immutable policy for {raw.source_class.value}",
        )
        return IngestedBootstrapSource(
            source=source,
            classification=classification,
            parsed=parsed,
            reference_candidate=None if evaluator_only else artifact,
        )

    def _parse(self, raw: RawBootstrapSource) -> JsonValue:
        try:
            text = raw.data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise BootstrapIngestionError(
                f"source is not valid UTF-8: {raw.source_id.root}"
            ) from error
        try:
            if raw.media_type in self._TEXT_MEDIA_TYPES:
                value: object = text
            elif raw.media_type in self._JSON_MEDIA_TYPES:
                value = json.loads(text)
            elif raw.media_type in self._YAML_MEDIA_TYPES:
                value = yaml.safe_load(text)
            else:
                raise BootstrapIngestionError(f"unsupported source media type: {raw.media_type}")
            return JSON_VALUE_ADAPTER.validate_python(value, strict=True)
        except (json.JSONDecodeError, yaml.YAMLError, ValidationError) as error:
            raise BootstrapIngestionError(
                f"source payload is invalid for {raw.media_type}: {raw.source_id.root}"
            ) from error
