"""Offline U8-C reasoner that can only select an existing safe action."""

from __future__ import annotations

import json

from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId, bounded_stable_id
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.recovery_reasoning import (
    RECOVERY_PROPOSAL_MEDIA_TYPE,
    RecoveryActionCandidate,
    RecoveryProposal,
    RecoveryProposalDraft,
    RecoveryReasonerRequest,
    RecoveryReasonerResult,
)
from novel_agent.prompts.registry import PromptRegistry
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.model_gateway import ModelGateway
from novel_agent.skills.registry import SkillRegistry


class RecoveryReasonerRejected(ValueError):
    """The model output did not select exactly one prevalidated action."""


class RecoveryReasonerContextExceeded(ValueError):
    """The immutable incident surface is too large for its frozen budget."""


_DEFAULT_SCHEMA_VERSION = SchemaVersion("1.0.0")


class RecoveryReasonerService:
    """Produce one durable proposal without executing or promoting it.

    The current caller is the offline U8 evolution campaign executor.  Runtime
    production assembly intentionally does not construct this service.
    """

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        artifacts: ArtifactRepository,
        prompts: PromptRegistry,
        skills: SkillRegistry,
        prompt_id: StableId,
        prompt_version: SchemaVersion,
        skill_id: StableId,
        skill_version: SchemaVersion,
        artifact_schema_version: SchemaVersion = _DEFAULT_SCHEMA_VERSION,
    ) -> None:
        self._gateway = gateway
        self._artifacts = artifacts
        self._prompts = prompts
        self._skills = skills
        self._prompt_id = prompt_id
        self._prompt_version = prompt_version
        self._skill_id = skill_id
        self._skill_version = skill_version
        self._artifact_schema_version = artifact_schema_version

    async def propose(self, request: RecoveryReasonerRequest) -> RecoveryReasonerResult:
        skill_text, skill_ref = self._skills.resolve(self._skill_id, self._skill_version)
        if skill_ref.content_hash != request.skill_contract_hash:
            raise RecoveryReasonerRejected("frozen recovery skill hash does not match registry")

        payload, source_hashes = self._incident_payload(request)
        rendered, prompt_refs = self._prompts.render(
            ((self._prompt_id, self._prompt_version),),
            task_payload=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            source_hashes=source_hashes,
        )
        if len(prompt_refs) != 1 or prompt_refs[0].content_hash != request.prompt_contract_hash:
            raise RecoveryReasonerRejected("frozen recovery prompt hash does not match registry")
        prompt = rendered + "\n\n<RECOVERY_SKILL>\n" + skill_text + "\n</RECOVERY_SKILL>"
        if len(prompt.encode("utf-8")) > request.budget.max_context_bytes:
            raise RecoveryReasonerContextExceeded(
                "recovery reasoner context exceeds the pre-registered byte budget"
            )

        model_request = ModelRequest(
            request_id=request.model_request_id,
            run_id=request.run_id,
            task_id=request.task_id,
            model_role=ModelRole.BATCH_TEST,
            purpose=ModelCallPurpose.EVALUATION,
            trace_id=f"u8c-recovery:{request.request_id.root}",
            prompt=prompt,
            prompt_contract_hashes=(request.prompt_contract_hash,),
            skill_contract_hashes=(request.skill_contract_hash,),
            max_output_tokens=request.budget.max_output_tokens,
            timeout_seconds=request.budget.timeout_seconds,
            enable_thinking=request.budget.enable_thinking,
            thinking_token_budget=request.budget.thinking_token_budget,
        )
        draft, call = await self._gateway.generate_structured_audited(
            model_request, RecoveryProposalDraft
        )
        selected = self._selected_candidate(request, draft)
        considered = tuple(candidate.action_id for candidate in request.candidates)
        expected_rejected = set(considered) - {selected.action_id}
        if set(draft.rejected_action_ids) != expected_rejected:
            raise RecoveryReasonerRejected(
                "recovery proposal must explicitly reject every unselected action"
            )
        proposal = RecoveryProposal(
            proposal_id=bounded_stable_id(
                f"recovery-proposal.{request.request_id.root}",
                f"recovery-proposal.{request.task_id.root}",
            ),
            request_id=request.request_id,
            project_id=request.project_id,
            run_id=request.run_id,
            task_id=request.task_id,
            incident_ref=request.incident_ref,
            failure_class=request.failure_class,
            basis_commit=request.basis_commit,
            safety_boundary_id=request.safety_boundary_id,
            selected_action_id=selected.action_id,
            selected_action_kind=selected.action_kind,
            selected_action_ref=selected.proposal_ref,
            selected_validation_ref=selected.validation_ref,
            considered_action_ids=considered,
            rejected_action_ids=draft.rejected_action_ids,
            rationale=draft.rationale,
            receipt_refs=request.receipt_refs,
            state_refs=request.state_refs,
            prompt_contract_hash=request.prompt_contract_hash,
            skill_contract_hash=request.skill_contract_hash,
            model_call=call,
        )
        proposal_ref = self._artifacts.put(
            canonical_json_bytes(proposal.model_dump(mode="json")),
            RECOVERY_PROPOSAL_MEDIA_TYPE,
            self._artifact_schema_version,
        )
        return RecoveryReasonerResult(proposal=proposal, proposal_ref=proposal_ref)

    def _incident_payload(
        self, request: RecoveryReasonerRequest
    ) -> tuple[dict[str, object], tuple[ArtifactId, ...]]:
        refs = (
            *request.admission.evidence_refs,
            request.incident_ref,
            *request.receipt_refs,
            *request.state_refs,
            *(candidate.proposal_ref for candidate in request.candidates),
            *(candidate.validation_ref for candidate in request.candidates),
        )
        objects = []
        for ref in refs:
            raw = self._artifacts.read_verified(ref)
            try:
                value: object = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = raw.decode("utf-8", errors="replace")
            objects.append(
                {
                    "artifact_id": ref.artifact_id.root,
                    "media_type": ref.media_type,
                    "value": value,
                }
            )
        payload: dict[str, object] = {
            "request_id": request.request_id.root,
            "failure_class": request.failure_class.value,
            "basis_commit": request.basis_commit.root,
            "safety_boundary_id": request.safety_boundary_id.root,
            "allowed_action_kinds": tuple(item.value for item in request.allowed_action_kinds),
            "candidates": tuple(
                {
                    "action_id": item.action_id.root,
                    "action_kind": item.action_kind.value,
                    "proposal_artifact_id": item.proposal_ref.artifact_id.root,
                    "validation_artifact_id": item.validation_ref.artifact_id.root,
                }
                for item in request.candidates
            ),
            "immutable_objects": tuple(objects),
        }
        return payload, tuple(ref.artifact_id for ref in refs)

    @staticmethod
    def _selected_candidate(
        request: RecoveryReasonerRequest, draft: RecoveryProposalDraft
    ) -> RecoveryActionCandidate:
        matches = tuple(
            candidate
            for candidate in request.candidates
            if candidate.action_id == draft.selected_action_id
        )
        if len(matches) != 1:
            raise RecoveryReasonerRejected(
                "recovery proposal selected an action outside the validated candidate set"
            )
        return matches[0]


__all__ = [
    "RecoveryReasonerContextExceeded",
    "RecoveryReasonerRejected",
    "RecoveryReasonerService",
]
