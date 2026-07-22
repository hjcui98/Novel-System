"""Fail-closed deterministic validation for Stage 1 candidate world changes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.changes import (
    CandidateChangeBundle,
    ChangeOperation,
    ChangeOperationType,
    StateTransitionPolicy,
    ValidationFinding,
    ValidationReport,
    ValidationStatus,
)
from novel_agent.domain.ids import CommitId, SchemaVersion, StableId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.world import Event, TruthClass
from novel_agent.services.benchmark_importer import (
    BenchmarkImportError,
    validate_evidence_ref,
)
from novel_agent.services.content_addressing import (
    canonical_json_bytes,
    world_root_content_id,
)
from novel_agent.services.overlay import OverlayError, WorldOverlay


class Stage1Validator:
    _NON_FACT_MARKERS = ("据说", "传闻", "梦见", "假如", "也许")
    _OBLIGATION_TRANSITIONS: ClassVar[dict[str, frozenset[str]]] = {
        "open": frozenset({"progressed", "resolved", "abandoned"}),
        "progressed": frozenset({"resolved", "abandoned"}),
        "resolved": frozenset(),
        "abandoned": frozenset(),
    }

    def __init__(self, transition_policy: StateTransitionPolicy | None = None) -> None:
        self._transition_policy = transition_policy or StateTransitionPolicy(
            policy_id=StableId("transition.stage1-structural-v1"),
            schema_version=SchemaVersion("0.1.0"),
        )

    def validate(
        self,
        bundle: CandidateChangeBundle,
        canonical_world: WorldRootDocument,
        proposed_world: WorldRootDocument,
        evidence_root: TextRootDocument,
        *,
        canonical_commit: CommitId | None = None,
    ) -> ValidationReport:
        findings: list[ValidationFinding] = []
        expected_base = canonical_commit or canonical_world.source_commit
        if bundle.base_commit != expected_base:
            findings.append(self._finding("BASE_MISMATCH", "error", "base commit mismatch"))
        if world_root_content_id(proposed_world) != proposed_world.root_hash:
            findings.append(
                self._finding("ROOT_HASH_MISMATCH", "error", "world root hash mismatch")
            )
        self._check_write_conflicts(bundle, findings)
        self._check_evidence_and_truth(bundle, evidence_root, findings)
        self._check_transitions_and_order(
            bundle, canonical_world, proposed_world, evidence_root, findings
        )
        try:
            expected = WorldOverlay().apply(
                canonical_world,
                bundle.observed_changes,
                canonical_commit=bundle.base_commit,
            )
            if expected != proposed_world:
                findings.append(
                    self._finding(
                        "OVERLAY_MISMATCH", "error", "proposed world differs from overlay result"
                    )
                )
        except OverlayError as error:
            findings.append(self._finding("INVALID_OVERLAY", "error", str(error)))
        status = (
            ValidationStatus.FAILED
            if any(item.severity == "error" for item in findings)
            else ValidationStatus.NEEDS_REVIEW
            if findings
            else ValidationStatus.PASSED
        )
        return ValidationReport(
            report_id=StableId(f"validation.{bundle.bundle_id.root}"),
            bundle_id=bundle.bundle_id,
            status=status,
            findings=tuple(findings),
            schema_version=SchemaVersion("0.1.0"),
            validation_profile=(
                f"stage1-validator-v1+{self._transition_policy.policy_id.root}@"
                f"{self._transition_policy.schema_version.root}"
            ),
            validated_at=datetime.now(UTC),
        )

    def _check_transitions_and_order(
        self,
        bundle: CandidateChangeBundle,
        canonical: WorldRootDocument,
        proposed: WorldRootDocument,
        evidence_root: TextRootDocument,
        findings: list[ValidationFinding],
    ) -> None:
        old_states = {record.state_id: record for record in canonical.states}
        new_states = {record.state_id: record for record in proposed.states}
        old_obligations = {record.obligation_id: record for record in canonical.obligations}
        new_obligations = {record.obligation_id: record for record in proposed.obligations}
        new_events = {record.event_id: record for record in proposed.events}
        chapter_indexes = {
            chapter.chapter_id: chapter.chapter_index for chapter in evidence_root.chapters
        }
        rules = {rule.predicate: rule for rule in self._transition_policy.rules}
        for operation in bundle.observed_changes.operations:
            payload = operation.payload
            if not isinstance(payload, dict):
                continue
            raw_kind = payload.get("record_type")
            if not isinstance(raw_kind, str):
                continue
            kind = raw_kind
            raw_record = payload.get("record")
            if kind != "entity" and isinstance(raw_record, dict):
                expected_evidence = [
                    evidence.model_dump(mode="json") for evidence in operation.evidence_refs
                ]
                if raw_record.get("evidence_refs") != expected_evidence:
                    findings.append(
                        self._finding(
                            "RECORD_EVIDENCE_MISMATCH",
                            "error",
                            "record evidence differs from operation evidence",
                        )
                    )
            if operation.operation is not ChangeOperationType.REPLACE:
                self._check_narrative_order(
                    operation.target_id,
                    kind,
                    new_events,
                    chapter_indexes,
                    operation,
                    findings,
                )
                continue
            if kind == "state":
                old = old_states.get(operation.target_id)
                new = new_states.get(operation.target_id)
                if old is not None and new is not None:
                    if old.subject_id != new.subject_id or old.predicate != new.predicate:
                        findings.append(
                            self._finding(
                                "STATE_IDENTITY_MUTATION",
                                "error",
                                "state replacement changed subject or predicate",
                            )
                        )
                    else:
                        rule = rules.get(old.predicate)
                        if rule is None and not self._transition_policy.allow_unlisted_predicates:
                            findings.append(
                                self._finding(
                                    "UNLISTED_STATE_TRANSITION",
                                    "error",
                                    f"predicate has no transition rule: {old.predicate}",
                                )
                            )
                        elif rule is not None and not any(
                            canonical_json_bytes(edge.from_value) == canonical_json_bytes(old.value)
                            and canonical_json_bytes(edge.to_value)
                            == canonical_json_bytes(new.value)
                            for edge in rule.allowed
                        ):
                            findings.append(
                                self._finding(
                                    "ILLEGAL_STATE_TRANSITION",
                                    "error",
                                    f"transition is not allowed for predicate: {old.predicate}",
                                )
                            )
            elif kind == "obligation":
                old_obligation = old_obligations.get(operation.target_id)
                new_obligation = new_obligations.get(operation.target_id)
                if (
                    old_obligation is not None
                    and new_obligation is not None
                    and old_obligation.status != new_obligation.status
                    and new_obligation.status.value
                    not in self._OBLIGATION_TRANSITIONS[old_obligation.status.value]
                ):
                    findings.append(
                        self._finding(
                            "ILLEGAL_OBLIGATION_TRANSITION",
                            "error",
                            "plan obligation lifecycle transition is not allowed",
                        )
                    )
            self._check_narrative_order(
                operation.target_id,
                kind,
                new_events,
                chapter_indexes,
                operation,
                findings,
            )

    def _check_narrative_order(
        self,
        target_id: StableId,
        kind: str,
        events: dict[StableId, Event],
        chapter_indexes: dict[StableId, int],
        operation: ChangeOperation,
        findings: list[ValidationFinding],
    ) -> None:
        if kind != "event":
            return
        event = events.get(target_id)
        narrative_order = event.narrative_order if event is not None else None
        if narrative_order is not None and narrative_order.chapter_index not in {
            chapter_indexes.get(evidence.chapter_id)
            for evidence in operation.evidence_refs
            if evidence.chapter_id is not None
        }:
            findings.append(
                self._finding(
                    "NARRATIVE_ORDER_EVIDENCE_MISMATCH",
                    "error",
                    "event narrative order differs from its evidence chapter",
                )
            )

    @staticmethod
    def _check_write_conflicts(
        bundle: CandidateChangeBundle, findings: list[ValidationFinding]
    ) -> None:
        identities = [
            (operation.root_kind, operation.target_id)
            for operation in bundle.observed_changes.operations
        ]
        if len(identities) != len(set(identities)):
            findings.append(
                Stage1Validator._finding(
                    "WRITE_CONFLICT", "error", "write set targets the same record more than once"
                )
            )

    def _check_evidence_and_truth(
        self,
        bundle: CandidateChangeBundle,
        root: TextRootDocument,
        findings: list[ValidationFinding],
    ) -> None:
        for operation in bundle.observed_changes.operations:
            if not operation.evidence_refs:
                findings.append(
                    self._finding("MISSING_EVIDENCE", "error", "world change has no evidence")
                )
                continue
            resolved_text: list[str] = []
            for evidence in operation.evidence_refs:
                try:
                    validate_evidence_ref(evidence, root)
                except BenchmarkImportError as error:
                    findings.append(
                        ValidationFinding(
                            code="INVALID_EVIDENCE",
                            severity="error",
                            message=str(error),
                            evidence_refs=(evidence,),
                        )
                    )
                    continue
                assert evidence.span is not None
                block = next(
                    block
                    for chapter in root.chapters
                    for scene in chapter.scenes
                    for block in scene.blocks
                    if block.block_id == evidence.span.block_id
                )
                resolved_text.append(block.text[evidence.span.start : evidence.span.end])
            payload = operation.payload
            record = payload.get("record", {}) if isinstance(payload, dict) else {}
            truth = record.get("truth_class") if isinstance(record, dict) else None
            if truth == TruthClass.ACCEPTED_WORLD_FACT.value and any(
                marker in text for marker in self._NON_FACT_MARKERS for text in resolved_text
            ):
                findings.append(
                    self._finding(
                        "FALSE_WORLD_FACT_PROMOTION",
                        "error",
                        "non-factual assertion was promoted to accepted world fact",
                    )
                )

    @staticmethod
    def _finding(code: str, severity: str, message: str) -> ValidationFinding:
        return ValidationFinding(code=code, severity=severity, message=message)
