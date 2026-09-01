"""U2-B/C: production ModelGateway Writer readout without the creative loop."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import build_engine, build_session_factory
from novel_agent.adapters.postgres.model_call_ledger import SqlModelCallLedger
from novel_agent.adapters.postgres.models import CommitRow
from novel_agent.domain.artifacts import (
    CONTEXT_WRITER_READOUT_RECORD_MEDIA_TYPE,
    QA_WRITER_READOUT_RECORD_MEDIA_TYPE,
    ArtifactRef,
    is_evaluation_artifact_media_type,
)
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, SchemaVersion, StableId
from novel_agent.domain.memory_benchmark import (
    ContextWriterConclusion,
    ContextWriterModelDraft,
    ContextWriterReadoutRecord,
    ContextWriterResponse,
    QaEvidenceItem,
    QaWriterResponse,
)
from novel_agent.domain.model_calls import (
    ModelCallLedgerStatus,
    ModelRequest,
    ModelRole,
    ModelUsage,
    ProviderModelResult,
)
from novel_agent.domain.text import EvidenceRef, EvidenceSupportStatus, QuoteHash, TextSpanRef
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
    ContextAssemblyStatus,
    EvidenceFirstLineage,
    FreezeReceipt,
    WriterContextBudgetReportV2,
    WriterContextPackageV2,
)
from novel_agent.runtime.creative_assembly import build_production_assembly
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.memory_benchmark_contract import build_safe_task_contract
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.writer_context_readout import (
    QA_READOUT_STAGE,
    WriterContextReadoutRequest,
    _readout_model_request,
    bind_production_context_readout,
    bind_production_qa_readout,
    readout_model_request_id,
)
from tests.unit.test_production_assembly import _context, _stamp_sqlite

COMMIT = CommitId("sha256:" + "a" * 64)
PLAN_HASH = ArtifactId("sha256:" + "1" * 64)
EVIDENCE = EvidenceRef(
    evidence_id=StableId("evidence.production-readout"),
    root_hash=ArtifactId("sha256:" + "b" * 64),
    object_hash=ArtifactId("sha256:" + "c" * 64),
    chapter_id=StableId("chapter.100"),
    scene_id=StableId("scene.100"),
    span=TextSpanRef(block_id=StableId("block.100"), start=0, end=1),
    quote_hash=QuoteHash("sha256:" + "d" * 64),
    support_status=EvidenceSupportStatus.CURRENT,
    resolved_at_commit=COMMIT,
)


def _freeze() -> FreezeReceipt:
    digest = ArtifactId("sha256:" + "f" * 64)
    return FreezeReceipt(
        receipt_id=StableId("freeze.production-readout"),
        public_input_hash=digest,
        code_version="test",
        run_config_hash=digest,
        arm_artifact_hashes={"A": digest, "B": digest, "C": digest},
        frozen_before_reveal=True,
    )


def _task(
    *,
    profile: BenchmarkInformationProfile,
    suffix: str = "readout",
) -> BenchmarkTaskContract:
    conditioned = profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
    return build_safe_task_contract(
        case_id=StableId(f"case.{suffix}"),
        checkpoint_chapter=100,
        target_range=(101, 120),
        information_profile=profile,
        task_intent="test" if conditioned else "",
        planning_context_ref=PLAN_HASH if conditioned else None,
        planning_context_hash=PLAN_HASH if conditioned else None,
    )


def _package(task: BenchmarkTaskContract, ledger: ArtifactRef) -> WriterContextPackageV2:
    return WriterContextPackageV2(
        task_contract=task,
        basis_commit_id=COMMIT,
        basis_snapshot_id=StableId("snapshot.production-readout"),
        arm="A",
        items=(),
        gaps=(),
        budget_report=WriterContextBudgetReportV2(
            tokenizer="t",
            tokenizer_version="v",
            configured_writer_token_budget=4000,
            actual_rendered_writer_tokens=0,
            configured_ledger_token_budget=12000,
            actual_rendered_ledger_tokens=0,
            item_count=0,
            evidence_item_count=0,
            gap_item_count=0,
            ledger_entry_count=0,
            final_status=ContextAssemblyStatus.READY,
        ),
        evidence_ledger_ref=ledger,
        lineage=EvidenceFirstLineage(assembler_version="test"),
        rendered_context="陈长生仍在国子监读书。",
    )


def _context_payload() -> str:
    return ContextWriterModelDraft(
        conclusions=(
            ContextWriterConclusion(
                conclusion_id=StableId("conclusion.production-readout"),
                text="陈长生仍在研究修行困境。",
                evidence_refs=(EVIDENCE,),
            ),
        ),
        gaps=(),
        rendered_response="",
    ).model_dump_json()


def _qa_payload() -> str:
    return QaWriterResponse(
        answer="经脉堵塞",
        evidence=(QaEvidenceItem(chapter=20, span="陈长生被诊断出经脉堵塞"),),
    ).model_dump_json()


class _TitleDispatchEndpoint:
    is_external = False

    def __init__(self, payloads: dict[str, str]) -> None:
        self._payloads = payloads
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ProviderModelResult:
        self.requests.append(request)
        schema = request.response_schema or {}
        title = str(schema.get("title") or "")
        text = self._payloads.get(title)
        if text is None:
            raise AssertionError(f"unscripted schema title={title!r}")
        return ProviderModelResult(
            text=text,
            model_version="fake-v1",
            usage=ModelUsage(input_tokens=0, output_tokens=0, cost_usd=Decimal("0")),
        )


class _ParseCrashThenSuccessEndpoint(_TitleDispatchEndpoint):
    def __init__(self, payloads: dict[str, str]) -> None:
        super().__init__(payloads)
        self._attempts: dict[str, int] = {}

    async def generate(self, request: ModelRequest) -> ProviderModelResult:
        schema = request.response_schema or {}
        title = str(schema.get("title") or "")
        attempt = self._attempts.get(title, 0)
        self._attempts[title] = attempt + 1
        if attempt == 0:
            self.requests.append(request)
            return ProviderModelResult(
                text="not-json",
                model_version="fake-v1",
                usage=ModelUsage(input_tokens=0, output_tokens=0, cost_usd=Decimal("0")),
            )
        return await super().generate(request)


def _ledger(tmp_path: Path) -> ArtifactRef:
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "eval-ledger"))
    return artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))


def test_production_context_readout_uses_assembly_gateway_without_commits(
    tmp_path: Path,
) -> None:
    url = _stamp_sqlite(tmp_path / "readout.db")
    endpoint = _TitleDispatchEndpoint({"ContextWriterModelDraft": _context_payload()})
    context = _context(
        tmp_path,
        url=url,
        model_endpoints=(
            RegisteredModelEndpoint(
                role=ModelRole.IMPLEMENTATION,
                endpoint_name="fake-implementation",
                model_name="fake-v1",
                adapter=endpoint,
                revision="fake-v1",
            ),
        ),
    )
    assembly = build_production_assembly(context)
    assert assembly.model_gateway is not None
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    ledger = _ledger(tmp_path)
    probe, writer = bind_production_context_readout(
        assembly.model_gateway,
        artifacts,
        run_id=RunId("run.production-factory"),
    )
    task = _task(profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED)
    response = asyncio.run(
        probe.arun(
            WriterContextReadoutRequest(
                task_contract=task,
                writer_context=_package(task, ledger),
                freeze_receipt=_freeze(),
                case_id=StableId("case.production-readout"),
            )
        )
    )
    assert isinstance(response, ContextWriterResponse)
    assert response.frozen_before_gold_reveal is True
    assert response.task_contract.task_id == task.task_id
    records = assembly.model_gateway.call_records
    assert len(records) == 1
    assert records[0].model_role is ModelRole.IMPLEMENTATION
    assert endpoint.requests[0].scheduling_stage == "benchmark.writer_context_readout"
    ledger_entry = assembly.model_gateway.call_ledger.load(records[0].request_id)
    assert ledger_entry is not None
    assert ledger_entry.raw_artifact_ref is not None
    assert writer.last_response_ref is not None
    assert ledger_entry.raw_artifact_ref.artifact_id != writer.last_response_ref.artifact_id
    assert writer.last_record_ref is not None
    assert writer.last_record_ref.media_type == CONTEXT_WRITER_READOUT_RECORD_MEDIA_TYPE
    assert is_evaluation_artifact_media_type(writer.last_record_ref.media_type)
    assert assembly.session_factory is not None
    with assembly.session_factory() as session:
        commits = session.scalar(select(func.count()).select_from(CommitRow))
    assert commits == 0


def test_production_qa_readout_freezes_track_a_schema(tmp_path: Path) -> None:
    url = _stamp_sqlite(tmp_path / "qa.db")
    endpoint = _TitleDispatchEndpoint({"QaWriterResponse": _qa_payload()})
    context = _context(
        tmp_path,
        url=url,
        model_endpoints=(
            RegisteredModelEndpoint(
                role=ModelRole.IMPLEMENTATION,
                endpoint_name="fake-implementation",
                model_name="fake-v1",
                adapter=endpoint,
                revision="fake-v1",
            ),
        ),
    )
    assembly = build_production_assembly(context)
    assert assembly.model_gateway is not None
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    ledger = _ledger(tmp_path)
    probe, writer = bind_production_qa_readout(
        assembly.model_gateway,
        artifacts,
        run_id=RunId("run.production-factory"),
    )
    task = _task(
        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        suffix="qa",
    )
    response = asyncio.run(
        probe.arun(
            WriterContextReadoutRequest(
                task_contract=task,
                writer_context=_package(task, ledger),
                freeze_receipt=_freeze(),
                case_id=StableId("case.qa-readout"),
                question_id=StableId("ZTJ-C020-Q001"),
                question_text="Chen Changsheng childhood diagnosis",
                track="novelmem_qa",
            )
        )
    )
    assert isinstance(response, QaWriterResponse)
    assert response.answer == "经脉堵塞"
    assert writer.last_record_ref is not None
    assert writer.last_record_ref.media_type == QA_WRITER_READOUT_RECORD_MEDIA_TYPE
    assert endpoint.requests[0].scheduling_stage == "benchmark.writer_qa_readout"


def test_production_readout_request_identity_includes_run_id(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    task = _task(profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF, suffix="identity")
    request = WriterContextReadoutRequest(
        task_contract=task,
        writer_context=_package(task, ledger),
        freeze_receipt=_freeze(),
        case_id=StableId("case.identity"),
        question_id=StableId("ZTJ-C020-Q001"),
        question_text="question",
        track="novelmem_qa",
    )

    first = _readout_model_request(
        request,
        run_id=RunId("run.u4s.first"),
        prompt="prompt",
        stage=QA_READOUT_STAGE,
        max_output_tokens=100,
        timeout_seconds=60.0,
    )
    second = _readout_model_request(
        request,
        run_id=RunId("run.u4s.second"),
        prompt="prompt",
        stage=QA_READOUT_STAGE,
        max_output_tokens=100,
    )

    assert first.request_id == readout_model_request_id(
        run_id=RunId("run.u4s.first"), task_id=task.task_id.root, stage=QA_READOUT_STAGE
    )
    assert first.request_id != second.request_id
    assert first.timeout_seconds == 60.0
    long_context_id = readout_model_request_id(
        run_id=RunId("run.u4s.long-context"),
        task_id="task.v05.context.ZTJ-C020.author-plan-conditioned",
        stage="benchmark.writer_context_readout",
    )
    assert len(long_context_id.root) <= 128


def test_qa_writer_response_schema_is_exported() -> None:
    schema_path = Path(__file__).parents[2] / "schemas" / "stage2" / "QaWriterResponse.schema.json"
    assert json.loads(schema_path.read_text()) == QaWriterResponse.model_json_schema()
    record_path = (
        Path(__file__).parents[2] / "schemas" / "stage2" / "ContextWriterReadoutRecord.schema.json"
    )
    assert json.loads(record_path.read_text()) == ContextWriterReadoutRecord.model_json_schema()


def test_production_context_parse_crash_retries_with_new_audited_request(
    tmp_path: Path,
) -> None:
    url = _stamp_sqlite(tmp_path / "context-retry.db")
    endpoint = _ParseCrashThenSuccessEndpoint({"ContextWriterModelDraft": _context_payload()})
    session_factory = build_session_factory(build_engine(url))
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.IMPLEMENTATION,
                endpoint_name="fake-implementation",
                model_name="fake-v1",
                adapter=endpoint,
            ),
        ),
        structured_max_retries=1,
        call_ledger=SqlModelCallLedger(session_factory),
        raw_artifacts=artifacts,
    )
    ledger = _ledger(tmp_path)
    probe, writer = bind_production_context_readout(
        gateway,
        artifacts,
        run_id=RunId("run.context-retry"),
    )
    task = _task(
        profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED, suffix="context-retry"
    )
    response = asyncio.run(
        probe.arun(
            WriterContextReadoutRequest(
                task_contract=task,
                writer_context=_package(task, ledger),
                freeze_receipt=_freeze(),
                case_id=StableId("case.context-retry"),
            )
        )
    )

    assert isinstance(response, ContextWriterResponse)
    entries = gateway.call_ledger.list_for_run(RunId("run.context-retry"))
    assert len(entries) == 2
    assert {entry.status for entry in entries} == {
        ModelCallLedgerStatus.COMPLETED,
        ModelCallLedgerStatus.VALIDATION_REJECTED,
    }
    assert len({entry.request_id for entry in entries}) == 2
    assert all(entry.raw_artifact_ref is not None for entry in entries)
    assert writer.last_response_ref is not None
    assert writer.last_record_ref is not None
    assert writer.last_response_ref.artifact_id not in {
        entry.raw_artifact_ref.artifact_id
        for entry in entries
        if entry.raw_artifact_ref is not None
    }
    assert writer.last_record_ref.artifact_id != writer.last_response_ref.artifact_id


def test_production_qa_parse_crash_retries_with_new_audited_request(tmp_path: Path) -> None:
    url = _stamp_sqlite(tmp_path / "qa-retry.db")
    endpoint = _ParseCrashThenSuccessEndpoint({"QaWriterResponse": _qa_payload()})
    session_factory = build_session_factory(build_engine(url))
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.IMPLEMENTATION,
                endpoint_name="fake-implementation",
                model_name="fake-v1",
                adapter=endpoint,
            ),
        ),
        structured_max_retries=1,
        call_ledger=SqlModelCallLedger(session_factory),
        raw_artifacts=artifacts,
    )
    ledger = _ledger(tmp_path)
    probe, writer = bind_production_qa_readout(
        gateway,
        artifacts,
        run_id=RunId("run.qa-retry"),
    )
    task = _task(profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF, suffix="qa-retry")
    response = asyncio.run(
        probe.arun(
            WriterContextReadoutRequest(
                task_contract=task,
                writer_context=_package(task, ledger),
                freeze_receipt=_freeze(),
                case_id=StableId("case.qa-retry"),
                question_id=StableId("ZTJ-C020-Q001"),
                question_text="Chen Changsheng childhood diagnosis",
                track="novelmem_qa",
            )
        )
    )

    assert isinstance(response, QaWriterResponse)
    entries = gateway.call_ledger.list_for_run(RunId("run.qa-retry"))
    assert len(entries) == 2
    assert {entry.status for entry in entries} == {
        ModelCallLedgerStatus.COMPLETED,
        ModelCallLedgerStatus.VALIDATION_REJECTED,
    }
    assert len({entry.request_id for entry in entries}) == 2
    assert all(entry.raw_artifact_ref is not None for entry in entries)
    assert writer.last_response_ref is not None
    assert writer.last_record_ref is not None
    assert writer.last_record_ref.artifact_id != writer.last_response_ref.artifact_id


def test_qa_writer_response_rejects_gold_side_channels() -> None:
    payload = QaWriterResponse(
        answer="经脉堵塞",
        evidence=(QaEvidenceItem(chapter=20, span="陈长生被诊断出经脉堵塞"),),
    ).model_dump(mode="json")
    payload["gold_ids"] = ["gold.forbidden"]
    with pytest.raises(ValidationError):
        QaWriterResponse.model_validate(payload)
    payload = QaWriterResponse(
        answer="经脉堵塞",
        evidence=(QaEvidenceItem(chapter=20, span="陈长生被诊断出经脉堵塞"),),
    ).model_dump(mode="json")
    payload["why_needed"] = "evaluator hint"
    with pytest.raises(ValidationError):
        QaWriterResponse.model_validate(payload)
