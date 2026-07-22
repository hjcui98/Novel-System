"""Audited structured-output runner shared by Stage 2 agents and modes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel

from novel_agent.agents.registry import AgentRegistry
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, CommitId, StableId
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.stage2 import (
    AgentExecutionReceipt,
    AgentMode,
    AgentSpec,
    AgentType,
    ContractRef,
    ExecutionStatus,
    SkillContractRef,
    SkillExecutionReceipt,
)
from novel_agent.prompts.registry import PromptRegistry, content_hash
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.model_gateway import ModelGateway
from novel_agent.skills.registry import SkillRegistry

OutputT = TypeVar("OutputT", bound=BaseModel)


class AgentExecutionError(ValueError):
    pass


@dataclass(frozen=True)
class AgentRunResult[OutputT: BaseModel]:
    output: OutputT
    model_call: ModelCallRecord
    receipt: AgentExecutionReceipt
    rendered_prompt: str


@dataclass(frozen=True)
class PreparedAgentRun:
    spec: AgentSpec
    request: ModelRequest
    rendered_prompt: str
    prompt_fingerprint: ArtifactId
    configuration_fingerprint: ArtifactId
    skill_refs: tuple[SkillContractRef, ...]
    input_artifacts: tuple[ArtifactRef, ...]
    base_commit: CommitId | None


class StructuredAgentRunner:
    def __init__(
        self,
        gateway: ModelGateway,
        agents: AgentRegistry,
        prompts: PromptRegistry,
        skills: SkillRegistry,
    ) -> None:
        self._gateway = gateway
        self._agents = agents
        self._prompts = prompts
        self._skills = skills

    async def run(
        self,
        agent_type: AgentType,
        mode: AgentMode,
        version: str,
        request: ModelRequest,
        task_payload: str,
        output_type: type[OutputT],
        *,
        source_hashes: tuple[ArtifactId, ...] = (),
        input_artifacts: tuple[ArtifactRef, ...] = (),
        base_commit: CommitId | None = None,
    ) -> AgentRunResult[OutputT]:
        prepared = self.prepare(
            agent_type,
            mode,
            version,
            request,
            task_payload,
            source_hashes=source_hashes,
            input_artifacts=input_artifacts,
            base_commit=base_commit,
        )
        return await self.execute(prepared, output_type)

    async def execute(
        self,
        prepared: PreparedAgentRun,
        output_type: type[OutputT],
        *,
        output_artifacts: tuple[ArtifactRef, ...] = (),
        unresolved: tuple[str, ...] = (),
    ) -> AgentRunResult[OutputT]:
        """Execute a prepared request and finalize its receipt with trusted outputs."""
        output, call = await self._gateway.generate_structured(prepared.request, output_type)
        receipt = self.receipt(
            prepared,
            call,
            output_artifacts=output_artifacts,
            unresolved=unresolved,
        )
        return AgentRunResult(output, call, receipt, prepared.rendered_prompt)

    def prepare(
        self,
        agent_type: AgentType,
        mode: AgentMode,
        version: str,
        request: ModelRequest,
        task_payload: str,
        *,
        source_hashes: tuple[ArtifactId, ...] = (),
        input_artifacts: tuple[ArtifactRef, ...] = (),
        base_commit: CommitId | None = None,
    ) -> PreparedAgentRun:
        """Resolve immutable contracts and prepare an audited request without calling a model."""
        spec = self._agents.resolve(agent_type, mode, version)
        skill_texts: list[str] = []
        skill_refs: list[SkillContractRef] = []
        for expected_skill in spec.skills:
            text, actual = self._skills.resolve(expected_skill.contract_id, expected_skill.version)
            if actual.content_hash != expected_skill.content_hash:
                raise AgentExecutionError(
                    f"AgentSpec skill hash mismatch: {expected_skill.contract_id.root}"
                )
            skill_texts.append(text)
            skill_refs.append(actual)
        skill_payload = "\n\n".join(skill_texts)
        bounded_payload = (
            "<SKILL_INSTRUCTIONS>\n" + skill_payload + "\n</SKILL_INSTRUCTIONS>\n" + task_payload
        )
        rendered, prompt_refs = self._prompts.render(
            (
                (spec.system_prompt.contract_id, spec.system_prompt.version),
                (spec.task_prompt.contract_id, spec.task_prompt.version),
            ),
            bounded_payload,
            source_hashes,
        )
        expected_prompts = (spec.system_prompt, spec.task_prompt)
        for expected_prompt, actual_prompt in zip(expected_prompts, prompt_refs, strict=True):
            if expected_prompt.content_hash != actual_prompt.content_hash:
                raise AgentExecutionError(
                    f"AgentSpec prompt hash mismatch: {expected_prompt.contract_id.root}"
                )
        render_fingerprint = content_hash(rendered.encode("utf-8"))
        configuration_fingerprint = content_hash(canonical_json_bytes(spec.model_dump(mode="json")))
        safe_request = request.model_copy(
            update={
                "prompt": rendered,
                "agent_id": spec.agent_id,
                "agent_mode": spec.mode.value,
                "agent_spec_hash": spec.content_hash,
                "prompt_contract_hashes": tuple(ref.content_hash for ref in prompt_refs),
                "skill_contract_hashes": tuple(ref.content_hash for ref in skill_refs),
                "tool_policy_hash": spec.tool_policy.content_hash,
                "render_fingerprint": render_fingerprint,
            }
        )
        return PreparedAgentRun(
            spec=spec,
            request=safe_request,
            rendered_prompt=rendered,
            prompt_fingerprint=render_fingerprint,
            configuration_fingerprint=configuration_fingerprint,
            skill_refs=tuple(skill_refs),
            input_artifacts=input_artifacts,
            base_commit=base_commit,
        )

    @staticmethod
    def receipt(
        prepared: PreparedAgentRun,
        call: ModelCallRecord,
        *,
        output_artifacts: tuple[ArtifactRef, ...] = (),
        unresolved: tuple[str, ...] = (),
    ) -> AgentExecutionReceipt:
        return StructuredAgentRunner._receipt(
            prepared.spec,
            prepared.request,
            call,
            prepared.prompt_fingerprint,
            prepared.configuration_fingerprint,
            prepared.skill_refs,
            prepared.input_artifacts,
            prepared.base_commit,
            output_artifacts,
            unresolved,
        )

    @staticmethod
    def _receipt(
        spec: AgentSpec,
        request: ModelRequest,
        call: ModelCallRecord,
        prompt_fingerprint: ArtifactId,
        configuration_fingerprint: ArtifactId,
        skill_refs: tuple[SkillContractRef, ...],
        input_artifacts: tuple[ArtifactRef, ...],
        base_commit: CommitId | None,
        output_artifacts: tuple[ArtifactRef, ...],
        unresolved: tuple[str, ...],
    ) -> AgentExecutionReceipt:
        identity = hashlib.sha256(
            "\0".join(
                (
                    request.run_id.root,
                    request.task_id.root,
                    spec.content_hash.root,
                    configuration_fingerprint.root,
                )
            ).encode()
        ).hexdigest()[:24]
        skill_receipts = tuple(
            SkillExecutionReceipt(
                receipt_id=StableId(f"skill-receipt.{identity}.{index}"),
                run_id=request.run_id,
                task_id=request.task_id,
                skill=skill,
                agent_type=spec.agent_type,
                agent_mode=spec.mode,
                base_commit=base_commit,
                input_artifacts=input_artifacts,
                output_artifacts=output_artifacts,
                unresolved=unresolved,
                status=ExecutionStatus.SUCCEEDED,
                latency_ms=call.latency_ms,
            )
            for index, skill in enumerate(skill_refs)
        )
        return AgentExecutionReceipt(
            receipt_id=StableId(f"agent-receipt.{identity}"),
            run_id=request.run_id,
            task_id=request.task_id,
            agent_spec=ContractRef(
                contract_id=spec.agent_id,
                version=spec.version,
                content_hash=spec.content_hash,
            ),
            agent_type=spec.agent_type,
            agent_mode=spec.mode,
            prompt_fingerprint=prompt_fingerprint,
            configuration_fingerprint=configuration_fingerprint,
            base_commit=base_commit,
            input_artifacts=input_artifacts,
            output_artifacts=output_artifacts,
            skill_receipts=skill_receipts,
            model_call_ids=(call.request_id,),
            unresolved=unresolved,
            status=ExecutionStatus.SUCCEEDED,
            started_at=call.started_at,
            completed_at=call.completed_at,
            latency_ms=call.latency_ms,
        )
