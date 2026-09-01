from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model import FakeModelEndpoint
from novel_agent.domain.evolution import (
    EvolutionCandidateGenerationBudget,
    EvolutionCandidateGenerationRequest,
    EvolutionCandidateGenerationResult,
    EvolutionIncident,
    EvolutionIncidentCluster,
    EvolutionTargetKind,
)
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.model_calls import ModelRole
from novel_agent.domain.runtime import FailureClass
from novel_agent.prompts.registry import PromptRegistry, PromptTemplate, content_hash
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.evolution_candidate import (
    EvolutionCandidateGenerationRejected,
    EvolutionCandidateGeneratorService,
    EvolutionIncidentClusterer,
)
from novel_agent.services.model_gateway import ModelGateway, RegisteredModelEndpoint
from novel_agent.skills.registry import SkillRegistry, SkillTemplate

SCHEMA = SchemaVersion("1.0.0")
BOUNDARY = ArtifactId("sha256:" + "c" * 64)


def _put(repository: ArtifactRepository, value: object, media_type: str = "application/json"):
    raw = value.encode("utf-8") if isinstance(value, str) else json.dumps(value).encode("utf-8")
    return repository.put(raw, media_type, SCHEMA)


def _incident(
    repository: ArtifactRepository,
    suffix: str,
    *,
    problem: str = "problem.repeated",
    target_kind: EvolutionTargetKind = EvolutionTargetKind.PROMPT,
) -> EvolutionIncident:
    return EvolutionIncident(
        incident_id=StableId(f"incident.{suffix}"),
        problem_key=StableId(problem),
        failure_class=FailureClass.CANON_EXTRACTION_GAP,
        safety_boundary_id=BOUNDARY,
        target_id=StableId("prompt.writer"),
        target_kind=target_kind,
        incident_ref=_put(repository, {"incident": suffix}),
    )


def _generator(
    tmp_path: Path,
    repository: ArtifactRepository,
) -> tuple[EvolutionCandidateGeneratorService, FakeModelEndpoint, ArtifactId, ArtifactId]:
    prompt_path = tmp_path / "candidate-prompt.md"
    skill_path = tmp_path / "candidate-skill.md"
    prompt_path.write_text("generate one bounded candidate", encoding="utf-8")
    skill_path.write_text("never generate code", encoding="utf-8")
    prompt_hash = content_hash(prompt_path.read_bytes())
    skill_hash = content_hash(skill_path.read_bytes())
    prompts = PromptRegistry(
        (PromptTemplate(StableId("prompt.evolution.v1"), SCHEMA, prompt_path, prompt_hash),)
    )
    skills = SkillRegistry(
        (SkillTemplate(StableId("skill.evolution.v1"), SCHEMA, skill_path, skill_hash),)
    )
    fake = FakeModelEndpoint(
        json.dumps(
            {
                "replacement_content": "# Writer prompt\nRequire exact source evidence.\n",
                "affected_contract_ids": ["prompt.writer"],
                "change_summary": "Require exact source evidence for the demonstrated gap.",
            }
        )
    )
    gateway = ModelGateway(
        (
            RegisteredModelEndpoint(
                role=ModelRole.IMPLEMENTATION,
                endpoint_name="evolution-test",
                model_name="fake",
                revision="fake-v1",
                adapter=fake,
                output_limit=16384,
            ),
        )
    )
    return (
        EvolutionCandidateGeneratorService(
            gateway=gateway,
            artifacts=repository,
            prompts=prompts,
            skills=skills,
            prompt_id=StableId("prompt.evolution.v1"),
            prompt_version=SCHEMA,
            skill_id=StableId("skill.evolution.v1"),
            skill_version=SCHEMA,
            artifact_schema_version=SCHEMA,
        ),
        fake,
        prompt_hash,
        skill_hash,
    )


