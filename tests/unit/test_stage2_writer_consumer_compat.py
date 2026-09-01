"""U1-B: Stage 2 completeness fields stay additive and Writer-visible."""

from __future__ import annotations

from novel_agent.domain.writer_context import WriterContextPackageV2


def test_missing_semantic_fields_do_not_default_to_complete() -> None:
    schema = WriterContextPackageV2.model_json_schema()
    properties = schema["properties"]
    assert properties["semantic_status"]["default"] == "UNASSESSED"
    assert properties["usable_with_gaps"]["default"] is True
    assert "READY" not in properties["semantic_status"].get("enum", ())
    required = set(schema.get("required", ()))
    assert "semantic_status" not in required
    assert "usable_with_gaps" not in required
