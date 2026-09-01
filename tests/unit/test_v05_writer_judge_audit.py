"""U3-D: judge receipts, pending≠0, ledger locate, evaluation discard."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.domain.artifacts import (
    V05_FAKE_CAMPAIGN_RECEIPT_MEDIA_TYPE,
    WRITER_JUDGE_INPUT_MEDIA_TYPE,
    WRITER_JUDGE_OUTPUT_MEDIA_TYPE,
    ArtifactRef,
)
from novel_agent.domain.ids import ArtifactId, RunId, SchemaVersion, StableId
from novel_agent.domain.memory_benchmark import ContextWriterResponse
from novel_agent.domain.model_calls import ModelRole
from novel_agent.domain.v05_readout import (
    WriterJudgeAvailability,
    WriterJudgeKind,
    WriterJudgeReceipt,
)
from novel_agent.domain.writer_context import BenchmarkInformationProfile
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.commits import CommitService
from novel_agent.services.evaluation_namespace import discard_evaluation_namespace
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.v05_fake_campaign import (
    memory_identity_from_manifest,
    v05_fake_writer_payloads,
)
from novel_agent.services.writer_context_readout import (
    WriterContextReadoutRequest,
    bind_production_context_readout,
)
from novel_agent.services.writer_judge import WriterJudgeError, WriterJudgeService
from tests.factories import make_commit_request, make_manifest
from tests.fixtures.stage2_memory_benchmark import frozen_evaluation_inputs
from tests.unit.test_production_writer_readout import _package, _task, _TitleDispatchEndpoint


def _ref() -> ArtifactRef:
    return ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "a" * 64),
        media_type="application/vnd.novel-agent.evaluation.qa-writer-response+json",
        byte_length=1,
        schema_version=SchemaVersion("1.0.0"),
    )


def test_pending_judge_rejects_a_zero_score() -> None:
    with pytest.raises(ValidationError, match="cannot carry a score"):
        WriterJudgeReceipt(
            receipt_id=StableId("writer-judge.answer.pending"),
            kind=WriterJudgeKind.ANSWER,
            availability=WriterJudgeAvailability.PENDING,
            logical_phase="benchmark.answer_judge",
            run_id=RunId("run.judge-pending"),
            task_id=StableId("task.judge-pending"),
            freeze_receipt_id=StableId("freeze.judge-pending"),
            response_ref=_ref(),
            score=0.0,
        )


def test_evidence_support_completes_while_answer_judge_stays_pending(tmp_path: Path) -> None:
    gold, _package_v1, ledger, freeze = frozen_evaluation_inputs()
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    endpoint = _TitleDispatchEndpoint(v05_fake_writer_payloads(ledger))
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.IMPLEMENTATION,
                endpoint_name="fake-implementation",
                model_name="fake-v1",
                adapter=endpoint,
            ),
        ),
        raw_artifacts=artifacts,
    )
    probe, writer = bind_production_context_readout(
        gateway,
        artifacts,
        run_id=RunId("run.judge-evidence"),
    )
    task = _task(profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED, suffix="judge")
    ledger_ref = artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))
    response = asyncio.run(
        probe.arun(
            WriterContextReadoutRequest(
                task_contract=task,
                writer_context=_package(task, ledger_ref),
                freeze_receipt=freeze,
                case_id=StableId("case.judge-evidence"),
            )
        )
    )
    assert writer.last_response_ref is not None
    assert isinstance(response, ContextWriterResponse)
    service = WriterJudgeService(artifacts)
    pending = service.pending_pair(
        run_id=RunId("run.judge-evidence"),
        task_id=task.task_id,
        freeze_receipt_id=freeze.receipt_id,
        response_ref=writer.last_response_ref,
    )
    completed = service.complete_evidence_support(
        pending,
        response=response,
        frozen_ledger=ledger,
        freeze_receipt=freeze,
        gold=gold,
    )
    assert completed.answer_judge.availability is WriterJudgeAvailability.PENDING
    assert completed.answer_judge.score is None
    assert completed.evidence_support_judge.availability is WriterJudgeAvailability.AVAILABLE
    assert completed.evidence_support_judge.score in {0.0, 1.0}
    assert completed.evidence_support_judge.input_artifact_ref is not None
    assert completed.evidence_support_judge.output_artifact_ref is not None
    assert (
        completed.evidence_support_judge.input_artifact_ref.media_type
        == WRITER_JUDGE_INPUT_MEDIA_TYPE
    )
    assert (
        completed.evidence_support_judge.output_artifact_ref.media_type
        == WRITER_JUDGE_OUTPUT_MEDIA_TYPE
    )
    ids = {
        writer.last_response_ref.artifact_id,
        completed.evidence_support_judge.input_artifact_ref.artifact_id,
        completed.evidence_support_judge.output_artifact_ref.artifact_id,
    }
    if writer.last_record_ref is not None:
        ids.add(writer.last_record_ref.artifact_id)
    entry = gateway.call_ledger.list_for_run(RunId("run.judge-evidence"))[0]
    if entry.raw_artifact_ref is not None:
        ids.add(entry.raw_artifact_ref.artifact_id)
    assert len(ids) >= 4
    available = service.record_available_pair(
        run_id=RunId("run.judge-evidence"),
        task_id=task.task_id,
        freeze_receipt_id=freeze.receipt_id,
        response_ref=writer.last_response_ref,
        answer_input_ref=_ref(),
        answer_output_ref=_ref(),
        answer_score=0.5,
        evidence_input_ref=_ref(),
        evidence_output_ref=_ref(),
        evidence_score=0.5,
        answer_model_request_id=StableId("model.answer-judge.available"),
        evidence_model_request_id=StableId("model.evidence-judge.available"),
    )
    with pytest.raises(WriterJudgeError, match="already available"):
        service.complete_evidence_support(
            available,
            response=response,
            frozen_ledger=ledger,
            freeze_receipt=freeze,
            gold=gold,
        )


def test_complete_evidence_support_rejects_colliding_artifacts(tmp_path: Path) -> None:
    gold, _package_v1, ledger, freeze = frozen_evaluation_inputs()
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    endpoint = _TitleDispatchEndpoint(v05_fake_writer_payloads(ledger))
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.IMPLEMENTATION,
                endpoint_name="fake-implementation",
                model_name="fake-v1",
                adapter=endpoint,
            ),
        ),
        raw_artifacts=artifacts,
    )
    probe, writer = bind_production_context_readout(
        gateway,
        artifacts,
        run_id=RunId("run.judge-collision"),
    )
    task = _task(profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED, suffix="collision")
    ledger_ref = artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))
    response = asyncio.run(
        probe.arun(
            WriterContextReadoutRequest(
                task_contract=task,
                writer_context=_package(task, ledger_ref),
                freeze_receipt=freeze,
                case_id=StableId("case.judge-collision"),
            )
        )
    )
    assert writer.last_response_ref is not None
    assert isinstance(response, ContextWriterResponse)
    service = WriterJudgeService(artifacts)
    shared_ref = ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "a" * 64),
        media_type="application/vnd.novel-agent.evaluation.qa-writer-response+json",
        byte_length=1,
        schema_version=SchemaVersion("1.0.0"),
    )
    output_ref = ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "b" * 64),
        media_type=WRITER_JUDGE_OUTPUT_MEDIA_TYPE,
        byte_length=1,
        schema_version=SchemaVersion("1.0.0"),
    )
    cast(Any, artifacts).put = Mock(return_value=shared_ref)
    pending = service.pending_pair(
        run_id=RunId("run.judge-collision"),
        task_id=task.task_id,
        freeze_receipt_id=freeze.receipt_id,
        response_ref=writer.last_response_ref,
    )
    with pytest.raises(WriterJudgeError, match="must be distinct artifacts"):
        service.complete_evidence_support(
            pending,
            response=response,
            frozen_ledger=ledger,
            freeze_receipt=freeze,
            gold=gold,
        )

    cast(Any, artifacts).put = Mock(side_effect=[shared_ref, output_ref])
    pending_writer_collision = service.pending_pair(
        run_id=RunId("run.judge-collision"),
        task_id=StableId("task.judge-collision.writer"),
        freeze_receipt_id=freeze.receipt_id,
        response_ref=output_ref,
    )
    with pytest.raises(WriterJudgeError, match="distinct from the Writer answer"):
        service.complete_evidence_support(
            pending_writer_collision,
            response=response,
            frozen_ledger=ledger,
            freeze_receipt=freeze,
            gold=gold,
        )


def test_discarding_evaluation_namespace_leaves_memory_identity_unchanged(
    tmp_path: Path,
) -> None:
    first_engine = create_engine("sqlite+pysqlite:///:memory:")
    second_engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(first_engine)
    Base.metadata.create_all(second_engine)
    evaluated = CommitService(build_session_factory(first_engine))
    control = CommitService(build_session_factory(second_engine))
    genesis = evaluated.initialize_project(make_manifest())
    control_genesis = control.initialize_project(make_manifest())
    first = evaluated.commit(make_commit_request(genesis))
    control_first = control.commit(make_commit_request(control_genesis))
    assert first.commit_id is not None
    assert control_first.commit_id == first.commit_id
    before = memory_identity_from_manifest(
        first.commit_id,
        evaluated.load_manifest(first.commit_id),
    )
    store = ArtifactRepository(FilesystemObjectStore(tmp_path / "eval"))
    eval_ref = store.put(
        b'{"campaign":"discard"}',
        V05_FAKE_CAMPAIGN_RECEIPT_MEDIA_TYPE,
        SchemaVersion("1.0.0"),
    )
    after = memory_identity_from_manifest(
        evaluated.current_commit(make_manifest().project_id),
        evaluated.load_manifest(first.commit_id),
    )
    discard = discard_evaluation_namespace(
        store,
        run_id=RunId("run.discard-eval"),
        discarded_refs=(eval_ref,),
        memory_before=before,
        memory_after=after,
    )
    assert discard.memory_identity_before == discard.memory_identity_after == before
    assert control_first.commit_id is not None
    next_evaluated = evaluated.commit(
        make_commit_request(first.commit_id, idempotency_key="commit.key.2", root_offset=10)
    )
    next_control = control.commit(
        make_commit_request(
            control_first.commit_id,
            idempotency_key="commit.key.2",
            root_offset=10,
        )
    )
    assert next_evaluated.commit_id == next_control.commit_id


def test_writer_judge_receipt_schema_is_exported() -> None:
    schema_path = (
        Path(__file__).parents[2] / "schemas" / "stage2" / "WriterJudgeReceipt.schema.json"
    )
    assert json.loads(schema_path.read_text()) == WriterJudgeReceipt.model_json_schema()