def test_clusterer_emits_only_repeated_exact_identity_groups(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    incidents = (
        _incident(repository, "one"),
        _incident(repository, "two"),
        _incident(repository, "singleton", problem="problem.singleton"),
    )

    clusters = EvolutionIncidentClusterer.cluster(incidents)

    assert len(clusters) == 1
    assert clusters[0].problem_key == StableId("problem.repeated")
    assert tuple(item.incident_id.root for item in clusters[0].incidents) == (
        "incident.one",
        "incident.two",
    )


def test_generator_persists_one_isolated_prompt_candidate(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    cluster = EvolutionIncidentClusterer.cluster(
        (_incident(repository, "one"), _incident(repository, "two"))
    )[0]
    base = _put(repository, "# Writer prompt\nOld content.\n", "text/markdown")
    service, fake, prompt_hash, skill_hash = _generator(tmp_path, repository)
    request = EvolutionCandidateGenerationRequest(
        request_id=StableId("evolution.request.1"),
        model_request_id=StableId("model.evolution.request.1"),
        cluster=cluster,
        base_artifact_ref=base,
        prompt_contract_hash=prompt_hash,
        skill_contract_hash=skill_hash,
    )

    result = asyncio.run(service.generate(request))

    assert result.candidate.target_kind is EvolutionTargetKind.PROMPT
    assert result.candidate.bounded_single_change is True
    assert result.candidate.isolated_candidate is True
    assert result.candidate.runtime_hot_mutation_allowed is False
    assert result.candidate.incident_refs == tuple(item.incident_ref for item in cluster.incidents)
    assert repository.read_verified(result.candidate_ref).startswith(b"# Writer prompt")
    assert len(fake.requests) == 1
    assert fake.requests[0].model_role is ModelRole.IMPLEMENTATION
    with pytest.raises(ValidationError, match="artifact differs from candidate identity"):
        EvolutionCandidateGenerationResult(
            candidate=result.candidate,
            candidate_ref=_put(repository, "another candidate", "text/markdown"),
            model_call=result.model_call,
        )


def test_generator_rejects_code_candidate_before_model_call(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    incidents = (
        _incident(repository, "one", target_kind=EvolutionTargetKind.CODE),
        _incident(repository, "two", target_kind=EvolutionTargetKind.CODE),
    )
    cluster = EvolutionIncidentCluster(
        cluster_id=StableId("cluster.code"),
        problem_key=incidents[0].problem_key,
        failure_class=incidents[0].failure_class,
        safety_boundary_id=incidents[0].safety_boundary_id,
        target_id=incidents[0].target_id,
        target_kind=EvolutionTargetKind.CODE,
        incidents=incidents,
    )
    service, fake, prompt_hash, skill_hash = _generator(tmp_path, repository)
    request = EvolutionCandidateGenerationRequest(
        request_id=StableId("evolution.request.code"),
        model_request_id=StableId("model.evolution.request.code"),
        cluster=cluster,
        base_artifact_ref=_put(repository, "diff --git", "text/x-diff"),
        prompt_contract_hash=prompt_hash,
        skill_contract_hash=skill_hash,
        requires_human_gate=True,
    )

    with pytest.raises(EvolutionCandidateGenerationRejected, match="cannot generate code"):
        asyncio.run(service.generate(request))

    assert fake.requests == []


def test_generator_rejects_frozen_prompt_or_skill_drift(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    cluster = EvolutionIncidentClusterer.cluster(
        (_incident(repository, "one"), _incident(repository, "two"))
    )[0]
    service, fake, prompt_hash, skill_hash = _generator(tmp_path, repository)
    request = EvolutionCandidateGenerationRequest(
        request_id=StableId("evolution.request.drift"),
        model_request_id=StableId("model.evolution.request.drift"),
        cluster=cluster,
        base_artifact_ref=_put(repository, "# base", "text/markdown"),
        prompt_contract_hash=prompt_hash,
        skill_contract_hash=skill_hash,
    )
    for field in ("prompt_contract_hash", "skill_contract_hash"):
        drifted = request.model_copy(update={field: ArtifactId("sha256:" + "f" * 64)})
        with pytest.raises(EvolutionCandidateGenerationRejected, match="hash does not match"):
            asyncio.run(service.generate(drifted))
    assert fake.requests == []


def test_generator_rejects_context_and_model_candidate_contract_failures(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    cluster = EvolutionIncidentClusterer.cluster(
        (_incident(repository, "one"), _incident(repository, "two"))
    )[0]
    service, fake, prompt_hash, skill_hash = _generator(tmp_path, repository)
    base = _put(repository, "# Writer prompt\nOld content.\n", "text/markdown")
    request = EvolutionCandidateGenerationRequest(
        request_id=StableId("evolution.request.invalid"),
        model_request_id=StableId("model.evolution.request.invalid"),
        cluster=cluster,
        base_artifact_ref=base,
        prompt_contract_hash=prompt_hash,
        skill_contract_hash=skill_hash,
    )
    large_base = _put(repository, "# large\n" + ("x" * 4096), "text/markdown")
    with pytest.raises(EvolutionCandidateGenerationRejected, match="context exceeds"):
        asyncio.run(
            service.generate(
                request.model_copy(
                    update={
                        "base_artifact_ref": large_base,
                        "budget": EvolutionCandidateGenerationBudget(max_context_bytes=1024),
                    }
                )
            )
        )

    fake.response_text = json.dumps(
        {
            "replacement_content": "# changed",
            "affected_contract_ids": ["prompt.other"],
            "change_summary": "wrong target",
        }
    )
    with pytest.raises(EvolutionCandidateGenerationRejected, match="pre-registered target"):
        asyncio.run(service.generate(request))

    fake.response_text = json.dumps(
        {
            "replacement_content": "# Writer prompt\nOld content.\n",
            "affected_contract_ids": ["prompt.writer"],
            "change_summary": "unchanged",
        }
    )
    with pytest.raises(EvolutionCandidateGenerationRejected, match="did not change"):
        asyncio.run(service.generate(request))
    assert len(fake.requests) == 2


def test_generator_reads_raw_incident_and_supports_skill_and_policy_targets(tmp_path: Path) -> None:
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    raw_ref = repository.put(b"\xff", "text/plain", SCHEMA)
    for target_kind, media_type in (
        (EvolutionTargetKind.SKILL, "text/markdown"),
        (EvolutionTargetKind.POLICY, "application/json"),
    ):
        first = _incident(repository, f"raw-{target_kind.value}-one", target_kind=target_kind)
        second = _incident(repository, f"raw-{target_kind.value}-two", target_kind=target_kind)
        first = first.model_copy(update={"incident_ref": raw_ref})
        cluster = EvolutionIncidentClusterer.cluster((first, second))[0]
        service, fake, prompt_hash, skill_hash = _generator(tmp_path, repository)
        fake.response_text = json.dumps(
            {
                "replacement_content": f"# changed {target_kind.value}",
                "affected_contract_ids": ["prompt.writer"],
                "change_summary": f"changed {target_kind.value}",
            }
        )
        request = EvolutionCandidateGenerationRequest(
            request_id=StableId(f"evolution.request.{target_kind.value}"),
            model_request_id=StableId(f"model.evolution.request.{target_kind.value}"),
            cluster=cluster,
            base_artifact_ref=_put(repository, f"base content {target_kind.value}", media_type),
            prompt_contract_hash=prompt_hash,
            skill_contract_hash=skill_hash,
        )
        result = asyncio.run(service.generate(request))
        assert result.candidate.target_kind is target_kind
        assert result.candidate.candidate_artifact_ref.media_type == media_type
