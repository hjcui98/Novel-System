"""Deterministic incident clustering and bounded U8-D candidate generation."""

from __future__ import annotations

import json
from collections import defaultdict

from novel_agent.domain.evolution import (
    EvolutionCandidate,
    EvolutionCandidateDraft,
    EvolutionCandidateGenerationRequest,
    EvolutionCandidateGenerationResult,
    EvolutionIncident,
    EvolutionIncidentCluster,
    EvolutionTargetKind,
)
from novel_agent.domain.ids import (
    ArtifactId,
    RunId,
    SchemaVersion,
    StableId,
    TaskId,
    bounded_stable_id,
)
from novel_agent.domain.model_calls import ModelCallPurpose, ModelRequest, ModelRole
from novel_agent.domain.runtime import FailureClass
from novel_agent.prompts.registry import PromptRegistry
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.model_gateway import ModelGateway
from novel_agent.skills.registry import SkillRegistry


class EvolutionCandidateGenerationRejected(ValueError):
    """The corpus or model output cannot form one bounded evolution candidate."""


class EvolutionIncidentClusterer:
    """Group only repeated incidents with exactly the same governed identity."""

    @staticmethod
    def cluster(
        incidents: tuple[EvolutionIncident, ...],
    ) -> tuple[EvolutionIncidentCluster, ...]:
        groups: dict[
            tuple[StableId, FailureClass, ArtifactId, StableId, EvolutionTargetKind],
            list[EvolutionIncident],
        ] = defaultdict(list)
        for incident in incidents:
            key = (
                incident.problem_key,
                incident.failure_class,
                incident.safety_boundary_id,
                incident.target_id,
                incident.target_kind,
            )
            groups[key].append(incident)
        clusters = []
        for key, items in groups.items():
            if len(items) < 2:
                continue
            problem_key, failure_class, boundary, target_id, target_kind = key
            first = items[0]
            clusters.append(
                EvolutionIncidentCluster(
                    cluster_id=bounded_stable_id(
                        f"evolution-cluster.{first.problem_key.root}",
                        f"evolution-cluster.{first.incident_id.root}",
                    ),
                    problem_key=problem_key,
                    failure_class=failure_class,
                    safety_boundary_id=boundary,
                    target_id=target_id,
                    target_kind=target_kind,
                    incidents=tuple(items),
                )
            )
        return tuple(sorted(clusters, key=lambda item: item.cluster_id.root))


