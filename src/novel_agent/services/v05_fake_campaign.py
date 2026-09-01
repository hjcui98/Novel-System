"""Fake-Writer V0.5 campaign over the existing readout and evaluator owners.

This is not a second benchmark runtime. It enumerates the unique 51/30
identities, calls the production Writer-role readout probes, freeze-gates
the answers through the existing thin adapters, and stores one evaluation
receipt. It does not write Memory/Canon or reveal Gold.
"""

from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING

from novel_agent.domain.artifacts import (
    V05_FAKE_CAMPAIGN_RECEIPT_MEDIA_TYPE,
    V05_READOUT_CAMPAIGN_MANIFEST_MEDIA_TYPE,
    ArtifactRef,
    RootManifest,
)
from novel_agent.domain.ids import ArtifactId, CommitId, RunId, SchemaVersion, StableId
from novel_agent.domain.memory_benchmark import (
    ContextWriterConclusion,
    ContextWriterModelDraft,
    ContextWriterResponse,
    EvidenceLedger,
    QaEvidenceItem,
    QaWriterResponse,
)
from novel_agent.domain.model_calls import EffectiveBudgetResult, ModelCallLedgerEntry, ModelRole
from novel_agent.domain.v05_readout import (
    MemoryIdentitySnapshot,
    V05FakeCampaignReceipt,
    V05FakeCampaignTaskReceipt,
    V05ReadoutCampaignManifest,
    V05ReadoutManifest,
    V05ReadoutTaskIdentity,
    V05ReadoutTrack,
)
from novel_agent.domain.writer_context import (
    BenchmarkInformationProfile,
    BenchmarkTaskContract,
    FreezeReceipt,
    WriterContextPackageV2,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.memory_benchmark_contract import build_safe_task_contract
from novel_agent.services.model_call_ledger import ModelCallLedgerPort
from novel_agent.services.model_gateway import ModelGateway, ModelRoutingError
from novel_agent.services.v05_readout_manifest import validate_v05_seed_identities
from novel_agent.services.writer_context_readout import (
    WriterContextReadoutRequest,
    bind_production_context_readout,
    bind_production_qa_readout,
)
from novel_agent.services.writer_judge import WriterJudgeService
from novel_agent.services.writer_response_evaluation import (
    QaWriterResponseAdapter,
    WriterResponseGoldAdapter,
)

if TYPE_CHECKING:
    from novel_agent.runtime.creative_assembly import ProductionRuntimeAssembly

CAMPAIGN_SCHEMA_VERSION = SchemaVersion("1.0.0")
PLANNING_CONTEXT_HASH = ArtifactId("sha256:" + "1" * 64)


class V05FakeCampaignError(ValueError):
    """The fake campaign cannot freeze, adapt, or retain a complete receipt."""


def v05_fake_writer_payloads(evidence_ledger: EvidenceLedger) -> dict[str, str]:
    """Deterministic Track A/B JSON for the fake Writer endpoint."""

    if not evidence_ledger.entries or not evidence_ledger.entries[0].evidence_refs:
        raise V05FakeCampaignError("fake campaign requires frozen ledger evidence")
    context = ContextWriterModelDraft(
        conclusions=(
            ContextWriterConclusion(
                conclusion_id=StableId("conclusion.v05-fake-campaign"),
                text="陈长生仍受经脉问题约束。",
                evidence_refs=evidence_ledger.entries[0].evidence_refs,
            ),
        ),
        gaps=(),
        rendered_response="",
    )
    qa = QaWriterResponse(
        answer="经脉堵塞",
        evidence=(QaEvidenceItem(chapter=0, span="陈长生被诊断出经脉堵塞"),),
    )
    return {
        "ContextWriterModelDraft": context.model_dump_json(),
        "QaWriterResponse": qa.model_dump_json(),
    }


def task_contract_for_v05_identity(identity: V05ReadoutTaskIdentity) -> BenchmarkTaskContract:
    """Translate a public V0.5 identity into the production task contract."""

    if identity.track is V05ReadoutTrack.CONTEXT:
        if identity.target_chapter_start is None or identity.target_chapter_end is None:
            raise V05FakeCampaignError("Context identity is missing its target window")
        target = (identity.target_chapter_start, identity.target_chapter_end)
    else:
        start = identity.checkpoint_chapter + 1
        target = (start, start)
    conditioned = (
        identity.information_profile is BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
    )
    contract = build_safe_task_contract(
        case_id=identity.task_id,
        checkpoint_chapter=identity.checkpoint_chapter,
        target_range=target,
        information_profile=identity.information_profile,
        task_intent="test" if conditioned else "",
        planning_context_ref=PLANNING_CONTEXT_HASH if conditioned else None,
        planning_context_hash=PLANNING_CONTEXT_HASH if conditioned else None,
    )
    return contract.model_copy(update={"task_id": identity.task_id})


def locate_campaign_model_call(
    receipt: V05FakeCampaignReceipt,
    ledger: ModelCallLedgerPort,
    *,
    campaign_id: StableId,
    run_id: RunId,
    checkpoint_chapter: int,
    information_profile: BenchmarkInformationProfile,
    phase: str,
    question_id: StableId | None = None,
    task_id: StableId | None = None,
) -> ModelCallLedgerEntry:
    """Locate one Writer readout request from campaign identity dimensions."""

    if receipt.campaign_id != campaign_id or receipt.run_id != run_id:
        raise V05FakeCampaignError("campaign/run identity does not match the receipt")
    matches = [
        task
        for task in receipt.tasks
        if task.identity.checkpoint_chapter == checkpoint_chapter
        and task.identity.information_profile is information_profile
        and (question_id is None or task.identity.question_id == question_id)
        and (task_id is None or task.identity.task_id == task_id)
    ]
    if len(matches) != 1:
        raise V05FakeCampaignError("campaign model call identity is not unique")
    entry = ledger.load(matches[0].model_request_id)
    if entry is None or entry.run_id != run_id:
        raise V05FakeCampaignError("located request is absent from the model-call ledger")
    if entry.logical_phase != phase:
        raise V05FakeCampaignError("located request phase does not match")
    return entry


def memory_identity_from_manifest(
    commit_id: CommitId,
    manifest: RootManifest,
) -> MemoryIdentitySnapshot:
    return MemoryIdentitySnapshot(
        commit_id=commit_id,
        text_root=manifest.text_root.artifact_id,
        world_root=manifest.world_root.artifact_id,
        plan_root=manifest.plan_root.artifact_id,
        profile_root=manifest.project_profile_root.artifact_id,
    )


class V05FakeCampaignRunner:
    """Loop unique V0.5 identities through production readout + thin adapters."""

    def __init__(
        self,
        gateway: ModelGateway,
        artifacts: ArtifactRepository,
        *,
        run_id: RunId,
        default_output_tokens: int = 8000,
    ) -> None:
        if default_output_tokens < 1:
            raise ValueError("default Writer output tokens must be positive")
        self._gateway = gateway
        self._artifacts = artifacts
        self._run_id = run_id
        self._default_output_tokens = default_output_tokens
        self.last_receipt_ref: ArtifactRef | None = None
        self.last_campaign_manifest_ref: ArtifactRef | None = None

    @classmethod
    def from_production_assembly(
        cls,
        assembly: ProductionRuntimeAssembly,
        *,
        run_id: RunId,
    ) -> V05FakeCampaignRunner:
        """Bind the readout runner to the assembly's single gateway and artifact owner."""

        gateway = assembly.model_gateway
        artifacts = assembly.artifacts
        if gateway is None or artifacts is None:
            raise V05FakeCampaignError(
                "production assembly does not expose its ModelGateway and artifacts"
            )
        if gateway.raw_artifacts is not artifacts:
            raise V05FakeCampaignError(
                "production readout must reuse the assembly ModelGateway artifact repository"
            )
        if assembly.attestation is None:
            raise V05FakeCampaignError(
                "production assembly readout requires a resolved budget attestation"
            )
        return cls(
            gateway,
            artifacts,
            run_id=run_id,
            default_output_tokens=assembly.attestation.output_limit,
        )

    async def run(
        self,
        *,
        manifest: V05ReadoutManifest | V05ReadoutCampaignManifest,
        freeze_receipt: FreezeReceipt,
        evidence_ledger: EvidenceLedger,
        writer_context: WriterContextPackageV2,
        campaign_id: StableId | None = None,
    ) -> V05FakeCampaignReceipt:
        self.last_receipt_ref = None
        self.last_campaign_manifest_ref = None
        if isinstance(manifest, V05ReadoutCampaignManifest):
            campaign_manifest: V05ReadoutCampaignManifest | None = manifest
            readout_manifest = manifest.readout_manifest
        else:
            campaign_manifest = None
            readout_manifest = manifest
        campaign_effective_budget: EffectiveBudgetResult | None = None
        if not freeze_receipt.frozen_before_reveal:
            raise V05FakeCampaignError("campaign must freeze before Gold reveal")
        validate_v05_seed_identities(readout_manifest)
        if campaign_manifest is not None:
            if campaign_id is not None and campaign_id != campaign_manifest.campaign_id:
                raise V05FakeCampaignError("campaign id does not match the frozen manifest")
            if campaign_manifest.canary_lock is None:
                raise V05FakeCampaignError("campaign manifest is missing the frozen canary lock")
            campaign_id = campaign_manifest.campaign_id
            if campaign_manifest.execution.repetitions != 1:
                raise V05FakeCampaignError(
                    "the V0.5 readout runner supports exactly one frozen repetition"
                )
            worst_case_calls = len(readout_manifest.tasks) * (
                1 + self._gateway.structured_max_retries
            )
            if worst_case_calls > campaign_manifest.execution.max_model_calls:
                raise V05FakeCampaignError(
                    "campaign max_model_calls is smaller than the worst-case structured call budget"
                )
            try:
                endpoint_name, model_name, revision = self._gateway.endpoint_runtime_identity(
                    ModelRole.IMPLEMENTATION
                )
            except ModelRoutingError as error:
                raise V05FakeCampaignError(
                    "campaign Writer endpoint identity is unavailable"
                ) from error
            if (
                endpoint_name != campaign_manifest.writer.endpoint
                or model_name != campaign_manifest.writer.model
                or revision != campaign_manifest.writer.revision
            ):
                raise V05FakeCampaignError(
                    "campaign Writer endpoint/model/revision does not match the production gateway"
                )
            limits = self._gateway.endpoint_budget_limits(ModelRole.IMPLEMENTATION)
            campaign_effective_budget = campaign_manifest.runtime.effective_budget
            if campaign_effective_budget is None:
                raise V05FakeCampaignError(
                    "campaign runtime is missing the resolved EffectiveBudgetResult"
                )
            if campaign_effective_budget.context_limit != limits.sequence_limit:
                raise V05FakeCampaignError(
                    "campaign context limit does not match the production endpoint sequence limit"
                )
            if limits.output_limit is not None and (
                campaign_effective_budget.total_output_budget > limits.output_limit
            ):
                raise V05FakeCampaignError(
                    "campaign provider total output reserve exceeds the registered endpoint limit"
                )
            if (
                limits.safety_allowance_tokens is not None
                and campaign_manifest.runtime.safety_allowance_tokens
                != limits.safety_allowance_tokens
            ):
                raise V05FakeCampaignError(
                    "campaign safety allowance does not match the production endpoint policy"
                )
            if (
                campaign_manifest.runtime.provider_reasoning_included_in_completion_tokens
                != limits.reasoning_included_in_completion_tokens
            ):
                raise V05FakeCampaignError(
                    "campaign reasoning billing policy does not match the production endpoint"
                )
            ledger_before_freeze = tuple(
                entry.request_id for entry in self._gateway.call_ledger.list_for_run(self._run_id)
            )
            self.last_campaign_manifest_ref = self._artifacts.put(
                canonical_json_bytes(campaign_manifest.model_dump(mode="json")),
                V05_READOUT_CAMPAIGN_MANIFEST_MEDIA_TYPE,
                CAMPAIGN_SCHEMA_VERSION,
            )
            ledger_after_freeze = tuple(
                entry.request_id for entry in self._gateway.call_ledger.list_for_run(self._run_id)
            )
            if ledger_after_freeze != ledger_before_freeze:
                raise V05FakeCampaignError(
                    "freezing the campaign manifest must not create model-call ledger requests"
                )
        else:
            self.last_campaign_manifest_ref = None
        campaign_deadline = (
            monotonic() + campaign_manifest.execution.max_wall_time_seconds
            if campaign_manifest is not None
            else None
        )
        context_probe, context_writer = bind_production_context_readout(
            self._gateway,
            self._artifacts,
            run_id=self._run_id,
            max_output_tokens=(
                campaign_manifest.runtime.body_output_tokens
                if campaign_manifest is not None
                else self._default_output_tokens
            ),
            enable_thinking=(
                campaign_manifest.runtime.thinking_enabled
                if campaign_manifest is not None
                else None
            ),
            thinking_token_budget=(
                campaign_effective_budget.thinking_budget
                if campaign_manifest is not None
                and campaign_effective_budget is not None
                and campaign_manifest.runtime.thinking_enabled
                else None
            ),
        )
        qa_probe, qa_writer = bind_production_qa_readout(
            self._gateway,
            self._artifacts,
            run_id=self._run_id,
            max_output_tokens=(
                campaign_manifest.runtime.body_output_tokens
                if campaign_manifest is not None
                else self._default_output_tokens
            ),
            enable_thinking=(
                campaign_manifest.runtime.thinking_enabled
                if campaign_manifest is not None
                else None
            ),
            thinking_token_budget=(
                campaign_effective_budget.thinking_budget
                if campaign_manifest is not None
                and campaign_effective_budget is not None
                and campaign_manifest.runtime.thinking_enabled
                else None
            ),
        )
        qa_adapter = QaWriterResponseAdapter()
        context_adapter = WriterResponseGoldAdapter()
        judges = WriterJudgeService(self._artifacts)
        tasks: list[V05FakeCampaignTaskReceipt] = []
        for identity in readout_manifest.tasks:
            if campaign_deadline is not None and monotonic() >= campaign_deadline:
                raise V05FakeCampaignError("campaign max_wall_time_seconds was exhausted")
            task_contract = task_contract_for_v05_identity(identity)
            request = WriterContextReadoutRequest(
                task_contract=task_contract,
                writer_context=writer_context.model_copy(update={"task_contract": task_contract}),
                freeze_receipt=freeze_receipt,
                case_id=identity.task_id,
                question_id=identity.question_id,
                question_text=None if identity.question_id is None else identity.question_id.root,
                track=identity.track.value,
            )
            if identity.track is V05ReadoutTrack.QA:
                response = await qa_probe.arun(request)
                if not isinstance(response, QaWriterResponse):
                    raise V05FakeCampaignError("QA campaign task did not return QaWriterResponse")
                qa_adapter.adapt(
                    response=response,
                    freeze_receipt=freeze_receipt,
                    checkpoint_chapter=identity.checkpoint_chapter,
                )
                response_ref = qa_writer.last_response_ref
                record_ref = qa_writer.last_record_ref
            else:
                response = await context_probe.arun(request)
                if not isinstance(response, ContextWriterResponse):
                    raise V05FakeCampaignError("Context campaign task did not return a freeze")
                context_adapter.writer_ledger(
                    response=response,
                    frozen_ledger=evidence_ledger,
                    freeze_receipt=freeze_receipt,
                )
                response_ref = context_writer.last_response_ref
                record_ref = context_writer.last_record_ref
            if response_ref is None or record_ref is None:
                raise V05FakeCampaignError("campaign readout did not retain evaluation artifacts")
            request_id, raw_ref = self._request_and_raw(identity)
            pair = judges.pending_pair(
                run_id=self._run_id,
                task_id=identity.task_id,
                freeze_receipt_id=freeze_receipt.receipt_id,
                response_ref=response_ref,
            )
            judges.persist(pair.answer_judge)
            judges.persist(pair.evidence_support_judge)
            tasks.append(
                V05FakeCampaignTaskReceipt(
                    identity=identity,
                    freeze_receipt_id=freeze_receipt.receipt_id,
                    model_request_id=request_id,
                    response_ref=response_ref,
                    record_ref=record_ref,
                    raw_artifact_ref=raw_ref,
                    judges=pair,
                    canary_lock=(
                        campaign_manifest.canary_lock if campaign_manifest is not None else None
                    ),
                )
            )
        qa_count = sum(1 for task in tasks if task.identity.track is V05ReadoutTrack.QA)
        context_count = len(tasks) - qa_count
        receipt = V05FakeCampaignReceipt(
            campaign_id=campaign_id or StableId("campaign.v05.fake"),
            run_id=self._run_id,
            freeze_receipt_id=freeze_receipt.receipt_id,
            campaign_manifest_id=(
                campaign_manifest.campaign_id if campaign_manifest is not None else None
            ),
            campaign_manifest_ref=self.last_campaign_manifest_ref,
            canary_lock=(campaign_manifest.canary_lock if campaign_manifest is not None else None),
            qa_count=qa_count,
            context_count=context_count,
            tasks=tuple(tasks),
        )
        self.last_receipt_ref = self._artifacts.put(
            canonical_json_bytes(receipt.model_dump(mode="json")),
            V05_FAKE_CAMPAIGN_RECEIPT_MEDIA_TYPE,
            CAMPAIGN_SCHEMA_VERSION,
        )
        return receipt

    def _request_and_raw(
        self,
        identity: V05ReadoutTaskIdentity,
    ) -> tuple[StableId, ArtifactRef | None]:
        matching = tuple(
            entry
            for entry in self._gateway.call_ledger.list_for_run(self._run_id)
            if entry.task_id.root == identity.task_id.root
        )
        if not matching:
            raise V05FakeCampaignError("campaign task is absent from the model-call ledger")
        entry = matching[-1]
        if self._gateway.raw_artifacts is not None and entry.raw_artifact_ref is None:
            raise V05FakeCampaignError("campaign task is missing the retained raw artifact")
        return entry.request_id, entry.raw_artifact_ref
