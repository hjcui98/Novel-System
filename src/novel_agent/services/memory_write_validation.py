"""Validation v2 adapters for the Stage 2W workflow."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.changes import ValidationFinding, ValidationReport, ValidationStatus
from novel_agent.domain.ids import StableId
from novel_agent.domain.memory_write import (
    BlockingScope,
    CandidateMaterialization,
    CandidateRevision,
    CanonicalWriteBasis,
    FindingRetryability,
    RepairScope,
    ValidationDecision,
    ValidationDisposition,
    ValidationFindingCategory,
    ValidationFindingV2,
    ValidationSeverity,
)
from novel_agent.services.memory_repair_policy import FINDING_REGISTRY
from novel_agent.services.overlay import WorldOverlay
from novel_agent.services.validation import Stage1Validator


class ValidationV2AdapterError(ValueError):
    pass


class ModelFindingProvider(Protocol):
    async def __call__(
        self,
        candidate: CandidateRevision,
        materialization: CandidateMaterialization,
        basis: CanonicalWriteBasis,
    ) -> tuple[ValidationFindingV2, ...]: ...


class ValidationV1Adapter:
    """Conservatively map an existing Stage 1 report to Validation v2."""

    @staticmethod
    def convert(
        report: ValidationReport,
        *,
        candidate: CandidateRevision,
        materialization: CandidateMaterialization,
    ) -> ValidationDecision:
        findings = tuple(
            ValidationV1Adapter._finding(item, index, candidate, materialization)
            for index, item in enumerate(report.findings, start=1)
        )
        disposition = _disposition(report.status, findings)
        return ValidationDecision(
            decision_id=StableId(f"validation-v2.{candidate.content_hash.root[7:39]}"),
            candidate_id=candidate.candidate_id,
            candidate_content_hash=candidate.content_hash,
            materialization_receipt=materialization.materialization_receipt,
            proposed_roots_hash=materialization.proposed_roots_hash,
            base_commit=candidate.base_commit,
            disposition=disposition,
            findings=findings,
            deterministic_profile=report.validation_profile,
            validated_at=report.validated_at,
        )

    @staticmethod
    def _finding(
        finding: ValidationFinding,
        index: int,
        candidate: CandidateRevision,
        materialization: CandidateMaterialization,
    ) -> ValidationFindingV2:
        rule = next((item for item in FINDING_REGISTRY if item.code == finding.code), None)
        operation_ids = _operation_ids(candidate, finding, materialization)
        category = (
            ValidationFindingCategory(rule.category)
            if rule is not None
            else _category(finding.code)
        )
        severity = _severity(finding.severity)
        retryability = (
            rule.retryability
            if rule is not None
            else (
                FindingRetryability.NON_REPAIRABLE
                if severity in {ValidationSeverity.ERROR, ValidationSeverity.CRITICAL}
                else FindingRetryability.REVIEW
            )
        )
        strategies = () if rule is None else rule.strategies
        field_paths = _field_paths(finding.code)
        scope = RepairScope(operation_ids=operation_ids, field_paths=field_paths)
        return ValidationFindingV2(
            finding_id=StableId(f"finding.{candidate.candidate_id.root}.{index}"),
            code=finding.code,
            category=category,
            severity=severity,
            message=finding.message,
            operation_ids=operation_ids,
            field_paths=field_paths,
            canonical_record_refs=(),
            evidence_refs=finding.evidence_refs,
            retryability=retryability,
            suggested_strategies=strategies,
            blocking_scope=BlockingScope.OPERATION if operation_ids else BlockingScope.CANDIDATE,
            allowed_repair_scope=scope,
            requires_context_refresh=finding.code in {"INVALID_EVIDENCE", "INVALID_EVIDENCE_REF"},
            requires_guardian=retryability is FindingRetryability.REVIEW,
            requires_human=finding.code in {"TRUTH_PROMOTION", "FUTURE_EVIDENCE"},
        )


class Stage2ValidationV2Adapter:
    """Run deterministic validation first and optionally add model findings."""

    def __init__(
        self,
        deterministic: Stage1Validator | None = None,
        model_findings: ModelFindingProvider | None = None,
        proposed_text_loader: Callable[[ArtifactRef], TextRootDocument] | None = None,
    ) -> None:
        self._deterministic = deterministic or Stage1Validator()
        self._model_findings = model_findings
        self._proposed_text_loader = proposed_text_loader

    async def validate(
        self,
        candidate: CandidateRevision,
        materialization: CandidateMaterialization,
        canonical: CanonicalWriteBasis,
    ) -> ValidationDecision:
        bundle = materialization.bundle
        if bundle is None:
            raise ValidationV2AdapterError(
                "validation requires a materialized CandidateChangeBundle"
            )
        if (
            bundle.base_commit != canonical.commit_id
            or candidate.base_commit != canonical.commit_id
        ):
            return self._fatal_decision(
                candidate,
                materialization,
                "BASE_COMMIT_MISMATCH",
                "candidate/materialization basis differs from canonical basis",
            )
        report = self._deterministic_report(bundle, canonical)
        decision = ValidationV1Adapter.convert(
            report,
            candidate=candidate,
            materialization=materialization,
        )
        if (
            decision.disposition is not ValidationDisposition.NON_REPAIRABLE
            and self._model_findings is not None
        ):
            extra = await self._model_findings(candidate, materialization, canonical)
            if extra:
                findings = (*decision.findings, *extra)
                decision = decision.model_copy(
                    update={
                        "findings": findings,
                        "disposition": _disposition_from_findings(findings),
                        "model_profile": "model-assisted-v2",
                    }
                )
        return decision

    def _deterministic_report(
        self,
        bundle: object,
        canonical: CanonicalWriteBasis,
    ) -> ValidationReport:
        if canonical.canonical_world is None or canonical.canonical_text is None:
            # Without the typed legacy roots the structural adapter can still
            # prove the basis and bundle binding; a richer port may add Stage 1.
            return ValidationReport(
                report_id=StableId(f"validation.structural.{bundle.bundle_id.root}"),  # type: ignore[attr-defined]
                bundle_id=bundle.bundle_id,  # type: ignore[attr-defined]
                status=ValidationStatus.PASSED,
                schema_version=bundle.proposed_roots.schema_version,  # type: ignore[attr-defined]
                validation_profile="stage2w-structural-v2",
                validated_at=datetime.now(UTC),
            )
        try:
            proposed = WorldOverlay().apply(
                canonical.canonical_world,
                bundle.observed_changes,  # type: ignore[attr-defined]
                canonical_commit=canonical.commit_id,
            )
        except Exception as error:
            return ValidationReport(
                report_id=StableId(f"validation.overlay.{bundle.bundle_id.root}"),  # type: ignore[attr-defined]
                bundle_id=bundle.bundle_id,  # type: ignore[attr-defined]
                status=ValidationStatus.FAILED,
                findings=(
                    ValidationFinding(code="INVALID_OVERLAY", severity="error", message=str(error)),
                ),
                schema_version=bundle.proposed_roots.schema_version,  # type: ignore[attr-defined]
                validation_profile="stage2w-structural-v2",
                validated_at=datetime.now(UTC),
            )
        validation_text = canonical.canonical_text
        manifest = canonical.root_manifest
        evidence_present = any(
            operation.evidence_refs
            for operation in bundle.observed_changes.operations  # type: ignore[attr-defined]
        )
        proposed_text_ref = bundle.proposed_roots.text_root  # type: ignore[attr-defined]
        if (
            evidence_present
            and manifest is not None
            and proposed_text_ref.artifact_id != manifest.text_root.artifact_id
        ):
            if self._proposed_text_loader is None:
                return self._text_unavailable_report(
                    bundle,
                    "proposed TextRoot loader is not configured",
                )
            try:
                validation_text = self._proposed_text_loader(proposed_text_ref)
            except Exception as error:
                return self._text_unavailable_report(
                    bundle,
                    f"proposed TextRoot cannot be loaded: {error}",
                )
        return self._deterministic.validate(
            bundle,  # type: ignore[arg-type]
            canonical.canonical_world,
            proposed,
            validation_text,
            canonical_commit=canonical.commit_id,
        )

    @staticmethod
    def _text_unavailable_report(bundle: object, message: str) -> ValidationReport:
        return ValidationReport(
            report_id=StableId(f"validation.proposed-text.{bundle.bundle_id.root}"),  # type: ignore[attr-defined]
            bundle_id=bundle.bundle_id,  # type: ignore[attr-defined]
            status=ValidationStatus.FAILED,
            findings=(
                ValidationFinding(
                    code="PROPOSED_TEXT_UNAVAILABLE",
                    severity="error",
                    message=message,
                ),
            ),
            schema_version=bundle.proposed_roots.schema_version,  # type: ignore[attr-defined]
            validation_profile="stage2w-proposed-text-v2",
            validated_at=datetime.now(UTC),
        )

    @staticmethod
    def _fatal_decision(
        candidate: CandidateRevision,
        materialization: CandidateMaterialization,
        code: str,
        message: str,
    ) -> ValidationDecision:
        finding = ValidationFindingV2(
            finding_id=StableId(f"finding.{candidate.candidate_id.root}.basis"),
            code=code,
            category=ValidationFindingCategory.BASIS,
            severity=ValidationSeverity.ERROR,
            message=message,
            retryability=FindingRetryability.NON_REPAIRABLE,
            blocking_scope=BlockingScope.CANDIDATE,
            allowed_repair_scope=RepairScope(),
        )
        return ValidationDecision(
            decision_id=StableId(f"validation-v2.{candidate.content_hash.root[7:39]}.fatal"),
            candidate_id=candidate.candidate_id,
            candidate_content_hash=candidate.content_hash,
            materialization_receipt=materialization.materialization_receipt,
            proposed_roots_hash=materialization.proposed_roots_hash,
            base_commit=candidate.base_commit,
            disposition=ValidationDisposition.NON_REPAIRABLE,
            findings=(finding,),
            deterministic_profile="stage2w-basis-v2",
            validated_at=datetime.now(UTC),
        )


def _disposition(
    status: ValidationStatus, findings: tuple[ValidationFindingV2, ...]
) -> ValidationDisposition:
    if status is ValidationStatus.PASSED and not findings:
        return ValidationDisposition.PASS
    return _disposition_from_findings(findings)


def _disposition_from_findings(findings: tuple[ValidationFindingV2, ...]) -> ValidationDisposition:
    if not findings:
        return ValidationDisposition.PASS
    if any(item.retryability is FindingRetryability.NON_REPAIRABLE for item in findings):
        return ValidationDisposition.NON_REPAIRABLE
    if any(
        item.requires_human or item.retryability is FindingRetryability.REVIEW for item in findings
    ):
        return ValidationDisposition.REVIEW_REQUIRED
    return (
        ValidationDisposition.PARTIAL_REPAIRABLE
        if any(item.blocking_scope is BlockingScope.OPERATION for item in findings)
        else ValidationDisposition.REPAIRABLE
    )


def _operation_ids(
    candidate: CandidateRevision,
    finding: ValidationFinding,
    materialization: CandidateMaterialization,
) -> tuple[StableId, ...]:
    # Stage 1 findings predate operation bindings.  Recover a conservative
    # binding from the immutable materialized bundle when the finding code has
    # a deterministic operation shape; unknown multi-operation findings stay
    # candidate-scoped instead of guessing.
    bundle = materialization.bundle
    if bundle is None:
        return ()
    operations = bundle.observed_changes.operations
    code = finding.code
    matched: list[StableId] = []
    for operation in operations:
        payload = operation.payload
        record_type = payload.get("record_type") if isinstance(payload, dict) else None
        if code in {
            "STATE_IDENTITY_MUTATION",
            "ILLEGAL_STATE_TRANSITION",
            "UNLISTED_STATE_TRANSITION",
        }:
            matches = record_type == "state" and operation.operation.value == "replace"
        elif code == "RECORD_EVIDENCE_MISMATCH":
            record = payload.get("record") if isinstance(payload, dict) else None
            expected = [item.model_dump(mode="json") for item in operation.evidence_refs]
            matches = isinstance(record, dict) and record.get("evidence_refs") != expected
        elif code == "CREATE_TARGET_EXISTS":
            matches = operation.operation.value == "create"
        elif code == "REPLACE_TARGET_MISSING":
            matches = operation.operation.value == "replace"
        elif code == "INVALID_EVIDENCE_REF":
            matches = bool(operation.evidence_refs)
        elif code == "OPERATION_TARGET_MISMATCH":
            record = payload.get("record") if isinstance(payload, dict) else None
            id_key = {
                "entity": "entity_id",
                "event": "event_id",
                "state": "state_id",
                "relation": "relation_id",
                "obligation": "obligation_id",
            }.get(str(record_type))
            matches = (
                isinstance(record, dict)
                and id_key is not None
                and record.get(id_key) != operation.target_id.root
            )
        else:
            matches = len(operations) == 1
        if matches:
            matched.append(operation.operation_id)
    del candidate
    return tuple(dict.fromkeys(matched))


def _field_paths(code: str) -> tuple[str, ...]:
    return {
        "STATE_IDENTITY_MUTATION": ("record.subject_id", "record.predicate"),
        "RECORD_EVIDENCE_MISMATCH": ("record.evidence_refs",),
        "INVALID_EVIDENCE": ("evidence_refs", "record.evidence_refs"),
        "INVALID_EVIDENCE_REF": ("evidence_refs", "record.evidence_refs"),
        "OPERATION_TARGET_MISMATCH": ("target_id", "record.id"),
    }.get(code, ())


def _category(code: str) -> ValidationFindingCategory:
    upper = code.upper()
    if "EVIDENCE" in upper:
        return ValidationFindingCategory.EVIDENCE
    if "IDENTITY" in upper or "TARGET" in upper:
        return ValidationFindingCategory.IDENTITY
    if "TRANSITION" in upper:
        return ValidationFindingCategory.TRANSITION
    if "TRUTH" in upper:
        return ValidationFindingCategory.TRUTH
    if "BASE" in upper:
        return ValidationFindingCategory.BASIS
    if "OVERLAY" in upper or "SCHEMA" in upper:
        return ValidationFindingCategory.SCHEMA
    return ValidationFindingCategory.UNKNOWN


def _severity(raw: str) -> ValidationSeverity:
    value = raw.lower()
    if value == "critical":
        return ValidationSeverity.CRITICAL
    if value == "warning":
        return ValidationSeverity.WARNING
    return ValidationSeverity.ERROR


__all__ = ["Stage2ValidationV2Adapter", "ValidationV1Adapter", "ValidationV2AdapterError"]
