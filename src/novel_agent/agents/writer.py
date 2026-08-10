"""Version-pinned Writer contracts and trusted prompt composition."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from novel_agent.agents.registry import (
    agent_spec_content_id,
    seal_agent_spec,
    tool_policy_content_id,
)
from novel_agent.agents.runner import AgentRunResult, PreparedAgentRun, StructuredAgentRunner
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.generation import (
    WriterContextItem,
    WriterContextSnapshot,
    WriterDraftPayload,
    WriterInvocation,
)
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentSpec,
    AgentType,
    ContractRef,
    PromptContractRef,
    SkillContractRef,
    ToolPermission,
    ToolPolicy,
)
from novel_agent.prompts.registry import (
    PromptRegistry,
    PromptTemplate,
    content_hash,
)
from novel_agent.services.content_addressing import canonical_json_bytes, content_id
from novel_agent.skills.registry import SkillRegistry, SkillTemplate

WRITER_CONTRACT_VERSION = SchemaVersion("1.0.0")
_ZERO_HASH = ArtifactId("sha256:" + "0" * 64)
WRITER_MODES = (
    AgentMode.DRAFT,
    AgentMode.CONTINUE,
    AgentMode.MAJOR_REWRITE,
)
WRITER_DENIED_TOOLS = (
    "memory.search_exact",
    "memory.search_temporal",
    "memory.search_bm25",
    "memory.search_vector",
    "memory.search_graph",
    "memory.write",
    "canonical.commit",
    "root.update",
)
_MODE_ASSETS: dict[AgentMode, tuple[str, str, str, str]] = {
    AgentMode.DRAFT: (
        "prompt.writer-draft",
        "writer_draft_v1.md",
        "skill.scene-composition",
        "scene_composition_v1.md",
    ),
    AgentMode.CONTINUE: (
        "prompt.writer-continue",
        "writer_continue_v1.md",
        "skill.continuation",
        "continuation_v1.md",
    ),
    AgentMode.MAJOR_REWRITE: (
        "prompt.writer-major-rewrite",
        "writer_major_rewrite_v1.md",
        "skill.major-rewrite",
        "major_rewrite_v1.md",
    ),
}


class WriterAgentError(ValueError):
    """A trusted Writer contract failed before model execution."""


@dataclass(frozen=True, slots=True)
class WriterContractBundle:
    """Sealed Writer specs and their content-pinned prompt/skill templates."""

    agent_specs: tuple[AgentSpec, ...]
    prompt_templates: tuple[PromptTemplate, ...]
    skill_templates: tuple[SkillTemplate, ...]

    @property
    def specs(self) -> tuple[AgentSpec, ...]:
        """Compatibility alias matching other Stage 2 harness bundles."""

        return self.agent_specs


def build_writer_contract_bundle(
    package_root: Path | None = None,
    *,
    modes: tuple[AgentMode, ...] = WRITER_MODES,
) -> WriterContractBundle:
    """Build sealed, version-pinned Writer contracts without a global registry."""

    if not modes:
        raise WriterAgentError("at least one Writer mode must be registered")
    if len(modes) != len(set(modes)):
        raise WriterAgentError("Writer modes must be unique")
    unsupported = tuple(mode for mode in modes if mode not in _MODE_ASSETS)
    if unsupported:
        raise WriterAgentError(f"unsupported Writer modes: {unsupported!r}")

    root = package_root or Path(__file__).parents[1]
    prompt_directory = root / "prompts"
    skill_directory = root / "skills"
    system_path = prompt_directory / "system_policy_v1.md"
    system_digest = content_hash(system_path.read_bytes())
    system_ref = PromptContractRef(
        contract_id=StableId("prompt.system-policy"),
        version=WRITER_CONTRACT_VERSION,
        content_hash=system_digest,
        render_fingerprint=system_digest,
    )
    prompt_templates: list[PromptTemplate] = [
        PromptTemplate(
            system_ref.contract_id,
            WRITER_CONTRACT_VERSION,
            system_path,
            system_digest,
        )
    ]
    skill_templates: list[SkillTemplate] = []
    specs: list[AgentSpec] = []
    input_schema = ContractRef(
        contract_id=StableId("schema.writer-invocation"),
        version=WRITER_CONTRACT_VERSION,
        content_hash=content_id(WriterInvocation.model_json_schema()),
    )
    output_schema = ContractRef(
        contract_id=StableId("schema.writer-draft-payload"),
        version=WRITER_CONTRACT_VERSION,
        content_hash=content_id(WriterDraftPayload.model_json_schema()),
    )

    for mode in modes:
        prompt_id, prompt_filename, skill_id, skill_filename = _MODE_ASSETS[mode]
        prompt_path = prompt_directory / prompt_filename
        prompt_digest = content_hash(prompt_path.read_bytes())
        prompt_ref = PromptContractRef(
            contract_id=StableId(prompt_id),
            version=WRITER_CONTRACT_VERSION,
            content_hash=prompt_digest,
            render_fingerprint=prompt_digest,
        )
        prompt_templates.append(
            PromptTemplate(
                prompt_ref.contract_id,
                WRITER_CONTRACT_VERSION,
                prompt_path,
                prompt_digest,
            )
        )
        skill_path = skill_directory / skill_filename
        skill_digest = content_hash(skill_path.read_bytes())
        skill_ref = SkillContractRef(
            contract_id=StableId(skill_id),
            version=WRITER_CONTRACT_VERSION,
            content_hash=skill_digest,
        )
        skill_templates.append(
            SkillTemplate(
                skill_ref.contract_id,
                WRITER_CONTRACT_VERSION,
                skill_path,
                skill_digest,
            )
        )
        policy = ToolPolicy(
            policy_id=StableId(f"policy.writer.{mode.value}.shadow"),
            version=WRITER_CONTRACT_VERSION,
            content_hash=_ZERO_HASH,
            allowed_tools=(),
            denied_tools=WRITER_DENIED_TOOLS,
            permission=ToolPermission.READ,
            max_tool_calls=0,
        )
        specs.append(
            seal_agent_spec(
                AgentSpec(
                    agent_id=StableId(f"agent.writer.{mode.value}"),
                    agent_type=AgentType.WRITER,
                    mode=mode,
                    version=WRITER_CONTRACT_VERSION,
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
    return WriterContractBundle(
        agent_specs=tuple(specs),
        prompt_templates=tuple(prompt_templates),
        skill_templates=tuple(skill_templates),
    )


class WriterAgent:
    """Prepare and execute Writer calls with a Writer-local trusted prompt tail."""

    def __init__(
        self,
        runner: StructuredAgentRunner,
        prompts: PromptRegistry,
        skills: SkillRegistry,
    ) -> None:
        self._runner = runner
        self._prompts = prompts
        self._skills = skills

    def prepare_contract(
        self,
        invocation: WriterInvocation,
        request: ModelRequest,
    ) -> PreparedAgentRun:
        """Resolve Writer runtime fingerprints without model or artifact access."""

        return self.prepare(invocation, request)

    def prepare(
        self,
        invocation: WriterInvocation,
        request: ModelRequest,
        *,
        source_payloads: Mapping[str, object] | None = None,
        prior_text: str | None = None,
    ) -> PreparedAgentRun:
        """Resolve pinned contracts, then replace generic rendering with Writer layering."""

        if request.run_id != invocation.run_id or request.task_id != invocation.task_id:
            raise WriterAgentError("model request identity does not match WriterInvocation")
        supplied = dict(source_payloads or {})
        if "context" in supplied or "prior_text" in supplied:
            raise WriterAgentError("Writer source payload labels use reserved names")
        source_data: dict[str, object] = {
            "context": _writer_safe_context_projection(invocation.context_package),
        }
        source_data.update(supplied)
        if prior_text is not None:
            source_data["prior_text"] = prior_text
        source_hashes = tuple(
            sorted(
                (artifact.artifact_id for artifact in invocation.input_artifacts),
                key=lambda item: item.root,
            )
        )
        generic = self._runner.prepare(
            AgentType.WRITER,
            invocation.mode,
            WRITER_CONTRACT_VERSION.root,
            request,
            _escaped_json(source_data),
            source_hashes=source_hashes,
            input_artifacts=invocation.input_artifacts,
            base_commit=invocation.basis.base_commit,
        )
        self._validate_spec(generic.spec)
        if invocation.basis.configuration_fingerprint != generic.configuration_fingerprint:
            raise WriterAgentError("Writer basis configuration fingerprint mismatch")
        rendered = self._render_writer_prompt(generic.spec, invocation, source_data)
        render_fingerprint = content_hash(rendered.encode("utf-8"))
        safe_request = generic.request.model_copy(
            update={
                "prompt": rendered,
                "render_fingerprint": render_fingerprint,
            }
        )
        return replace(
            generic,
            request=safe_request,
            rendered_prompt=rendered,
            prompt_fingerprint=render_fingerprint,
        )

    async def execute(
        self,
        prepared: PreparedAgentRun,
    ) -> AgentRunResult[WriterDraftPayload]:
        """Execute one prepared Writer call and attach unresolved output to its receipt."""

        self._validate_prepared(prepared)
        result = await self._runner.execute(prepared, WriterDraftPayload)
        receipt = self.receipt(
            prepared,
            result.model_call,
            unresolved=result.output.unresolved_questions,
        )
        return replace(result, receipt=receipt)

    def receipt(
        self,
        prepared: PreparedAgentRun,
        call: ModelCallRecord,
        *,
        output_artifacts: tuple[ArtifactRef, ...] = (),
        unresolved: tuple[str, ...] = (),
    ) -> AgentExecutionReceipt:
        """Finalize a Writer receipt after trusted artifact materialization."""

        self._validate_prepared(prepared)
        return self._runner.receipt(
            prepared,
            call,
            output_artifacts=output_artifacts,
            unresolved=unresolved,
        )

    def _render_writer_prompt(
        self,
        spec: AgentSpec,
        invocation: WriterInvocation,
        source_data: Mapping[str, object],
    ) -> str:
        system = self._prompts.read(
            spec.system_prompt.contract_id,
            spec.system_prompt.version,
        )
        mode_contract = self._prompts.read(
            spec.task_prompt.contract_id,
            spec.task_prompt.version,
        )
        skill_texts: list[str] = []
        for expected in spec.skills:
            text, actual = self._skills.resolve(expected.contract_id, expected.version)
            if actual.content_hash != expected.content_hash:
                raise WriterAgentError(
                    f"AgentSpec skill hash mismatch: {expected.contract_id.root}"
                )
            skill_texts.append(text)
        trusted_task = {
            "mode": invocation.mode.value,
            "writing_task": _json_safe(invocation.writing_task),
            "continuation_boundary": _json_safe(invocation.continuation_boundary),
            "rewrite_directive": _json_safe(invocation.rewrite_directive),
        }
        return "\n\n".join(
            (
                "<TRUSTED_WRITER_SYSTEM_POLICY>\n" + system + "\n</TRUSTED_WRITER_SYSTEM_POLICY>",
                "<TRUSTED_WRITER_MODE_CONTRACT>\n"
                + mode_contract
                + "\n</TRUSTED_WRITER_MODE_CONTRACT>",
                "<TRUSTED_WRITER_SKILL>\n" + "\n\n".join(skill_texts) + "\n</TRUSTED_WRITER_SKILL>",
                "<TRUSTED_WRITING_TASK_CONTRACT>\n"
                + _escaped_json(trusted_task)
                + "\n</TRUSTED_WRITING_TASK_CONTRACT>",
                '<WRITER_SOURCE_DATA trusted="false">\n'
                + _escaped_json(source_data)
                + "\n</WRITER_SOURCE_DATA>",
                "<TRUSTED_WRITER_OUTPUT_CONTRACT>\n"
                "Return exactly one JSON object matching WriterDraftPayload with only "
                "draft_text, declared_memory_hints, unresolved_questions, and "
                "self_observations. Do not emit trusted IDs, hashes, offsets, "
                "EvidenceRef, ObservedChangeSet, CandidateChangeBundle, approval, or "
                "Canon writes.\n"
                "</TRUSTED_WRITER_OUTPUT_CONTRACT>",
            )
        )

    @staticmethod
    def _validate_spec(spec: AgentSpec) -> None:
        if spec.agent_type is not AgentType.WRITER or spec.mode not in WRITER_MODES:
            raise WriterAgentError("resolved AgentSpec is not a supported Writer contract")
        if spec.tool_policy.content_hash != tool_policy_content_id(spec.tool_policy):
            raise WriterAgentError("Writer ToolPolicy content hash mismatch")
        if spec.content_hash != agent_spec_content_id(spec):
            raise WriterAgentError("Writer AgentSpec content hash mismatch")
        if spec.input_schema.content_hash != content_id(WriterInvocation.model_json_schema()):
            raise WriterAgentError("Writer input schema hash mismatch")
        if spec.output_schema.content_hash != content_id(WriterDraftPayload.model_json_schema()):
            raise WriterAgentError("Writer output schema hash mismatch")
        policy = spec.tool_policy
        if (
            policy.allowed_tools
            or policy.denied_tools != WRITER_DENIED_TOOLS
            or policy.max_tool_calls != 0
            or policy.permission is not ToolPermission.READ
        ):
            raise WriterAgentError("Writer ToolPolicy is not the sealed zero-tool policy")

    @classmethod
    def _validate_prepared(cls, prepared: PreparedAgentRun) -> None:
        cls._validate_spec(prepared.spec)
        fingerprint = content_hash(prepared.rendered_prompt.encode("utf-8"))
        if (
            prepared.prompt_fingerprint != fingerprint
            or prepared.request.prompt != prepared.rendered_prompt
            or prepared.request.render_fingerprint != fingerprint
            or prepared.request.agent_spec_hash != prepared.spec.content_hash
            or prepared.request.tool_policy_hash != prepared.spec.tool_policy.content_hash
        ):
            raise WriterAgentError("prepared Writer request fingerprint mismatch")


def _writer_safe_context_projection(context: WriterContextSnapshot) -> dict[str, object]:
    projection: dict[str, object] = {
        "task_contract": context.task_contract,
        "unresolved_gaps": context.unresolved_gaps,
        "budget_report": context.budget_report,
    }
    categories = tuple(sorted({item.category for item in context.items}))
    for category in categories:
        projection[category] = tuple(
            _writer_safe_item_projection(item)
            for item in context.items
            if item.category == category
        )
    return projection


def _writer_safe_item_projection(item: WriterContextItem) -> dict[str, object]:
    fields = (
        "item_id",
        "text",
        "entity_ids",
        "predicate",
        "narrative_start",
        "narrative_end",
        "story_time_start",
        "story_time_end",
        "truth_class",
        "support_status",
        "mandatory",
    )
    return {field: _json_safe(getattr(item, field)) for field in fields}


def _escaped_json(value: object) -> str:
    """Serialize deterministic source data while making delimiter spoofing inert."""

    payload = canonical_json_bytes(_json_safe(value)).decode("utf-8")
    return payload.replace("<", "\\u003c").replace(">", "\\u003e")


def _json_safe(value: object) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_safe(item) for item in value]
    return value
