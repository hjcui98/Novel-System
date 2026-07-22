from __future__ import annotations

from pathlib import Path

import pytest

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.ids import ProjectId, StableId
from novel_agent.domain.stage2 import SourceClass, SourceDestination
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.bootstrap import (
    BootstrapIngestionError,
    BootstrapIngestionService,
    RawBootstrapSource,
)


def service(tmp_path: Path) -> tuple[BootstrapIngestionService, ArtifactRepository]:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    return BootstrapIngestionService(artifacts), artifacts


def raw(
    source_id: str,
    source_class: SourceClass,
    data: bytes = b"source text",
    media_type: str = "text/plain",
    chapter_index: int | None = None,
) -> RawBootstrapSource:
    return RawBootstrapSource(
        source_id=StableId(source_id),
        source_class=source_class,
        media_type=media_type,
        data=data,
        chapter_index=chapter_index,
    )


def test_ingestion_preserves_raw_bytes_classifies_taint_and_is_idempotent(tmp_path: Path) -> None:
    ingestion, artifacts = service(tmp_path)
    inputs = (
        raw("source.brief", SourceClass.AUTHOR_INITIAL_BRIEF, b"premise"),
        raw(
            "source.chapter.1",
            SourceClass.CHAPTER_TEXT,
            b"chapter one",
            chapter_index=1,
        ),
        raw(
            "source.private",
            SourceClass.FUTURE_TEXT_PRIVATE,
            b'{"future":true}',
            "application/json",
        ),
        raw(
            "source.style",
            SourceClass.STYLE_GUIDE,
            b"pov: third\ntense: past\n",
            "application/yaml",
        ),
    )

    bundle, ingested = ingestion.ingest(ProjectId("project.1"), StableId("bundle.1"), inputs)
    replayed, replayed_sources = ingestion.ingest(
        ProjectId("project.1"), StableId("bundle.1"), inputs
    )

    assert replayed.bundle_hash == bundle.bundle_hash
    assert replayed_sources == ingested
    assert artifacts.read_verified(ingested[0].source.artifact_ref) == b"premise"
    assert ingested[1].source.earliest_visible_chapter == 1
    assert ingested[1].classification.allowed_destinations == (
        SourceDestination.TEXT,
        SourceDestination.REFERENCE,
    )
    assert ingested[2].source.evaluator_only
    assert ingested[2].reference_candidate is None
    assert ingested[2].classification.allowed_destinations == (SourceDestination.EVALUATION,)
    assert ingested[2].parsed == {"future": True}
    assert ingested[3].parsed == {"pov": "third", "tense": "past"}


@pytest.mark.parametrize(
    ("source_class", "allowed"),
    [
        (SourceClass.AUTHOR_KNOWN_FUTURE_PLAN, SourceDestination.PLAN),
        (SourceClass.BASELINE_SETTING, SourceDestination.WORLD),
        (SourceClass.RETROSPECTIVE_SUMMARY, SourceDestination.EVALUATION),
        (SourceClass.READ_GOLD, SourceDestination.EVALUATION),
        (SourceClass.REPLAY_GOLD, SourceDestination.EVALUATION),
        (SourceClass.EXTERNAL_REFERENCE, SourceDestination.REFERENCE),
    ],
)
def test_every_source_class_uses_a_fixed_policy(
    tmp_path: Path, source_class: SourceClass, allowed: SourceDestination
) -> None:
    ingestion, _ = service(tmp_path)
    _, (result,) = ingestion.ingest(
        ProjectId("project.1"), StableId("bundle.1"), (raw("source.1", source_class),)
    )

    assert allowed in result.classification.allowed_destinations


def test_ingestion_rejects_ambiguous_or_unparseable_inputs(tmp_path: Path) -> None:
    ingestion, _ = service(tmp_path)
    duplicate = raw("source.same", SourceClass.BASELINE_SETTING)
    with pytest.raises(BootstrapIngestionError, match="unique"):
        ingestion.ingest(ProjectId("project.1"), StableId("bundle.1"), (duplicate, duplicate))

    invalid_inputs = (
        (raw("source.binary", SourceClass.BASELINE_SETTING, b"\xff"), "UTF-8"),
        (
            raw(
                "source.json",
                SourceClass.BASELINE_SETTING,
                b"{",
                "application/json",
            ),
            "payload is invalid",
        ),
        (
            raw(
                "source.yaml",
                SourceClass.BASELINE_SETTING,
                b"key: [",
                "application/yaml",
            ),
            "payload is invalid",
        ),
        (
            raw(
                "source.html",
                SourceClass.BASELINE_SETTING,
                b"<p>x</p>",
                "text/html",
            ),
            "unsupported source media type",
        ),
    )
    for source, message in invalid_inputs:
        with pytest.raises(BootstrapIngestionError, match=message):
            ingestion.ingest(ProjectId("project.1"), StableId("bundle.1"), (source,))
