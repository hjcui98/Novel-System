"""Model-assisted validation that can only add findings after deterministic checks."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.changes import (
    CandidateChangeBundle,
    ModelValidationDraft,
    ModelValidationSeverity,
    ValidationFinding,
    ValidationReport,
    ValidationStatus,
)
from novel_agent.domain.ids import CommitId, StableId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.text import EvidenceRef
from novel_agent.services.benchmark_importer import validate_evidence_ref
from novel_agent.services.model_gateway import ModelGateway
from novel_agent.services.validation import Stage1Validator


class ModelValidationContractError(ValueError):
    pass


class ModelAssistedValidator:
    def __init__(
        self,
        gateway: ModelGateway,
        deterministic: Stage1Validator | None = None,
    ) -> None:
        self._gateway = gateway
        self._deterministic = deterministic or Stage1Validator()

    async def validate(
        self,
        bundle: CandidateChangeBundle,
        canonical_world: WorldRootDocument,
        proposed_world: WorldRootDocument,
        evidence_root: TextRootDocument,
        request: ModelRequest,
        *,
        canonical_commit: CommitId,
    ) -> tuple[ValidationReport, ModelCallRecord | None]:
        deterministic = self._deterministic.validate(
            bundle,
            canonical_world,
            proposed_world,
            evidence_root,
            canonical_commit=canonical_commit,
        )
        if deterministic.status is ValidationStatus.FAILED:
            return deterministic, None
        allowed_evidence = {
            evidence.evidence_id: evidence
            for operation in bundle.observed_changes.operations
            for evidence in operation.evidence_refs
        }
        safe_request = request.model_copy(
            update={
                "prompt": self._prompt(
                    bundle,
                    canonical_world,
                    proposed_world,
                    evidence_root,
                    tuple(allowed_evidence.values()),
                )
            }
        )
        draft, call = await self._gateway.generate_structured(safe_request, ModelValidationDraft)
        findings: list[ValidationFinding] = list(deterministic.findings)
        seen: set[tuple[str, tuple[StableId, ...]]] = set()
        for finding in draft.findings:
            for evidence in finding.evidence_refs:
                validate_evidence_ref(evidence, evidence_root)
                if evidence.evidence_id not in allowed_evidence:
                    raise ModelValidationContractError(
                        "model validator cited evidence outside the candidate"
                    )
                if evidence != allowed_evidence[evidence.evidence_id]:
                    raise ModelValidationContractError(
                        "model validator altered a candidate EvidenceRef"
                    )
            identity = (
                finding.code,
                tuple(evidence.evidence_id for evidence in finding.evidence_refs),
            )
            if identity in seen:
                raise ModelValidationContractError("model validator returned a duplicate finding")
            seen.add(identity)
            findings.append(
                ValidationFinding(
                    code=f"MODEL_{finding.code}",
                    severity=finding.severity.value,
                    message=finding.message,
                    evidence_refs=finding.evidence_refs,
                )
            )
        status = (
            ValidationStatus.FAILED
            if any(finding.severity == ModelValidationSeverity.ERROR.value for finding in findings)
            else ValidationStatus.NEEDS_REVIEW
            if findings
            else ValidationStatus.PASSED
        )
        return (
            ValidationReport(
                report_id=StableId(f"validation.model.{bundle.bundle_id.root}"),
                bundle_id=bundle.bundle_id,
                status=status,
                findings=tuple(findings),
                schema_version=deterministic.schema_version,
                validation_profile=(
                    f"{deterministic.validation_profile}+model:{call.model}@{call.model_version}"
                ),
                validated_at=datetime.now(UTC),
            ),
            call,
        )

    @staticmethod
    def _prompt(
        bundle: CandidateChangeBundle,
        canonical_world: WorldRootDocument,
        proposed_world: WorldRootDocument,
        evidence_root: TextRootDocument,
        evidence_refs: tuple[EvidenceRef, ...],
    ) -> str:
        blocks = {
            block.block_id: block
            for chapter in evidence_root.chapters
            for scene in chapter.scenes
            for block in scene.blocks
        }
        evidence_payload = []
        for evidence in evidence_refs:
            validate_evidence_ref(evidence, evidence_root)
            assert evidence.span is not None
            block = blocks[evidence.span.block_id]
            evidence_payload.append(
                {
                    "evidence": evidence.model_dump(mode="json"),
                    "text": block.text[evidence.span.start : evidence.span.end],
                }
            )
        return (
            "Review the candidate change for semantic contradictions or truth-class errors. "
            "Return ModelValidationDraft JSON. You may only cite the supplied candidate evidence; "
            "you cannot approve or suppress deterministic findings.\n"
            f"CANDIDATE={bundle.model_dump_json()}\n"
            f"CANONICAL_WORLD={canonical_world.model_dump_json()}\n"
            f"PROPOSED_WORLD={proposed_world.model_dump_json()}\n"
            f"EVIDENCE={json.dumps(evidence_payload, ensure_ascii=False, sort_keys=True)}"
        )
