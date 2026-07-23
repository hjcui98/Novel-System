"""Content-addressed Stage 2 configuration inventory tests."""

from __future__ import annotations

import pytest

from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.services.stage2_configuration import Stage2ConfigurationBuilder
from tests.contract.test_stage2_contract import agent_spec
from tests.factories import make_artifact


def test_configuration_builder_sorts_deduplicates_and_hashes_all_inventory() -> None:
    spec = agent_spec()
    other = spec.model_copy(
        update={
            "agent_id": StableId("agent.other"),
            "content_hash": ArtifactId("sha256:" + "c" * 64),
        }
    )
    prompt = spec.system_prompt
    skill = spec.skills[0]
    policy = spec.tool_policy
    schema = make_artifact("d")

    result = Stage2ConfigurationBuilder().build(
        manifest_id=StableId("configuration.stage2"),
        schema_version=SchemaVersion("2.0.0"),
        agent_specs=(other, spec, spec),
        prompt_contracts=(prompt, prompt),
        skill_contracts=(skill, skill),
        tool_policies=(policy, policy),
        schema_artifacts=(schema, schema),
    )

    assert result.agent_specs == (spec, other)
    assert result.prompt_contracts == (prompt,)
    assert result.skill_contracts == (skill,)
    assert result.tool_policies == (policy,)
    assert result.schema_artifacts == (schema,)
    assert result.configuration_fingerprint.root.startswith("sha256:")


@pytest.mark.parametrize(
    ("field", "values"),
    (
        (
            "agent_specs",
            lambda spec: (
                spec,
                spec.model_copy(update={"content_hash": ArtifactId("sha256:" + "9" * 64)}),
            ),
        ),
        (
            "schema_artifacts",
            lambda spec: (
                make_artifact("8"),
                make_artifact("8").model_copy(update={"media_type": "text/plain"}),
            ),
        ),
    ),
)
def test_configuration_identity_collision_is_rejected(field: str, values: object) -> None:
    spec = agent_spec()
    kwargs = {
        "manifest_id": StableId("configuration.collision"),
        "schema_version": SchemaVersion("2.0.0"),
        "agent_specs": (),
        "prompt_contracts": (),
        "skill_contracts": (),
        "tool_policies": (),
        "schema_artifacts": (),
        field: values(spec),  # type: ignore[operator]
    }
    with pytest.raises(ValueError, match="conflicting content"):
        Stage2ConfigurationBuilder().build(**kwargs)