class EvolutionCandidateGeneratorService:
    """Generate exactly one offline prompt/Skill/policy candidate.

    Code candidates remain owned by the Codex-DSH worktree loop and must be
    supplied as already isolated artifacts with a human gate.
    """

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        artifacts: ArtifactRepository,
        prompts: PromptRegistry,
        skills: SkillRegistry,
        prompt_id: StableId,
        prompt_version: SchemaVersion,
        skill_id: StableId,
        skill_version: SchemaVersion,
        artifact_schema_version: SchemaVersion,
    ) -> None:
        self._gateway = gateway
        self._artifacts = artifacts
        self._prompts = prompts
        self._skills = skills
        self._prompt_id = prompt_id
        self._prompt_version = prompt_version
        self._skill_id = skill_id
        self._skill_version = skill_version
        self._artifact_schema_version = artifact_schema_version

    async def generate(
        self, request: EvolutionCandidateGenerationRequest
    ) -> EvolutionCandidateGenerationResult:
        cluster = request.cluster
        if cluster.target_kind is EvolutionTargetKind.CODE:
            raise EvolutionCandidateGenerationRejected(
                "runtime cannot generate code candidates or obtain Git authority"
            )
        skill_text, skill_ref = self._skills.resolve(self._skill_id, self._skill_version)
        if skill_ref.content_hash != request.skill_contract_hash:
            raise EvolutionCandidateGenerationRejected(
                "frozen evolution skill hash does not match registry"
            )
        base = self._artifacts.read_verified(request.base_artifact_ref)
        incident_objects = tuple(self._read_incident(incident) for incident in cluster.incidents)
        task_payload = {
            "cluster_id": cluster.cluster_id.root,
            "problem_key": cluster.problem_key.root,
            "failure_class": cluster.failure_class.value,
            "safety_boundary_id": cluster.safety_boundary_id.root,
            "target_id": cluster.target_id.root,
            "target_kind": cluster.target_kind.value,
            "base_content": base.decode("utf-8", errors="strict"),
            "incidents": incident_objects,
            "maximum_candidates": 1,
        }
        source_hashes = (
            request.base_artifact_ref.artifact_id,
            *(item.incident_ref.artifact_id for item in cluster.incidents),
        )
        rendered, prompt_refs = self._prompts.render(
            ((self._prompt_id, self._prompt_version),),
            task_payload=json.dumps(task_payload, ensure_ascii=False, sort_keys=True),
            source_hashes=source_hashes,
        )
        if len(prompt_refs) != 1 or prompt_refs[0].content_hash != request.prompt_contract_hash:
            raise EvolutionCandidateGenerationRejected(
                "frozen evolution prompt hash does not match registry"
            )
        prompt = rendered + "\n\n<EVOLUTION_SKILL>\n" + skill_text + "\n</EVOLUTION_SKILL>"
        if len(prompt.encode("utf-8")) > request.budget.max_context_bytes:
            raise EvolutionCandidateGenerationRejected(
                "evolution candidate context exceeds the pre-registered byte budget"
            )
        draft, call = await self._gateway.generate_structured_audited(
            ModelRequest(
                request_id=request.model_request_id,
                run_id=self._run_id(cluster),
                task_id=self._task_id(cluster),
                model_role=ModelRole.IMPLEMENTATION,
                purpose=ModelCallPurpose.DEVELOPMENT,
                trace_id=f"u8d-evolution:{request.request_id.root}",
                prompt=prompt,
                prompt_contract_hashes=(request.prompt_contract_hash,),
                skill_contract_hashes=(request.skill_contract_hash,),
                max_output_tokens=request.budget.max_output_tokens,
                timeout_seconds=request.budget.timeout_seconds,
                enable_thinking=request.budget.enable_thinking,
                thinking_token_budget=request.budget.thinking_token_budget,
            ),
            EvolutionCandidateDraft,
        )
        if cluster.target_id not in draft.affected_contract_ids:
            raise EvolutionCandidateGenerationRejected(
                "bounded candidate must name its pre-registered target contract"
            )
        replacement = draft.replacement_content.encode("utf-8")
        if replacement == base:
            raise EvolutionCandidateGenerationRejected(
                "evolution candidate did not change the active content"
            )
        candidate_ref = self._artifacts.put(
            replacement,
            self._media_type(cluster.target_kind),
            self._artifact_schema_version,
        )
        candidate = EvolutionCandidate(
            candidate_id=bounded_stable_id(
                f"evolution-candidate.{request.request_id.root}",
                f"evolution-candidate.{cluster.cluster_id.root}",
            ),
            target_id=cluster.target_id,
            target_kind=cluster.target_kind,
            base_artifact_ref=request.base_artifact_ref,
            candidate_artifact_ref=candidate_ref,
            incident_refs=tuple(item.incident_ref for item in cluster.incidents),
            affected_contract_ids=draft.affected_contract_ids,
            change_summary=draft.change_summary,
            requires_human_gate=request.requires_human_gate,
        )
        return EvolutionCandidateGenerationResult(
            candidate=candidate,
            candidate_ref=candidate_ref,
            model_call=call,
        )

    def _read_incident(self, incident: EvolutionIncident) -> dict[str, object]:
        raw = self._artifacts.read_verified(incident.incident_ref)
        try:
            value: object = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            value = raw.decode("utf-8", errors="replace")
        return {"incident_id": incident.incident_id.root, "value": value}

    @staticmethod
    def _run_id(cluster: EvolutionIncidentCluster) -> RunId:
        return RunId(
            bounded_stable_id(
                f"run.{cluster.cluster_id.root}", f"run.{cluster.problem_key.root}"
            ).root
        )

    @staticmethod
    def _task_id(cluster: EvolutionIncidentCluster) -> TaskId:
        return TaskId(
            bounded_stable_id(
                f"task.{cluster.cluster_id.root}", f"task.{cluster.problem_key.root}"
            ).root
        )

    @staticmethod
    def _media_type(target_kind: EvolutionTargetKind) -> str:
        return {
            EvolutionTargetKind.PROMPT: "text/markdown",
            EvolutionTargetKind.SKILL: "text/markdown",
            EvolutionTargetKind.POLICY: "application/json",
        }[target_kind]


__all__ = [
    "EvolutionCandidateGenerationRejected",
    "EvolutionCandidateGeneratorService",
    "EvolutionIncidentClusterer",
]
