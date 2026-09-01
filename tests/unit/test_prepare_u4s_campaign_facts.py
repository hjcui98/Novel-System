from scripts.prepare_u4s_campaign_facts import _copy_ref

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.artifacts import TextRootRef
from novel_agent.domain.ids import SchemaVersion
from novel_agent.services.artifacts import ArtifactRepository


def test_copy_ref_accepts_typed_root_ref_with_same_content_identity(tmp_path) -> None:
    source = ArtifactRepository(FilesystemObjectStore(tmp_path / "source"))
    destination = ArtifactRepository(FilesystemObjectStore(tmp_path / "destination"))
    schema_version = SchemaVersion("1.0.0")
    source_ref = source.put(b"canonical-root", "application/json", schema_version)
    typed_ref = TextRootRef.model_validate(source_ref.model_dump(mode="json"))

    _copy_ref(source, destination, typed_ref)

    assert destination.read_verified(typed_ref) == b"canonical-root"
