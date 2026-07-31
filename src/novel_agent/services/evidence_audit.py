"""Read-only EvidenceRef audit for accepted C1-C20 commits (WP6)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.changes import EvidenceSupportDisposition
from novel_agent.domain.ids import ArtifactId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.text import EvidenceRef
from novel_agent.services.benchmark_importer import validate_evidence_ref
from novel_agent.services.content_addressing import quote_hash
from novel_agent.services.evidence_support import EvidenceSupportGate


@dataclass(frozen=True, slots=True)
class EvidenceAuditFinding:
    record_kind: str
    record_id: str
    predicate_value_summary: str
    evidence_id: str
    chapter_id: str | None
    block_id: str | None
    start: int | None
    end: int | None
    selected_text_hash: str | None
    hard_validation: str
    semantic_disposition: str | None
    risk_tags: tuple[str, ...]
    severity: str
    recommended_action: str


class EvidenceRefAuditor:
    def __init__(self, support_gate: EvidenceSupportGate | None = None) -> None:
        self._support = support_gate or EvidenceSupportGate()

    def audit_world(
        self,
        world: WorldRootDocument,
        text_root: TextRootDocument,
        *,
        historical_text_roots: Mapping[ArtifactId, TextRootDocument] | None = None,
    ) -> tuple[EvidenceAuditFinding, ...]:
        roots_by_hash = dict(historical_text_roots or {})
        roots_by_hash.setdefault(text_root.root_hash, text_root)
        findings: list[EvidenceAuditFinding] = []
        for kind, records in (
            ("state", world.states),
            ("relation", world.relations),
            ("obligation", world.obligations),
            ("event", world.events),
        ):
            for record in records:
                record_id = getattr(record, f"{kind}_id").root
                summary = self._summary(kind, record)
                evidence_refs = getattr(record, "evidence_refs", ()) or ()
                for evidence in evidence_refs:
                    findings.append(
                        self._audit_one(
                            record_kind=kind,
                            record_id=record_id,
                            summary=summary,
                            evidence=evidence,
                            text_root=roots_by_hash.get(evidence.root_hash),
                        )
                    )
        return tuple(findings)

    @staticmethod
    def write_report(
        findings: Sequence[EvidenceAuditFinding],
        output_dir: Path,
        *,
        audit_id: str,
    ) -> Path:
        target = output_dir / audit_id
        target.mkdir(parents=True, exist_ok=True)
        serialized = [asdict(item) for item in findings]
        summary = {
            "audit_id": audit_id,
            "finding_count": len(findings),
            "hard_failures": sum(1 for item in findings if item.hard_validation != "pass"),
            "unrelated": sum(
                1
                for item in findings
                if item.semantic_disposition == EvidenceSupportDisposition.UNRELATED.value
            ),
            "high_severity": sum(1 for item in findings if item.severity == "high"),
        }
        (target / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with (target / "evidence_findings.jsonl").open("w", encoding="utf-8") as handle:
            for item in serialized:
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
        mandatory = [item for item in serialized if item["severity"] == "high"]
        (target / "mandatory_findings.json").write_text(
            json.dumps(mandatory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        queue = [
            item
            for item in serialized
            if item["severity"] in {"high", "medium"}
            or item.get("semantic_disposition")
            in {
                EvidenceSupportDisposition.UNRELATED.value,
                EvidenceSupportDisposition.CONTRADICTS.value,
                EvidenceSupportDisposition.PARTIAL.value,
            }
        ]
        (target / "human_review_queue.json").write_text(
            json.dumps(queue, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest = {
            "audit_id": audit_id,
            "files": sorted(path.name for path in target.iterdir() if path.is_file()),
        }
        (target / "audit_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return target

    def _audit_one(
        self,
        *,
        record_kind: str,
        record_id: str,
        summary: str,
        evidence: EvidenceRef,
        text_root: TextRootDocument | None,
    ) -> EvidenceAuditFinding:
        risk_tags: list[str] = []
        hard = "pass"
        selected_hash: str | None = None
        block_id = evidence.span.block_id.root if evidence.span is not None else None
        start = evidence.span.start if evidence.span is not None else None
        end = evidence.span.end if evidence.span is not None else None
        resolved_text_root = text_root
        try:
            if resolved_text_root is None:
                raise LookupError("historical TextRoot is unavailable")
            validate_evidence_ref(evidence, resolved_text_root)
            if evidence.span is not None:  # pragma: no branch
                block = next(
                    block
                    for chapter in resolved_text_root.chapters
                    for scene in chapter.scenes
                    for block in scene.blocks
                    if block.block_id == evidence.span.block_id
                )
                if not (0 <= evidence.span.start < evidence.span.end <= len(block.text)):
                    hard = "range_invalid"
                selected = block.text[evidence.span.start : evidence.span.end]
                selected_hash = quote_hash(selected).root
                if evidence.span.start % 100 == 0 and evidence.span.end % 100 == 0:
                    risk_tags.append("ROUND_HUNDRED_OFFSET")
                elif evidence.span.start % 50 == 0 and evidence.span.end % 50 == 0:
                    risk_tags.append("ROUND_FIFTY_OFFSET")
                if evidence.span.end - evidence.span.start > 400:
                    risk_tags.append("UNUSUALLY_WIDE_SPAN")
        except Exception:
            hard = "identity_or_hash_failure"

        disposition = None
        if (
            hard == "pass"
            and selected_hash is not None
            and evidence.span is not None
            and resolved_text_root is not None
        ):
            tokens = [part for part in summary.replace("=", " ").split() if len(part) >= 2][:4]
            block = next(
                block
                for chapter in resolved_text_root.chapters
                for scene in chapter.scenes
                for block in scene.blocks
                if block.block_id == evidence.span.block_id
            )
            text = block.text[evidence.span.start : evidence.span.end]
            hits = sum(1 for token in tokens if token.casefold() in text.casefold())
            if not tokens:
                disposition = EvidenceSupportDisposition.PARTIAL.value
                risk_tags.append("PREDICATE_VALUE_LOW_SUPPORT")
            elif hits == 0:
                disposition = EvidenceSupportDisposition.UNRELATED.value
                risk_tags.append("SELECTED_TEXT_UNRELATED")
            elif hits < max(1, len(tokens) // 2):
                disposition = EvidenceSupportDisposition.PARTIAL.value
                risk_tags.append("PREDICATE_VALUE_LOW_SUPPORT")
            else:
                disposition = EvidenceSupportDisposition.SUPPORTS.value

        severity = "low"
        if hard != "pass" or disposition in {
            EvidenceSupportDisposition.UNRELATED.value,
            EvidenceSupportDisposition.CONTRADICTS.value,
        }:
            severity = "high"
        elif (
            "UNUSUALLY_WIDE_SPAN" in risk_tags
            or disposition == EvidenceSupportDisposition.PARTIAL.value
        ):
            severity = "medium"

        action = "none"
        if hard != "pass":
            action = "stop_c21_p0"
        elif severity == "high":
            action = "human_evidence_maintenance"
        elif severity == "medium":
            action = "human_sample_review"

        return EvidenceAuditFinding(
            record_kind=record_kind,
            record_id=record_id,
            predicate_value_summary=summary[:160],
            evidence_id=evidence.evidence_id.root,
            chapter_id=evidence.chapter_id.root if evidence.chapter_id else None,
            block_id=block_id,
            start=start,
            end=end,
            selected_text_hash=selected_hash,
            hard_validation=hard,
            semantic_disposition=disposition,
            risk_tags=tuple(risk_tags),
            severity=severity,
            recommended_action=action,
        )

    @staticmethod
    def _summary(kind: str, record: object) -> str:
        if kind == "state":
            return f"{getattr(record, 'predicate', '')}={getattr(record, 'value', '')}"
        if kind == "relation":
            return str(getattr(record, "predicate", ""))
        if kind == "obligation":
            return f"{getattr(record, 'kind', '')}:{getattr(record, 'description', '')}"
        return str(getattr(record, "event_type", kind))
