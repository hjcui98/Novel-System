"""WriterWorkPlan, pinned Writing Skills, and structured Writer-turn execution."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from novel_agent.domain.agent_context import AgentContextView
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.generation import (
    WriterTurnOutput,
    WriterWorkPlan,
    WriterWorkPlanResult,
    WritingLoopRequest,
)
from novel_agent.domain.ids import SchemaVersion, StableId
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentType,
    ExecutionStatus,
    SkillContractRef,
    SkillExecutionReceipt,
)
from novel_agent.prompts.registry import content_hash
from novel_agent.services.agent_context import render_context
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.model_gateway import ModelGateway
from novel_agent.skills.registry import SkillRegistry

WRITER_COGNITION_SCHEMA_VERSION = SchemaVersion("1.0.0")
WRITER_WORK_PLAN_MEDIA_TYPE = "application/vnd.novel-agent.writer-work-plan+json"
WRITER_TURN_MEDIA_TYPE = "application/vnd.novel-agent.writer-turn+json"

_SKILL_FILES: dict[str, str] = {
    "skill.scene-composition": "scene_composition_v1.md",
    "skill.continuation": "continuation_v1.md",
    "skill.major-rewrite": "major_rewrite_v1.md",
    "skill.character-voice-writing": "character_voice_writing_v1.md",
    "skill.dialogue-subtext-writing": "dialogue_subtext_writing_v1.md",
    "skill.pov-epistemic-writing": "pov_epistemic_writing_v1.md",
    "skill.pacing-transition-writing": "pacing_transition_writing_v1.md",
    "skill.hook-foreshadowing-writing": "hook_foreshadowing_writing_v1.md",
    "skill.style-genre-writing": "style_genre_writing_v1.md",
}


class WriterCognitionError(ValueError):
    """Writer cognition violated a trusted plan, Skill, or Context boundary."""


@dataclass(frozen=True, slots=True)
class WriterTurnResult:
    output: WriterTurnOutput
    artifact: ArtifactRef
    raw_output_artifact: ArtifactRef
    model_call: ModelCallRecord


class WriterCognitionService:
    """Use the shared ModelGateway for prewriting and bounded Writer turns."""

    def __init__(
        self,
        gateway: ModelGateway,
        artifacts: ArtifactRepository,
        skills: SkillRegistry,
        *,
        schema_version: SchemaVersion = WRITER_COGNITION_SCHEMA_VERSION,
        package_root: Path | None = None,
        require_admission: bool = True,
    ) -> None:
        if require_admission and gateway.admission_controller is None:
            raise WriterCognitionError("Writer cognition requires endpoint-global admission")
        self._gateway = gateway
        self._artifacts = artifacts
        self._skills = skills
        self._schema_version = schema_version
        root = package_root or Path(__file__).parents[1]
        self._prompt_root = root / "prompts"

    @staticmethod
    def skill_contracts(package_root: Path | None = None) -> tuple[SkillContractRef, ...]:
        root = package_root or Path(__file__).parents[1]
        result = []
        for skill_id, filename in _SKILL_FILES.items():
            digest = content_hash((root / "skills" / filename).read_bytes())
            result.append(
                SkillContractRef(
                    contract_id=StableId(skill_id),
                    version=WRITER_COGNITION_SCHEMA_VERSION,
                    content_hash=digest,
                )
            )
        return tuple(result)

    async def create_work_plan(
        self,
        request: WritingLoopRequest,
        view: AgentContextView,
        model_request: ModelRequest,
    ) -> WriterWorkPlanResult:
        self._validate_view(request, view)
        allowed = set(request.allowed_skills)
        catalog = {
            item.contract_id: item
            for item in self.skill_contracts(Path(__file__).parents[1])
            if item.contract_id in allowed
        }
        if set(catalog) != allowed:
            missing = sorted(item.root for item in allowed - set(catalog))
            raise WriterCognitionError(f"unregistered Writer Skill allowlist: {missing}")
        skill_payload = []
        for skill_id in request.allowed_skills:
            contract = catalog[skill_id]
            text, resolved = self._skills.resolve(skill_id, contract.version)
            if resolved != contract:
                raise WriterCognitionError(f"Writer Skill hash mismatch: {skill_id.root}")
            skill_payload.append(f'<SKILL id="{skill_id.root}">\n{text}\n</SKILL>')
        prompt = self._read_prompt("writer_work_plan_v1.md")
        task_payload = canonical_json_bytes(
            {
                "writing_task": request.writing_task.model_dump(mode="json"),
                "writing_task_ref": request.writing_task_artifact.model_dump(mode="json"),
                "accepted_plan_ref": request.accepted_plan.artifact.model_dump(mode="json"),
                "writer_context_ref": request.writer_context_package_artifact.model_dump(
                    mode="json"
                ),
                "allowed_skill_ids": [item.root for item in request.allowed_skills],
                "context_hash": view.context_hash.root,
                # The work plan must be conditioned on the same bounded View as
                # the Writer turn, especially the previous chapter and typed gaps.
                "agent_context_view": render_context(view),
            }
        ).decode("utf-8")
        prepared = model_request.model_copy(
            update={
                "prompt": (
                    prompt
                    + "\n\n"
                    + "\n\n".join(skill_payload)
                    + "\n\n<TRUSTED_INPUT>\n"
                    + task_payload
                    + "\n</TRUSTED_INPUT>"
                ),
                "agent_id": StableId("agent.writer.work-plan"),
                "agent_mode": AgentMode.DRAFT.value,
                "skill_contract_hashes": tuple(item.content_hash for item in catalog.values()),
                "max_output_tokens": request.budgets.reserved_output_tokens or 4096,
                "scheduling_stage": "stage3.writer_work_plan",
            }
        )
        work_plan, call = await self._gateway.generate_structured(prepared, WriterWorkPlan)
        if (
            work_plan.writing_task_ref != request.writing_task_artifact
            or work_plan.accepted_plan_ref != request.accepted_plan.artifact
            or work_plan.writer_context_ref != request.writer_context_package_artifact
        ):
            raise WriterCognitionError("WriterWorkPlan lineage differs from loop inputs")
        selected = set(work_plan.selected_skill_ids)
        if not selected.issubset(allowed):
            raise WriterCognitionError("WriterWorkPlan selected a Skill outside the allowlist")
        work_plan_ref = self._artifacts.put(
            canonical_json_bytes(work_plan.model_dump(mode="json")),
            WRITER_WORK_PLAN_MEDIA_TYPE,
            self._schema_version,
        )
        receipts = tuple(
            SkillExecutionReceipt(
                receipt_id=StableId(
                    "skill-receipt."
                    + hashlib.sha256(
                        f"{request.run_id.root}\0{request.task_id.root}\0{skill_id.root}".encode()
                    ).hexdigest()
                ),
                run_id=request.run_id,
                task_id=request.task_id,
                skill=catalog[skill_id],
                agent_type=AgentType.WRITER,
                agent_mode=request.mode,
                base_commit=request.base_commit,
                context_manifest=view.context_hash,
                input_artifacts=(
                    request.writing_task_artifact,
                    request.accepted_plan.artifact,
                    request.writer_context_package_artifact,
                ),
                output_artifacts=(work_plan_ref,),
                completed_checkpoints=work_plan.expected_skill_checkpoints.get(
                    skill_id.root,
                    (),
                ),
                status=ExecutionStatus.SUCCEEDED,
                latency_ms=call.latency_ms,
            )
            for skill_id in work_plan.selected_skill_ids
        )
        return WriterWorkPlanResult(
            work_plan=work_plan,
            work_plan_artifact=work_plan_ref,
            skill_receipts=receipts,
            model_call_record=call,
        )

    async def take_turn(
        self,
        request: WritingLoopRequest,
        view: AgentContextView,
        plan: WriterWorkPlanResult,
        model_request: ModelRequest,
    ) -> WriterTurnResult:
        self._validate_view(request, view)
        selected_texts: list[str] = []
        contracts = {item.contract_id: item for item in self.skill_contracts()}
        for skill_id in plan.work_plan.selected_skill_ids:
            if skill_id not in request.allowed_skills:
                raise WriterCognitionError("selected Writer Skill is no longer allowed")
            contract = contracts[skill_id]
            text, actual = self._skills.resolve(skill_id, contract.version)
            if actual != contract:
                raise WriterCognitionError(f"Writer Skill hash mismatch: {skill_id.root}")
            selected_texts.append(f'<SKILL id="{skill_id.root}">\n{text}\n</SKILL>')
        prompt = (
            self._read_prompt("writer_turn_v1.md")
            + "\n\n"
            + "\n\n".join(selected_texts)
            + "\n\n<WRITER_WORK_PLAN>\n"
            + plan.work_plan.model_dump_json()
            + "\n</WRITER_WORK_PLAN>\n<AGENT_CONTEXT_VIEW>\n"
            + render_context(view)
            + "\n</AGENT_CONTEXT_VIEW>"
        )
        prepared = model_request.model_copy(
            update={
                "prompt": prompt,
                "agent_id": StableId("agent.writer.turn"),
                "agent_mode": request.mode.value,
                "skill_contract_hashes": tuple(
                    contracts[item].content_hash for item in plan.work_plan.selected_skill_ids
                ),
                "max_output_tokens": request.budgets.reserved_output_tokens or 4096,
                "scheduling_stage": "stage3.writer_turn",
            }
        )
        output, call = await self._gateway.generate_structured(prepared, WriterTurnOutput)
        if len(output.memory_requests) > request.budgets.max_memory_questions:
            raise WriterCognitionError("Writer exceeded the bounded memory-question budget")
        visible_ids = {
            item.item_id
            for item in (
                *view.protected_items,
                *view.active_memory_items,
                *view.working_items,
                *view.recent_settled_tail,
                *view.compacted_prefix_items,
            )
        }
        if any(
            not set(item.known_context_item_ids).issubset(visible_ids)
            for item in output.memory_requests
        ):
            raise WriterCognitionError("Writer memory request cites a non-visible Context item")
        artifact = self._artifacts.put(
            canonical_json_bytes(output.model_dump(mode="json")),
            WRITER_TURN_MEDIA_TYPE,
            self._schema_version,
        )
        raw = self._gateway.raw_responses.get(call.request_id.root)
        if raw is None:
            raise WriterCognitionError("Writer raw response is absent from ModelGateway audit")
        raw_output_artifact = self._artifacts.put(
            raw.encode("utf-8"),
            "application/vnd.novel-agent.writer-raw-response+json",
            self._schema_version,
        )
        return WriterTurnResult(
            output=output,
            artifact=artifact,
            raw_output_artifact=raw_output_artifact,
            model_call=call,
        )

    def _read_prompt(self, filename: str) -> str:
        return (self._prompt_root / filename).read_text(encoding="utf-8")

    @staticmethod
    def _validate_view(request: WritingLoopRequest, view: AgentContextView) -> None:
        if (
            view.run_id != request.run_id
            or view.task_id != request.task_id
            or view.base_commit != request.base_commit
            or view.snapshot_id != request.snapshot_id
            or view.plan_ref != request.accepted_plan.artifact
            or view.profile_ref != request.project_profile_artifact
            or view.information_scope != "writer_safe"
        ):
            raise WriterCognitionError("Writer Context View differs from the WritingLoop basis")
        receipt = view.provider_validity_receipt
        if (
            receipt is None
            or not receipt.provider_valid
            or receipt.context_hash != view.context_hash
        ):
            raise WriterCognitionError("Writer dispatch requires a current provider-valid View")


__all__ = [
    "WRITER_COGNITION_SCHEMA_VERSION",
    "WRITER_TURN_MEDIA_TYPE",
    "WRITER_WORK_PLAN_MEDIA_TYPE",
    "WriterCognitionError",
    "WriterCognitionService",
    "WriterTurnResult",
]
