"""High-level Stage 2 Memory Gateway with frozen output and deterministic fallback."""

from __future__ import annotations

from novel_agent.domain.benchmark import TextRootDocument
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.stage2 import (
    ControllerStopReason,
    MemoryGatewayMode,
    MemoryGatewayPolicy,
    MemoryGatewayResult,
    MemoryResolutionRequest,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.paired_controller import PairedMemoryControllerRunner


class MemoryGatewayBlockedError(RuntimeError):
    pass


class MemoryGateway:
    def __init__(
        self,
        paired_runner: PairedMemoryControllerRunner,
        policy: MemoryGatewayPolicy,
        artifacts: ArtifactRepository,
        *,
        schema_version: SchemaVersion,
    ) -> None:
        if policy.configuration_fingerprint != paired_runner.comparison_basis_fingerprint:
            raise ValueError("Memory Gateway policy and runner configuration differ")
        self._paired = paired_runner
        self._policy = policy
        self._artifacts = artifacts
        self._schema_version = schema_version

    def resolve(
        self,
        request: MemoryResolutionRequest,
        text_root: TextRootDocument,
        *,
        thread_id: str,
        evaluator_only_artifacts: tuple[ArtifactId, ...] = (),
    ) -> MemoryGatewayResult:
        comparison = None
        fallback = False
        fallback_reason: str | None = None
        if self._policy.mode is MemoryGatewayMode.DETERMINISTIC:
            selected = self._paired.run_deterministic(
                request,
                text_root,
                evaluator_only_artifacts=evaluator_only_artifacts,
            )
        else:
            agentic = self._paired.run_agentic(
                request,
                text_root,
                thread_id=thread_id,
                evaluator_only_artifacts=evaluator_only_artifacts,
            )
            bounded_eligible = (
                agentic.stop_reason is ControllerStopReason.SUFFICIENT
                and agentic.future_leakage_count == 0
            )
            if bounded_eligible:
                selected = agentic
            elif self._policy.allow_deterministic_fallback:
                deterministic = self._paired.run_deterministic(
                    request,
                    text_root,
                    evaluator_only_artifacts=evaluator_only_artifacts,
                )
                comparison = self._paired.compare(request, deterministic, agentic)
                if deterministic.future_leakage_count:
                    raise MemoryGatewayBlockedError(
                        "deterministic fallback contains evaluator-only artifacts"
                    )
                selected = deterministic
                fallback = True
                fallback_reason = (
                    "bounded controller contains evaluator-only artifacts"
                    if agentic.future_leakage_count
                    else f"bounded controller stopped: {agentic.stop_reason.value}"
                )
            else:
                raise MemoryGatewayBlockedError(
                    "bounded controller is ineligible and deterministic fallback is disabled"
                )
        if selected.future_leakage_count:
            raise MemoryGatewayBlockedError("selected context contains evaluator-only artifacts")
        frozen = self._artifacts.put(
            canonical_json_bytes(selected.context.model_dump(mode="json")),
            "application/vnd.novel-agent.context-package+json",
            self._schema_version,
        )
        return MemoryGatewayResult(
            gateway_result_id=StableId(f"gateway-result.{request.request_id.root}"),
            request_id=request.request_id,
            selected_arm=selected.arm,
            fallback_used=fallback,
            fallback_reason=fallback_reason,
            context=selected.context,
            frozen_context_artifact=frozen,
            selected_result=selected,
            comparison=comparison,
            promotion_evidence=self._policy.promotion_evidence,
            policy_id=self._policy.policy_id,
            configuration_fingerprint=self._policy.configuration_fingerprint,
        )
