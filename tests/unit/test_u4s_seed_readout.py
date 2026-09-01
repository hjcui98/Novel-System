from pathlib import Path

from novel_agent.domain.ids import RunId, StableId
from novel_agent.domain.retrieval_routing import RetrievalBackendProfile
from novel_agent.domain.v05_readout import (
    V05HistoryAccess,
    V05ReadoutTaskIdentity,
    V05ReadoutTrack,
)
from novel_agent.domain.writer_context import BenchmarkInformationProfile
from novel_agent.services.u4s_seed_readout import U4SPublicCorpus

BUNDLE = Path("benchmarks/private/ztj_novelmem_v0.5")


def _identity(
    *,
    task_id: str,
    track: V05ReadoutTrack,
    checkpoint: int,
    access: V05HistoryAccess,
    question_id: str | None = None,
) -> V05ReadoutTaskIdentity:
    conditioned = access is V05HistoryAccess.AUTHOR_PLAN_CONDITIONED
    return V05ReadoutTaskIdentity(
        task_id=StableId(task_id),
        track=track,
        checkpoint_id=StableId(f"ZTJ-C{checkpoint:03d}"),
        checkpoint_chapter=checkpoint,
        history_access=access,
        information_profile=(
            BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED
            if conditioned
            else BenchmarkInformationProfile.VISIBLE_AT_CUTOFF
        ),
        question_id=None if question_id is None else StableId(question_id),
        question_release=None if question_id is None else "after_checkpoint_freeze",
        target_chapter_start=None if track is V05ReadoutTrack.QA else checkpoint + 1,
        target_chapter_end=None if track is V05ReadoutTrack.QA else checkpoint + 5,
    )


def test_public_corpus_builds_cumulative_cutoff_roots() -> None:
    corpus = U4SPublicCorpus(BUNDLE)
    assert corpus._question_cache == {}
    assert corpus._context_cache == {}
    early = corpus.text_root(20)
    late = corpus.text_root(21)

    assert [chapter.chapter_index for chapter in early.chapters][-1] == 20
    assert all(chapter.chapter_index <= 20 for chapter in early.chapters)
    assert late.root_hash != early.root_hash
    assert late.chapters[-1].chapter_index == 21


def test_qa_input_releases_question_after_checkpoint_and_uses_text_replay() -> None:
    corpus = U4SPublicCorpus(BUNDLE)
    input_ = corpus.checkpoint_input(
        _identity(
            task_id="task.v05.qa.ZTJ-C020-Q001",
            track=V05ReadoutTrack.QA,
            checkpoint=20,
            access=V05HistoryAccess.HISTORY_ONLY,
            question_id="ZTJ-C020-Q001",
        ),
        run_id=RunId("run.u4s.test.qa"),
    )

    assert input_.question_text is not None
    assert input_.question_text.startswith("陈长生十岁时被诊断出的根本身体问题是什么")
    assert input_.task.task_id.root == "task.v05.qa.ZTJ-C020-Q001"
    assert input_.task.planning_context_ref is None
    assert input_.need.base_commit == input_.basis_commit
    assert input_.backend_bundle.attestation.retrieval_backend_profile is (
        RetrievalBackendProfile.BENCHMARK_TEXT_REPLAY
    )
    assert input_.backend_bundle.attestation.embedding_model is None
    assert input_.backend_bundle.attestation.capability.status.value == "exact"


def test_apc_input_is_plan_bound_but_history_only_input_is_not() -> None:
    corpus = U4SPublicCorpus(BUNDLE)
    apc = corpus.checkpoint_input(
        _identity(
            task_id="task.v05.context.ZTJ-C020.author-plan-conditioned",
            track=V05ReadoutTrack.CONTEXT,
            checkpoint=20,
            access=V05HistoryAccess.AUTHOR_PLAN_CONDITIONED,
        ),
        run_id=RunId("run.u4s.test.apc"),
    )
    history = corpus.checkpoint_input(
        _identity(
            task_id="task.v05.context.ZTJ-C020.history-only",
            track=V05ReadoutTrack.CONTEXT,
            checkpoint=20,
            access=V05HistoryAccess.HISTORY_ONLY,
        ),
        run_id=RunId("run.u4s.test.history"),
    )

    assert apc.task.planning_context_ref is not None
    assert apc.task.planning_context_hash == apc.planning_context.source_hash
    assert apc.need.allow_plan is True
    assert apc.plan.nodes
    assert apc.plan.chapter_goals
    assert history.task.planning_context_ref is None
    assert history.need.allow_plan is False
    assert history.plan.nodes == ()
    assert history.plan.chapter_goals == ()


def test_fallback_planner_artifact_is_typed_and_does_not_require_a_model() -> None:
    corpus = U4SPublicCorpus(BUNDLE)
    input_ = corpus.checkpoint_input(
        _identity(
            task_id="task.v05.qa.ZTJ-C020-Q003",
            track=V05ReadoutTrack.QA,
            checkpoint=20,
            access=V05HistoryAccess.HISTORY_ONLY,
            question_id="ZTJ-C020-Q003",
        ),
        run_id=RunId("run.u4s.test.fallback"),
    )

    assert input_.planner_artifact.fallback_status.value == "planner_fallback"
    assert input_.planner_artifact.fallback_reason
    assert input_.planner_artifact.attempts == ()
    assert input_.planner_artifact.parsed_drafts == ()
