from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import create_engine

from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
from novel_agent.adapters.postgres.database import Base, build_session_factory
from novel_agent.domain.creative_runtime import AutomationMode
from novel_agent.domain.ids import ProjectId, RunId, SchemaVersion, StableId
from novel_agent.domain.stage2 import (
    AgentMode,
    BootstrapStrategy,
    PlannerExecutionResult,
    ProjectIntentModel,
    ProposalProvenance,
    ProposedItem,
)
from novel_agent.runtime.production_novel_bootstrap import ProductionNovelBootstrap
from novel_agent.services.artifacts import ArtifactRepository
from tests.unit.test_stage2_bootstrap_workflow import proposals

VERSION = SchemaVersion("1.0.0")


def test_bootstrap_prepare_then_commit_emits_auto_dispatch_descriptor(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = build_session_factory(engine)
    artifacts = ArtifactRepository(FilesystemObjectStore(tmp_path / "objects"))
    project_id = ProjectId("project.bootstrap.novel")
    plan_proposal, world_patch = proposals(project_id)
    plan_proposal = plan_proposal.model_copy(
        update={"strategy": BootstrapStrategy.DEVELOP_CANDIDATES}
    )
    planner_result = PlannerExecutionResult(
        mode=AgentMode.PROJECT_BOOTSTRAP,
        project_intent=ProjectIntentModel(
            intent_id=StableId("intent.bootstrap"),
            project_id=project_id,
            strategy=BootstrapStrategy.DEVELOP_CANDIDATES,
            items=(
                ProposedItem(
                    item_id=StableId("intent.item"),
                    kind="premise",
                    payload={"summary": "story"},
                    provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                    source_ids=(StableId("source.author-initial-brief"),),
                ),
            ),
            source_ids=(StableId("source.author-initial-brief"),),
            coverage=1,
        ),
        plan_proposal=plan_proposal.model_copy(
            update={
                "items": (
                    ProposedItem(
                        item_id=StableId("plan.item"),
                        kind="premise",
                        payload={"summary": "story"},
                        provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                        source_ids=(StableId("source.author-initial-brief"),),
                    ),
                ),
                "strategy": BootstrapStrategy.DEVELOP_CANDIDATES,
            }
        ),
        output_artifact=artifacts.put(b"planner", "application/json", VERSION),
        receipt=plan_proposal.receipt,
    )
    world_patch = world_patch.model_copy(
        update={
            "origin_source_ids": (StableId("source.author-initial-brief"),),
            "items": (
                ProposedItem(
                    item_id=StableId("world.item"),
                    kind="baseline_state",
                    payload={"fact": "known", "label": "Lin"},
                    provenance=ProposalProvenance.AUTHOR_SUPPLIED,
                    source_ids=(StableId("source.author-initial-brief"),),
                ),
            ),
        }
    )

    async def planner() -> PlannerExecutionResult:
        return planner_result

    async def curator() -> object:
        return world_patch

    service = ProductionNovelBootstrap(
        artifacts=artifacts,
        session_factory=sessions,
        planner=planner,
        curator=curator,  # type: ignore[arg-type]
    )
    prepared = asyncio.run(
        service.prepare(project_id=project_id, brief_text="A wounded heir enters the tower.")
    )
    assert prepared.document.preview["validation_status"] == "passed"
    assert prepared.document.plan.nodes or prepared.document.plan.chapter_goals
    assert prepared.document.world.entities
    policy, request, descriptor = service.commit(
        prepared=prepared.document,
        author_id=StableId("author.1"),
        reason="reviewed Plan/World/Profile",
        target_chapters=10,
        run_id=RunId("run.bootstrap.novel"),
        object_store_root=tmp_path / "objects",
    )
    assert policy.automation_mode is AutomationMode.AUTO
    assert policy.auto_accept_plan is True
    assert policy.auto_accept_draft is True
    assert request.target_chapters == 10
    assert descriptor.stop_after_chapter == 10
    engine.dispose()
