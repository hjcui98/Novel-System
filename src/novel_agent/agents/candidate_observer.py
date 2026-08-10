"""Read-only final-candidate observation through the shared ModelGateway."""

from __future__ import annotations

from pathlib import Path

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.editorial import CuratorObservation
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.model_calls import ModelCallRecord, ModelRequest
from novel_agent.domain.stage2 import AgentMode
from novel_agent.prompts.registry import content_hash
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.content_addressing import canonical_json_bytes
from novel_agent.services.model_gateway import ModelGateway

CANDIDATE_OBSERVER_VERSION = SchemaVersion("1.0.0")
CANDIDATE_OBSERVATION_MEDIA_TYPE = "application/vnd.novel-agent.candidate-observation+json"
_DENIED_CAPABILITIES = (
    "memory.read",
    "memory.write",
    "memory.patch",
    "canonical.commit",
    "root.update",
    "draft.write",
)


class CandidateObservationError(ValueError):
    """The final-candidate observation boundary failed closed."""


class CandidateObservationAgent:
    """Observe one final Draft with no tool or write capability."""

    def __init__(
        self,
        gateway: ModelGateway,
        artifacts: ArtifactRepository,
        *,
        schema_version: SchemaVersion = CANDIDATE_OBSERVER_VERSION,
        package_root: Path | None = None,
        require_admission: bool = True,
    ) -> None:
        if require_admission and gateway.admission_controller is None:
            raise CandidateObservationError("Candidate Observer requires endpoint-global admission")
        self._gateway = gateway
        self._artifacts = artifacts
        self._schema_version = schema_version
        root = package_root or Path(__file__).parents[1]
        prompt = (root / "prompts" / "candidate_observer_v1.md").read_bytes()
        skill = (root / "skills" / "candidate_observation_v1.md").read_bytes()
        self._prompt = prompt.decode("utf-8")
        self._skill = skill.decode("utf-8")
        self._prompt_hash = content_hash(prompt)
        self._skill_hash = content_hash(skill)

    async def observe(
        self,
        draft_id: ArtifactId,
        text_artifact: ArtifactRef,
        basis_context_hash: ArtifactId,
        request: ModelRequest,
    ) -> tuple[CuratorObservation, ArtifactRef, ModelCallRecord]:
        text = self._artifacts.read_verified(text_artifact).decode("utf-8")
        if not text.strip():
            raise CandidateObservationError("Candidate Observer cannot read a blank Draft")
        payload = canonical_json_bytes(
            {
                "draft_id": draft_id.root,
                "draft_text": text,
                "basis_context_hash": basis_context_hash.root,
                "denied_capabilities": _DENIED_CAPABILITIES,
            }
        ).decode("utf-8")
        prepared = request.model_copy(
            update={
                "prompt": (
                    self._prompt
                    + "\n\n<SKILL_INSTRUCTIONS>\n"
                    + self._skill
                    + "\n</SKILL_INSTRUCTIONS>\n<UNTRUSTED_DRAFT>\n"
                    + payload
                    + "\n</UNTRUSTED_DRAFT>"
                ),
                "agent_id": StableId("agent.candidate-observer.observe"),
                "agent_mode": AgentMode.OBSERVE.value,
                "prompt_contract_hashes": (self._prompt_hash,),
                "skill_contract_hashes": (self._skill_hash,),
                "scheduling_stage": "stage3.candidate_observation",
            }
        )
        observation, call = await self._gateway.generate_structured(
            prepared,
            CuratorObservation,
        )
        if observation.draft_id != draft_id:
            raise CandidateObservationError("Candidate observation belongs to another Draft")
        observation = observation.model_copy(update={"model_call_record": call})
        artifact = self._artifacts.put(
            canonical_json_bytes(observation.model_dump(mode="json")),
            CANDIDATE_OBSERVATION_MEDIA_TYPE,
            self._schema_version,
        )
        return observation, artifact, call


__all__ = [
    "CANDIDATE_OBSERVATION_MEDIA_TYPE",
    "CANDIDATE_OBSERVER_VERSION",
    "CandidateObservationAgent",
    "CandidateObservationError",
]
