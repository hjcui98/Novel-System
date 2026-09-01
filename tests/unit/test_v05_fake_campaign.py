"""U2 Gate remainder: fake Writer 51 QA / 30 Context campaign receipt."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.domain.artifacts import (
    V05_FAKE_CAMPAIGN_RECEIPT_MEDIA_TYPE,
    ArtifactRef,
    is_evaluation_artifact_media_type,
)
from novel_agent.domain.ids import CommitId, RunId, SchemaVersion, StableId
from novel_agent.domain.memory_write import CanonicalWriteBasis, SourceProvenance
from novel_agent.domain.model_calls import ModelCallLedgerStatus, ModelRole
from novel_agent.domain.v05_readout import (
    V05FakeCampaignReceipt,
    V05HistoryAccess,
    V05ReadoutManifest,
    V05ReadoutTaskIdentity,
    V05ReadoutTrack,
    WriterJudgeAvailability,
)
from novel_agent.domain.writer_context import BenchmarkInformationProfile
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.benchmark_scenario_compiler import BenchmarkScenarioCompiler
from novel_agent.services.information_boundary import (
    InformationBoundaryPort,
    InformationBoundaryViolation,
)
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.services.v05_fake_campaign import (
    V05FakeCampaignError,
    V05FakeCampaignRunner,
    locate_campaign_model_call,
    task_contract_for_v05_identity,
    v05_fake_writer_payloads,
)
from novel_agent.services.v05_readout_manifest import EXPECTED_CONTEXT_TASKS, EXPECTED_QA_TASKS
from tests.contract.test_memory_write_workflow_contract import PROJECT, _manifest, _request
from tests.fixtures.stage2_memory_benchmark import frozen_evaluation_inputs
from tests.unit.test_information_boundary import TRUSTED_POLICY, _visibility
from tests.unit.test_production_writer_readout import _package, _task, _TitleDispatchEndpoint
from tests.unit.test_v05_readout_manifest import CONTEXT_WINDOWS, _checkpoints, _questions


def _identities() -> V05ReadoutManifest:
    return BenchmarkScenarioCompiler().compile_v05_readout_identities(
        benchmark_id="novelmem-eval-ztj",
        version="0.5-seed.2",
        checkpoints=_checkpoints(),
        qa_questions=_questions(),
        context_windows=CONTEXT_WINDOWS,
    )


def _reject_memory_write(artifact: ArtifactRef) -> None:
    request = _request()
    visibility = _visibility(
        artifact,
        boundary_id=request.information_boundary.boundary_id,
    )
    tainted = request.model_copy(
        update={
            "source_artifacts": (artifact,),
            "source_visibility_receipts": (visibility,),
            "source_provenance": (SourceProvenance.REVEALED_TEXT,),
        }
    )
    basis = CanonicalWriteBasis(
        project_id=PROJECT,
        commit_id=CommitId("sha256:" + "1" * 64),
        root_manifest=_manifest(),
    )
    with pytest.raises(InformationBoundaryViolation, match="evaluation"):
        InformationBoundaryPort(
            trusted_policy_hashes=(TRUSTED_POLICY,)
        ).verify_request_and_derivation_graph(tainted, basis)


def test_fake_campaign_freezes_unique_51_30_identities_without_writeback(
    tmp_path: Path,
) -> None:
    identities = _identities()
    _gold, _package_v1, ledger, freeze = frozen_evaluation_inputs()
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    ledger_ref = artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0"))
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
    runner = V05FakeCampaignRunner(
        gateway,
        artifacts,
        run_id=RunId("run.v05-fake-campaign"),
    )
    receipt = asyncio.run(
        runner.run(
            manifest=identities,
            freeze_receipt=freeze,
            evidence_ledger=ledger,
            writer_context=_package(
                _task(
                    profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
                    suffix="campaign",
                ),
                ledger_ref,
            ),
        )
    )
    qa = tuple(task for task in receipt.tasks if task.identity.track is V05ReadoutTrack.QA)
    context = tuple(
        task for task in receipt.tasks if task.identity.track is V05ReadoutTrack.CONTEXT
    )
    assert receipt.qa_count == EXPECTED_QA_TASKS == 51
    assert receipt.context_count == EXPECTED_CONTEXT_TASKS == 30
    assert len(receipt.tasks) == 81
    assert len({task.identity.task_id for task in receipt.tasks}) == 81
    assert not any(task.identity.checkpoint_chapter == 100 for task in qa)
    assert not any(task.identity.checkpoint_chapter == 300 for task in context)
    assert {task.identity.history_access for task in context} == {
        V05HistoryAccess.HISTORY_ONLY,
        V05HistoryAccess.AUTHOR_PLAN_CONDITIONED,
    }
    assert all(task.evaluator_adapted and not task.gold_revealed for task in receipt.tasks)
    assert all(task.freeze_receipt_id == freeze.receipt_id for task in receipt.tasks)
    entries = gateway.call_ledger.list_for_run(RunId("run.v05-fake-campaign"))
    assert len(entries) == 81
    assert len({entry.request_id for entry in entries}) == 81
    assert {entry.status for entry in entries} == {ModelCallLedgerStatus.COMPLETED}
    assert {entry.logical_phase for entry in entries} == {
        "benchmark.writer_qa_readout",
        "benchmark.writer_context_readout",
    }
    assert runner.last_receipt_ref is not None
    assert runner.last_receipt_ref.media_type == V05_FAKE_CAMPAIGN_RECEIPT_MEDIA_TYPE
    assert is_evaluation_artifact_media_type(runner.last_receipt_ref.media_type)
    payload = json.loads(artifacts.read_verified(runner.last_receipt_ref))
    assert payload["qa_count"] == 51
    assert payload["context_count"] == 30
    _reject_memory_write(runner.last_receipt_ref)
    _reject_memory_write(receipt.tasks[0].response_ref)
    _reject_memory_write(context[0].response_ref)
    assert all(
        task.judges.answer_judge.availability is WriterJudgeAvailability.PENDING
        and task.judges.evidence_support_judge.availability is WriterJudgeAvailability.PENDING
        and task.judges.answer_judge.score is None
        and task.judges.evidence_support_judge.score is None
        for task in receipt.tasks
    )
    qa_c20 = next(task for task in qa if task.identity.checkpoint_chapter == 20)
    located_qa = locate_campaign_model_call(
        receipt,
        gateway.call_ledger,
        campaign_id=receipt.campaign_id,
        run_id=receipt.run_id,
        checkpoint_chapter=20,
        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
        phase="benchmark.writer_qa_readout",
        question_id=qa_c20.identity.question_id,
    )
    assert located_qa.request_id == qa_c20.model_request_id
    context_c20_apc = next(
        task
        for task in context
        if task.identity.checkpoint_chapter == 20
        and task.identity.information_profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
    )
    located_context = locate_campaign_model_call(
        receipt,
        gateway.call_ledger,
        campaign_id=receipt.campaign_id,
        run_id=receipt.run_id,
        checkpoint_chapter=20,
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        phase="benchmark.writer_context_readout",
    )
    assert located_context.request_id == context_c20_apc.model_request_id
    assert qa_c20.raw_artifact_ref is not None
    assert (
        len(
            {
                qa_c20.raw_artifact_ref.artifact_id,
                qa_c20.response_ref.artifact_id,
                qa_c20.record_ref.artifact_id,
            }
        )
        == 3
    )
    with pytest.raises(V05FakeCampaignError, match="not unique"):
        locate_campaign_model_call(
            receipt,
            gateway.call_ledger,
            campaign_id=receipt.campaign_id,
            run_id=receipt.run_id,
            checkpoint_chapter=20,
            information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            phase="benchmark.writer_qa_readout",
        )


def test_v05_fake_campaign_receipt_schema_is_exported() -> None:
    schema_path = (
        Path(__file__).parents[2] / "schemas" / "stage2" / "V05FakeCampaignReceipt.schema.json"
    )
    assert json.loads(schema_path.read_text()) == V05FakeCampaignReceipt.model_json_schema()


def test_fake_campaign_helpers_fail_closed(tmp_path: Path) -> None:
    _gold, _package_v1, ledger, freeze = frozen_evaluation_inputs()
    empty_entry = ledger.entries[0].model_copy(update={"evidence_refs": ()})
    with pytest.raises(V05FakeCampaignError, match="frozen ledger evidence"):
        v05_fake_writer_payloads(ledger.model_copy(update={"entries": (empty_entry,)}))
    context_identity = V05ReadoutTaskIdentity(
        task_id=StableId("task.v05.context.missing"),
        track=V05ReadoutTrack.CONTEXT,
        checkpoint_id=StableId("checkpoint.v05"),
        checkpoint_chapter=20,
        history_access=V05HistoryAccess.AUTHOR_PLAN_CONDITIONED,
        information_profile=BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        target_chapter_start=21,
        target_chapter_end=25,
    )
    contract = task_contract_for_v05_identity(context_identity)
    assert contract.task_id == context_identity.task_id
    data = context_identity.model_dump()
    data["target_chapter_start"] = None
    data["target_chapter_end"] = None
    missing_window = V05ReadoutTaskIdentity.model_construct(**data)
    with pytest.raises(V05FakeCampaignError, match="missing its target window"):
        task_contract_for_v05_identity(missing_window)

    identities = _identities()
    receipt = V05FakeCampaignReceipt.model_construct(
        campaign_id=StableId("campaign.mismatch"),
        run_id=RunId("run.mismatch"),
        freeze_receipt_id=freeze.receipt_id,
        qa_count=0,
        context_count=0,
        tasks=(),
    )
    with pytest.raises(V05FakeCampaignError, match="campaign/run identity"):
        locate_campaign_model_call(
            receipt,
            type("Ledger", (), {"load": staticmethod(lambda _id: None)})(),
            campaign_id=StableId("campaign.other"),
            run_id=RunId("run.mismatch"),
            checkpoint_chapter=20,
            information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            phase="benchmark.writer_qa_readout",
        )
    unfrozen = freeze.model_copy(update={"frozen_before_reveal": False})
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "unused"))
    with pytest.raises(V05FakeCampaignError, match="freeze before Gold"):
        asyncio.run(
            V05FakeCampaignRunner(
                object(),  # type: ignore[arg-type]
                artifacts,
                run_id=RunId("run.unfrozen"),
            ).run(
                manifest=identities,
                freeze_receipt=unfrozen,
                evidence_ledger=ledger,
                writer_context=_package(
                    _task(
                        profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
                        suffix="unfrozen",
                    ),
                    artifacts.put(b"{}", "application/json", SchemaVersion("1.0.0")),
                ),
            )
        )


def test_locate_campaign_model_call_requires_ledger_entry_and_phase() -> None:
    receipt = V05FakeCampaignReceipt.model_construct(
        campaign_id=StableId("campaign.locate"),
        run_id=RunId("run.locate"),
        freeze_receipt_id=StableId("freeze.locate"),
        qa_count=1,
        context_count=0,
        tasks=(
            type(
                "Task",
                (),
                {
                    "identity": V05ReadoutTaskIdentity(
                        task_id=StableId("task.v05.qa.locate"),
                        track=V05ReadoutTrack.QA,
                        checkpoint_id=StableId("checkpoint.v05"),
                        checkpoint_chapter=20,
                        history_access=V05HistoryAccess.HISTORY_ONLY,
                        information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
                        question_id=StableId("question.locate"),
                        question_release="after_checkpoint_freeze",
                    ),
                    "model_request_id": StableId("request.locate"),
                },
            )(),
        ),
    )
    with pytest.raises(V05FakeCampaignError, match="absent from the model-call ledger"):
        locate_campaign_model_call(
            receipt,
            type("Ledger", (), {"load": staticmethod(lambda _id: None)})(),
            campaign_id=receipt.campaign_id,
            run_id=receipt.run_id,
            checkpoint_chapter=20,
            information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            phase="benchmark.writer_qa_readout",
            question_id=StableId("question.locate"),
        )
    wrong_phase = type(
        "Entry",
        (),
        {"run_id": receipt.run_id, "logical_phase": "benchmark.writer_context_readout"},
    )()
    with pytest.raises(V05FakeCampaignError, match="phase does not match"):
        locate_campaign_model_call(
            receipt,
            type("Ledger", (), {"load": staticmethod(lambda _id: wrong_phase)})(),
            campaign_id=receipt.campaign_id,
            run_id=receipt.run_id,
            checkpoint_chapter=20,
            information_profile=BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
            phase="benchmark.writer_qa_readout",
            question_id=StableId("question.locate"),
        )
