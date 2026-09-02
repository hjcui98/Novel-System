"""Version-pinned independent Editor REVIEW and LOCAL_REPAIR agent facade."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

from novel_agent.agents.registry import seal_agent_spec
from novel_agent.agents.runner import AgentRunResult, PreparedAgentRun, StructuredAgentRunner
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.editorial import (
    EditorialReviewInput,
    EditorRepairPayload,
    EditorReviewPayload,
)
from novel_agent.domain.ids import ArtifactId, CommitId, SchemaVersion, StableId
from novel_agent.domain.model_calls import ModelRequest
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentSpec,
    AgentType,
    ContractRef,
    PromptContractRef,
    SkillContractRef,
    ToolPermission,
    ToolPolicy,
)
from novel_agent.prompts.registry import PromptTemplate, content_hash
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.skills.registry import SkillTemplate

EDITOR_CONTRACT_VERSION = SchemaVersion("1.0.0")
EDITOR_MODES = (AgentMode.REVIEW, AgentMode.LOCAL_REPAIR)
_ZERO_HASH = ArtifactId("sha256:" + "0" * 64)
EDITOR_DENIED_TOOLS = (
    "memory.search_exact",
    "memory.search_temporal",
    "memory.search_bm25",
    "memory.search_vector",
    "memory.search_graph",
    "memory.write",
    "canonical.commit",
    "root.update",
)
_EDITOR_LENS_ASSETS: tuple[tuple[str, str], ...] = (
    ("skill.editor.chapter-length", "editor_chapter_length_v1.md"),
    ("skill.editor.plan-adherence-hook-payoff", "editor_plan_adherence_hook_payoff_v1.md"),
    ("skill.editor.pacing-repetition", "editor_pacing_repetition_v1.md"),
)
_MODE_ASSETS: dict[AgentMode, tuple[str, str, str, str]] = {
    AgentMode.REVIEW: (
        "prompt.editor-review",
        "editor_review_v1.md",
        "skill.editor-review",
        "editor_review_v1.md",
    ),
    AgentMode.LOCAL_REPAIR: (
        "prompt.editor-local-repair",
        "editor_local_repair_v1.md",
        "skill.editor-local-repair",
        "editor_local_repair_v1.md",
    ),
}


class EditorAgentError(ValueError):
    """A trusted Editor contract failed before or during model execution."""


@dataclass(frozen=True, slots=True)
class EditorContractBundle:
    agent_specs: tuple[AgentSpec, ...]
    prompt_templates: tuple[PromptTemplate, ...]
    skill_templates: tuple[SkillTemplate, ...]

    @property
    def specs(self) -> tuple[AgentSpec, ...]:
        return self.agent_specs


def build_editor_contract_bundle(
    package_root: Path | None = None,
    *,
    modes: tuple[AgentMode, ...] = EDITOR_MODES,
) -> EditorContractBundle:
    """Build the independent Editor contracts without mutating a global registry."""

    if not modes:
        raise EditorAgentError("at least one Editor mode must be registered")
    if len(modes) != len(set(modes)):
        raise EditorAgentError("Editor modes must be unique")
    unsupported = tuple(mode for mode in modes if mode not in _MODE_ASSETS)
    if unsupported:
        raise EditorAgentError(f"unsupported Editor modes: {unsupported!r}")

    root = package_root or Path(__file__).parents[1]
    prompt_directory = root / "prompts"
    skill_directory = root / "skills"
    system_path = prompt_directory / "system_policy_v1.md"
    system_digest = content_hash(system_path.read_bytes())
    system_ref = PromptContractRef(
        contract_id=StableId("prompt.system-policy"),
        version=EDITOR_CONTRACT_VERSION,
        content_hash=system_digest,
        render_fingerprint=system_digest,
    )
    prompt_templates: list[PromptTemplate] = [
        PromptTemplate(
            system_ref.contract_id,
            EDITOR_CONTRACT_VERSION,
            system_path,
            system_digest,
        )
    ]
    skill_templates: list[SkillTemplate] = []
    specs: list[AgentSpec] = []
    input_schema = ContractRef(
        contract_id=StableId("schema.editor-review-input"),
        version=EDITOR_CONTRACT_VERSION,
        content_hash=content_id(EditorialReviewInput.model_json_schema()),
    )

    for mode in modes:
        prompt_id, prompt_filename, skill_id, skill_filename = _MODE_ASSETS[mode]
        prompt_path = prompt_directory / prompt_filename
        prompt_digest = content_hash(prompt_path.read_bytes())
        prompt_ref = PromptContractRef(
            contract_id=StableId(prompt_id),
            version=EDITOR_CONTRACT_VERSION,
            content_hash=prompt_digest,
            render_fingerprint=prompt_digest,
        )
        prompt_templates.append(
            PromptTemplate(
                prompt_ref.contract_id,
                EDITOR_CONTRACT_VERSION,
                prompt_path,
                prompt_digest,
            )
        )
        skill_path = skill_directory / skill_filename
        skill_digest = content_hash(skill_path.read_bytes())
        skill_ref = SkillContractRef(
            contract_id=StableId(skill_id),
            version=EDITOR_CONTRACT_VERSION,
            content_hash=skill_digest,
        )
        skill_templates.append(
            SkillTemplate(
                skill_ref.contract_id,
                EDITOR_CONTRACT_VERSION,
                skill_path,
                skill_digest,
            )
        )
        output_type = EditorReviewPayload if mode is AgentMode.REVIEW else EditorRepairPayload
        output_schema = ContractRef(
            contract_id=StableId(
                "schema.editor-review-payload"
                if mode is AgentMode.REVIEW
                else "schema.editor-repair-payload"
            ),
            version=EDITOR_CONTRACT_VERSION,
            content_hash=content_id(output_type.model_json_schema()),
        )
        policy = ToolPolicy(
            policy_id=StableId(f"policy.editor.{mode.value}.candidate"),
            version=EDITOR_CONTRACT_VERSION,
            content_hash=_ZERO_HASH,
            allowed_tools=(),
            denied_tools=EDITOR_DENIED_TOOLS,
            permission=ToolPermission.READ,
            max_tool_calls=0,
        )
        specs.append(
            seal_agent_spec(
                AgentSpec(
                    agent_id=StableId(f"agent.editor.{mode.value}"),
                    agent_type=AgentType.EDITOR,
                    mode=mode,
                    version=EDITOR_CONTRACT_VERSION,
                    content_hash=_ZERO_HASH,
                    input_schema=input_schema,
                    output_schema=output_schema,
                    system_prompt=system_ref,
                    task_prompt=prompt_ref,
                    skills=(skill_ref,),
                    tool_policy=policy,
                )
            )
        )
    for lens_id, lens_filename in _EDITOR_LENS_ASSETS:
        lens_path = skill_directory / lens_filename
        lens_digest = content_hash(lens_path.read_bytes())
        skill_templates.append(
            SkillTemplate(
                StableId(lens_id),
                EDITOR_CONTRACT_VERSION,
                lens_path,
                lens_digest,
                summary=lens_id.removeprefix("skill.editor."),
                tags=("editor", "lens"),
                applicable_modes=(AgentMode.REVIEW.value,),
            )
        )
    return EditorContractBundle(
        agent_specs=tuple(specs),
        prompt_templates=tuple(prompt_templates),
        skill_templates=tuple(skill_templates),
    )


class EditorAgent:
    """Run structured Editor calls with no retrieval or write-capable tools."""

    def __init__(
        self,
        runner: StructuredAgentRunner,
    ) -> None:
        self._runner = runner

    def prepare(
        self,
        mode: AgentMode,
        request: ModelRequest,
        payload: Mapping[str, object],
        *,
        source_hashes: tuple[ArtifactId, ...] = (),
        input_artifacts: tuple[ArtifactRef, ...] = (),
        base_commit: CommitId | None = None,
    ) -> PreparedAgentRun:
        if mode not in EDITOR_MODES:
            raise EditorAgentError(f"unsupported Editor mode: {mode.value}")
        # The runner owns the AgentSpec, Prompt, Skill, and tool-policy fingerprints.
        # Payload is deliberately passed as a bounded untrusted JSON layer.
        return self._runner.prepare(
            AgentType.EDITOR,
            mode,
            EDITOR_CONTRACT_VERSION.root,
            request,
            _escaped_json(payload),
            source_hashes=source_hashes,
            input_artifacts=input_artifacts,
            base_commit=base_commit,
        )

    async def review(
        self,
        request: ModelRequest,
        payload: Mapping[str, object],
        *,
        source_hashes: tuple[ArtifactId, ...] = (),
        input_artifacts: tuple[ArtifactRef, ...] = (),
        base_commit: CommitId | None = None,
    ) -> AgentRunResult[EditorReviewPayload]:
        prepared = self.prepare(
            AgentMode.REVIEW,
            request,
            payload,
            source_hashes=source_hashes,
            input_artifacts=input_artifacts,
            base_commit=base_commit,
        )
        return await self._runner.execute(prepared, EditorReviewPayload)

    async def local_repair(
        self,
        request: ModelRequest,
        payload: Mapping[str, object],
        *,
        source_hashes: tuple[ArtifactId, ...] = (),
        input_artifacts: tuple[ArtifactRef, ...] = (),
        base_commit: CommitId | None = None,
    ) -> AgentRunResult[EditorRepairPayload]:
        prepared = self.prepare(
            AgentMode.LOCAL_REPAIR,
            request,
            payload,
            source_hashes=source_hashes,
            input_artifacts=input_artifacts,
            base_commit=base_commit,
        )
        return await self._runner.execute(prepared, EditorRepairPayload)


def _escaped_json(value: Mapping[str, object]) -> str:
    payload = canonical_json_bytes(_json_safe(value)).decode("utf-8")
    return payload.replace("<", "\\u003c").replace(">", "\\u003e")


def _json_safe(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_safe(item) for item in value]
    return value


__all__ = [
    "EDITOR_CONTRACT_VERSION",
    "EDITOR_DENIED_TOOLS",
    "EDITOR_MODES",
    "EditorAgent",
    "EditorAgentError",
    "EditorContractBundle",
    "build_editor_contract_bundle",
]
