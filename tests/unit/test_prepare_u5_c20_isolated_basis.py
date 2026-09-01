from pathlib import Path

from scripts.prepare_u5_c20_isolated_basis import prepare_basis
from sqlalchemy import create_engine

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.domain.artifacts import (
    PlanRootRef,
    ProjectProfileRootRef,
    ReferenceRootRef,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, SchemaVersion, StableId
from novel_agent.domain.stage2 import ReferenceAsset, ReferenceRootDocument, SourceClass
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.content_addressing import canonical_json_bytes
from tests.factories import make_manifest
from tests.fixtures.stage1_synthetic import make_synthetic_bundle


def _database(path: Path) -> str:
    url = f"sqlite+pysqlite:///{path}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return url


def test_prepare_basis_copies_typed_roots_without_rewinding_source(tmp_path: Path) -> None:
    source_url = _database(tmp_path / "source.db")
    destination_url = _database(tmp_path / "destination.db")
    source_project = ProjectId("project.u5.source")
    destination_project = ProjectId("project.u5.isolated")
    source_root = tmp_path / "source-objects"
    destination_root = tmp_path / "destination-objects"
    source_artifacts = ArtifactRepository(FilesystemObjectStore(source_root))
    bundle = make_synthetic_bundle()
    text = next(root for root in bundle.text_roots if len(root.chapters) == 20)
    world = bundle.world_roots[0]
    plan = bundle.plan_roots[0]
    version = SchemaVersion("1.0.0")

    def put(value: object, media_type: str):
        return source_artifacts.put(
            canonical_json_bytes(value.model_dump(mode="json")), media_type, version
        )

    text_ref = put(text, "application/json")
    plan_ref = put(plan, "application/json")
    world_ref = put(world, "application/json")
    author_ref = source_artifacts.put(b"author intent", "text/plain", version)
    reference = ReferenceRootDocument(
        root_hash=ArtifactId("sha256:" + "3" * 64),
        schema_version=version,
        assets=(
            ReferenceAsset(
                asset_id=StableId("reference.author"),
                source_id=StableId("source.author"),
                source_class=SourceClass.AUTHOR_INITIAL_BRIEF,
                artifact=author_ref,
            ),
        ),
    )
    reference_ref = source_artifacts.put(
        canonical_json_bytes(reference.model_dump(mode="json")), "application/json", version
    )
    profile_ref = source_artifacts.put(b"{}", "application/json", version)
    manifest = make_manifest(source_project).model_copy(
        update={
            "text_root": TextRootRef(**text_ref.model_dump(mode="python")),
            "plan_root": PlanRootRef(**plan_ref.model_dump(mode="python")),
            "world_root": WorldRootRef(**world_ref.model_dump(mode="python")),
            "reference_root": ReferenceRootRef(**reference_ref.model_dump(mode="python")),
            "project_profile_root": ProjectProfileRootRef(**profile_ref.model_dump(mode="python")),
        }
    )
    source_commits = CommitService(build_session_factory(create_engine(source_url)))
    source_commit = source_commits.initialize_project(manifest)

    receipt = prepare_basis(
        source_database_url=source_url,
        source_project_id=source_project,
        source_commit=source_commit,
        source_object_root=source_root,
        destination_database_url=destination_url,
        destination_project_id=destination_project,
        destination_object_root=destination_root,
    )

    assert receipt["schema"] == "u5-c20-isolated-basis.v1"
    assert receipt["history_last_chapter"] == 20
    assert receipt["source_is_current"] is True
    assert receipt["source_commit"] == source_commit.root
    assert receipt["isolated_project_id"] == destination_project.root
    assert receipt["isolated_basis_commit"] != source_commit.root
    assert receipt["isolated_database_descriptor"] == destination_url
    assert "@" not in str(receipt["isolated_database_descriptor"])
    assert receipt["canonical_root_artifacts"]["text_root"] == text_ref.artifact_id.root
    destination_commits = CommitService(build_session_factory(create_engine(destination_url)))
    assert destination_commits.current_commit(destination_project) == CommitId(
        str(receipt["isolated_basis_commit"])
    )
    destination_artifacts = ArtifactRepository(FilesystemObjectStore(destination_root))
    assert destination_artifacts.read_verified(author_ref) == b"author intent"
