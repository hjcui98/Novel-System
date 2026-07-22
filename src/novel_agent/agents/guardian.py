"""Version-pinned Stage 2 Memory Guardian RISK_REVIEW agent facade."""

from __future__ import annotations

from novel_agent.agents.runner import StructuredAgentRunner
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.changes import ObservedChangeSet, ValidationReport, ValidationStatus
from novel_agent.domain.ids import SchemaVersion, StableId
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.stage2 import (
    AgentMode,
    AgentType,
    GuardianDecision,
    GuardianDecisionDraft,
    PatchRiskAssessment,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes


class GuardianInvocationError(ValueError):
    pass


class GuardianRiskReviewAgent:
    def __init__(
        self,
        runner: StructuredAgentRunner,
        artifacts: ArtifactRepository,
    ) -> None:
        self._runner = runner
        self._artifacts = artifacts

    async def review(
        self,
        *,
        version: SchemaVersion,
        changes: ObservedChangeSet,
        validation: ValidationReport,
        risk: PatchRiskAssessment,
        request: ModelRequest,
        evidence_root: TextRootDocument | None = None,
    ) -> tuple[GuardianDecision, ModelCallRecord]:
        if validation.status is ValidationStatus.FAILED:
            raise GuardianInvocationError("deterministic validation failure blocks Guardian call")
        if risk.change_set_id != changes.change_set_id or risk.base_commit != changes.base_commit:
            raise GuardianInvocationError("risk assessment does not belong to the candidate patch")
        if not risk.requires_guardian:
            raise GuardianInvocationError("low-risk patch must not invoke Guardian")
        changes_artifact = self._artifacts.put(
            canonical_json_bytes(changes.model_dump(mode="json")),
            "application/vnd.novel-agent.observed-change-set+json",
            version,
        )
        validation_artifact = self._artifacts.put(
            canonical_json_bytes(validation.model_dump(mode="json")),
            "application/vnd.novel-agent.validation-report+json",
            version,
        )
        evidence_context = self._evidence_context(changes, evidence_root)
        evidence_artifact = (
            self._artifacts.put(
                canonical_json_bytes(evidence_context),
                "application/vnd.novel-agent.guardian-evidence-context+json",
                version,
            )
            if evidence_context
            else None
        )
        input_artifacts = (
            (changes_artifact, validation_artifact, evidence_artifact)
            if evidence_artifact is not None
            else (changes_artifact, validation_artifact)
        )
        prepared = self._runner.prepare(
            AgentType.MEMORY_GUARDIAN,
            AgentMode.RISK_REVIEW,
            version.root,
            request,
            (
                f"RISK={risk.model_dump_json()}\n"
                f"CHANGES={changes.model_dump_json()}\n"
                f"VALIDATION={validation.model_dump_json()}\n"
                f"EVIDENCE_CONTEXT={canonical_json_bytes(evidence_context).decode()}"
            ),
            source_hashes=tuple(artifact.artifact_id for artifact in input_artifacts),
            input_artifacts=input_artifacts,
            base_commit=changes.base_commit,
        )
        execution = await self._runner.execute(prepared, GuardianDecisionDraft)
        draft = execution.output
        decision_artifact = self._artifacts.put(
            canonical_json_bytes(draft.model_dump(mode="json")),
            "application/vnd.novel-agent.guardian-decision-draft+json",
            version,
        )
        revision_artifact = (
            self._artifacts.put(
                canonical_json_bytes(draft.revised_candidate),
                "application/vnd.novel-agent.guardian-revision+json",
                version,
            )
            if draft.revised_candidate is not None
            else None
        )
        outputs = (
            (decision_artifact, revision_artifact)
            if revision_artifact is not None
            else (decision_artifact,)
        )
        receipt = self._runner.receipt(prepared, execution.model_call, output_artifacts=outputs)
        risk_codes = tuple(sorted(set(risk.risk_codes) | set(draft.risk_codes)))
        return (
            GuardianDecision(
                decision_id=StableId(
                    "guardian-decision."
                    + decision_artifact.artifact_id.root.removeprefix("sha256:")[:24]
                ),
                proposal_id=changes.change_set_id,
                base_commit=changes.base_commit,
                outcome=draft.outcome,
                risk_codes=risk_codes,
                reasons=draft.reasons,
                revised_candidate=revision_artifact,
                receipt=receipt,
            ),
            execution.model_call,
        )

    @staticmethod
    def _evidence_context(
        changes: ObservedChangeSet,
        evidence_root: TextRootDocument | None,
    ) -> tuple[dict[str, object], ...]:
        if evidence_root is None:
            return ()
        blocks = {
            block.block_id: block
            for chapter in evidence_root.chapters
            for scene in chapter.scenes
            for block in scene.blocks
        }
        context: list[dict[str, object]] = []
        seen: set[StableId] = set()
        for operation in changes.operations:
            for evidence in operation.evidence_refs:
                if evidence.evidence_id in seen or evidence.span is None:
                    continue
                block = blocks.get(evidence.span.block_id)
                if block is None:
                    raise GuardianInvocationError(
                        "Guardian evidence is outside the supplied visible TextRoot"
                    )
                excerpt = block.text[evidence.span.start : evidence.span.end]
                context.append(
                    {
                        "evidence_id": evidence.evidence_id.root,
                        "chapter_id": block.chapter_id.root,
                        "block_id": block.block_id.root,
                        "text": excerpt[:2000],
                        "truncated": len(excerpt) > 2000,
                    }
                )
                seen.add(evidence.evidence_id)
        return tuple(context)
