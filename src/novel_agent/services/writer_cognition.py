"""WriterWorkPlan, pinned Writing Skills, and structured Writer-turn execution."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from novel_agent.domain.agent_context import AgentContextView, ContextItemKind
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.generation import (
    WriterTurnAction,
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

_INTERNAL_DRAFT_MARKERS = (
    "evidence.curator.",
    "契约非交易",
)
_KNOWN_NARRATIVE_MARKER_REPLACEMENTS = {
    "契约非交易": "这份婚约不是可以拿来交换的筹码",
}
_INTERNAL_CHAPTER_LABEL = re.compile(r"(?<![A-Za-z0-9_])ch\d+(?![A-Za-z0-9_])", re.IGNORECASE)
_META_RELATION_MARKER = re.compile(r"婚约线.{0,20}冲突载体")
_RECENT_PROSE_MIN_LENGTH = 384
_RECENT_PROSE_MIN_MATCH_CHARS = 128
_RECENT_PROSE_SIMILARITY_THRESHOLD = 0.85
_RECENT_PROSE_MIN_OVERLAP_RATIO = 0.10
_RECENT_PROSE_LONG_MATCH_CHARS = 256
_RECENT_PROSE_LONG_MATCH_RATIO = 0.25
_COMPACT_RECENT_PROSE_MIN_MATCH_CHARS = 80
_COMPACT_RECENT_PROSE_MIN_OVERLAP_RATIO = 0.10
_SURFACE_RETRY_REPETITION_PENALTY = 1.10


def _repeats_recent_prose(
    prose: str,
    draft_text: str,
    *,
    compact_trail: bool = False,
) -> bool:
    """Detect a demonstrated near-copy while leaving ordinary continuity alone."""

    recent = prose.strip()
    draft = draft_text.strip()
    if not recent or not draft:
        return False
    if recent == draft:
        return True
    minimum_length = min(len(recent), len(draft))
    matcher = SequenceMatcher(None, recent, draft, autojunk=False)
    longest = matcher.find_longest_match(0, len(recent), 0, len(draft))
    if compact_trail:
        return (
            longest.size >= _COMPACT_RECENT_PROSE_MIN_MATCH_CHARS
            and longest.size / len(recent) >= _COMPACT_RECENT_PROSE_MIN_OVERLAP_RATIO
        )
    if minimum_length < _RECENT_PROSE_MIN_LENGTH:
        return False
    overall_near_copy = (
        matcher.ratio() >= _RECENT_PROSE_SIMILARITY_THRESHOLD
        and longest.size >= _RECENT_PROSE_MIN_MATCH_CHARS
        and longest.size / minimum_length >= _RECENT_PROSE_MIN_OVERLAP_RATIO
    )
    long_contiguous_copy = (
        longest.size >= _RECENT_PROSE_LONG_MATCH_CHARS
        and longest.size / len(recent) >= _RECENT_PROSE_LONG_MATCH_RATIO
    )
    return overall_near_copy or long_contiguous_copy


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


def _writer_draft_surface_error(draft_text: str, view: AgentContextView) -> str | None:
    """Reject only demonstrated model surface failures before editorial review."""

    for marker in _INTERNAL_DRAFT_MARKERS:
        if marker in draft_text:
            return f"Writer draft contains internal planning marker: {marker}"
    if _INTERNAL_CHAPTER_LABEL.search(draft_text) is not None:
        return "Writer draft contains an internal chapter label"
    if _META_RELATION_MARKER.search(draft_text) is not None:
        return "Writer draft contains a planning relation marker"

    for item in (
        *view.protected_items,
        *view.active_memory_items,
        *view.working_items,
        *view.recent_settled_tail,
        *view.compacted_prefix_items,
    ):
        if item.kind is not ContextItemKind.RECENT_PROSE:
            continue
        _header, separator, prose = item.content.partition("\n")
        compact_trail = _header.startswith("[近期章尾:")
        if (
            separator
            and (_header.startswith("[上一章完整正文:") or _header.startswith("[近期章尾:"))
            and _repeats_recent_prose(prose, draft_text, compact_trail=compact_trail)
        ):
            return "Writer draft repeats visible recent prose"
    return None


def _rewrite_known_narrative_markers(draft_text: str) -> str:
    """Turn one demonstrated planning alias into natural narrative wording."""

    for marker, replacement in _KNOWN_NARRATIVE_MARKER_REPLACEMENTS.items():
        draft_text = draft_text.replace(marker, replacement)
    return draft_text


def _same_artifact(left: ArtifactRef | None, right: ArtifactRef | None) -> bool:
    if left is None or right is None:
        return left is right
    return (
        left.artifact_id == right.artifact_id
        and left.media_type == right.media_type
        and left.byte_length == right.byte_length
        and left.schema_version == right.schema_version
    )


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
            card = self._skills.describe(skill_id, contract.version)
            skill_payload.append(f'<SKILL_CARD id="{skill_id.root}">\n{card}\n</SKILL_CARD>')
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
        opaque_lineage = canonical_json_bytes(
            {
                "writing_task_ref": request.writing_task_artifact.model_dump(mode="json"),
                "accepted_plan_ref": request.accepted_plan.artifact.model_dump(mode="json"),
                "writer_context_ref": request.writer_context_package_artifact.model_dump(
                    mode="json"
                ),
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
                    + "\n<OPAQUE_LINEAGE_BINDING>\n"
                    + opaque_lineage
                    + "\n</OPAQUE_LINEAGE_BINDING>\n</TRUSTED_INPUT>"
                ),
                "agent_id": StableId("agent.writer.work-plan"),
                "agent_mode": AgentMode.DRAFT.value,
                "skill_contract_hashes": tuple(item.content_hash for item in catalog.values()),
                "max_output_tokens": (
                    request.budgets.reserved_output_tokens
                    if request.budgets.reserved_output_tokens >= 1
                    else None
                ),
                "scheduling_stage": "stage3.writer_work_plan",
            }
        )
        work_plan, call = await self._gateway.generate_structured(prepared, WriterWorkPlan)
        # These three fields are model-visible echoes, not model-owned lineage.  The raw response
        # remains in the call ledger for audit, while the typed WorkPlan is always bound to the
        # request that the host already validated.  This prevents a long content-addressed id
        # copied incorrectly by the model from changing the trusted Writer basis.
        work_plan = work_plan.model_copy(
            update={
                "writing_task_ref": request.writing_task_artifact,
                "accepted_plan_ref": request.accepted_plan.artifact,
                "writer_context_ref": request.writer_context_package_artifact,
            }
        )
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
                planned_checkpoints=work_plan.expected_skill_checkpoints.get(
                    skill_id.root,
                    (),
                ),
                selected_checkpoints=work_plan.expected_skill_checkpoints.get(
                    skill_id.root,
                    (),
                ),
                status=ExecutionStatus.PLANNED,
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
        *,
        major_rewrite_attempt: int = 1,
    ) -> WriterTurnResult:
        if major_rewrite_attempt < 1:
            raise WriterCognitionError("major rewrite attempt must be positive")
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
        if request.mode is AgentMode.MAJOR_REWRITE:
            directives = tuple(
                item.content
                for item in view.working_items
                if item.kind is ContextItemKind.EDITOR_INSTRUCTION
            )
            if len(directives) != 1:
                raise WriterCognitionError(
                    "MAJOR_REWRITE requires exactly one Editor instruction in Context"
                )
            mode_prompt = self._read_prompt("writer_major_rewrite_v1.md")
            directive_prompt = (
                "\n\n<TRUSTED_EDITOR_REWRITE_DIRECTIVE>\n"
                + directives[0]
                + "\n</TRUSTED_EDITOR_REWRITE_DIRECTIVE>"
            )
            if major_rewrite_attempt > 1:
                directive_prompt += (
                    "\n\n<TRUSTED_MAJOR_REWRITE_RETRY>\n"
                    f"This is independent major rewrite attempt {major_rewrite_attempt}. "
                    "The previous rewrite still failed the Editor review. Discard both "
                    "earlier candidates and write a complete replacement scene. Execute "
                    "every required scene beat and advance the causal state; do not repeat, "
                    "paraphrase, summarize, or lightly edit any earlier candidate or the "
                    "visible prior-chapter ending. Resolve every blocking directive before "
                    "returning DRAFT_READY.\n"
                    "</TRUSTED_MAJOR_REWRITE_RETRY>"
                )
        else:
            mode_prompt = self._read_prompt("writer_turn_v1.md")
            directive_prompt = ""
        prompt = (
            mode_prompt
            + directive_prompt
            + "\n\n"
            + "\n\n".join(selected_texts)
            + "\n\n<TRUSTED_WRITING_LENGTH_POLICY>\n"
            + (
                "For DRAFT_READY, draft_text must contain between "
                f"{request.writing_task.length_policy.minimum_characters} and "
                f"{request.writing_task.length_policy.maximum_characters} characters "
                f"inclusive; aim for {request.writing_task.length_policy.target_characters} "
                "characters and stop before the maximum.\n"
            )
            + request.writing_task.length_policy.model_dump_json()
            + "\n</TRUSTED_WRITING_LENGTH_POLICY>"
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
                "max_output_tokens": (
                    request.budgets.reserved_output_tokens
                    if request.budgets.reserved_output_tokens >= 1
                    else None
                ),
                "scheduling_stage": "stage3.writer_turn",
            }
        )
        output, call = await self._gateway.generate_structured(prepared, WriterTurnOutput)
        if output.action is WriterTurnAction.DRAFT_READY and output.draft_text is not None:
            draft_text = output.draft_text
            rewritten_text = _rewrite_known_narrative_markers(draft_text)
            if rewritten_text != draft_text:
                output = output.model_copy(update={"draft_text": rewritten_text})
                draft_text = rewritten_text
            surface_error = _writer_draft_surface_error(draft_text, view)
            if surface_error == "Writer draft repeats visible recent prose":
                retry_digest = hashlib.sha256(
                    f"{prepared.request_id.root}:surface-retry".encode()
                ).hexdigest()[:48]
                retry_request = prepared.model_copy(
                    update={
                        "request_id": StableId(
                            f"request.stage3.writer-surface-retry.{retry_digest}"
                        ),
                        "trace_id": f"{prepared.trace_id}:surface-retry",
                        "repetition_penalty": _SURFACE_RETRY_REPETITION_PENALTY,
                        "prompt": (
                            prepared.prompt + "\n\n<WRITER_SURFACE_RETRY>\n"
                            "The previous draft was rejected because it copied the visible "
                            "recent prose. Discard that candidate. Write a distinct "
                            "target-chapter narrative that starts from the prior final state, "
                            "advances the accepted plan, and does not reproduce any complete "
                            "paragraph or any contiguous phrase longer than 64 characters "
                            "from visible recent prose. Change the opening action and scene "
                            "progression rather than paraphrasing the prior chapter.\n"
                            "</WRITER_SURFACE_RETRY>"
                        ),
                    }
                )
                output, call = await self._gateway.generate_structured(
                    retry_request,
                    WriterTurnOutput,
                )
                if output.action is WriterTurnAction.DRAFT_READY and output.draft_text is not None:
                    draft_text = output.draft_text
                    rewritten_text = _rewrite_known_narrative_markers(draft_text)
                    if rewritten_text != draft_text:
                        output = output.model_copy(update={"draft_text": rewritten_text})
                        draft_text = rewritten_text
                    surface_error = _writer_draft_surface_error(draft_text, view)
            if surface_error is not None:
                raise WriterCognitionError(surface_error)
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
        normalized_requests = tuple(
            item.model_copy(
                update={
                    "known_context_item_ids": tuple(
                        context_id
                        for context_id in item.known_context_item_ids
                        if context_id in visible_ids
                    )
                }
            )
            for item in output.memory_requests
        )
        if normalized_requests != output.memory_requests:
            # A stale model-side anchor is advisory metadata, not a permission to read hidden
            # Context. Keep only exact visible ids and let the bounded Memory query proceed.
            output = output.model_copy(update={"memory_requests": normalized_requests})
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
            or not _same_artifact(view.plan_ref, request.accepted_plan.artifact)
            or not _same_artifact(view.profile_ref, request.project_profile_artifact)
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
