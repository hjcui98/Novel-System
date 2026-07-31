"""Compile canonical checkpoint cases into one continuous Stage 2 benchmark scenario."""

from __future__ import annotations

from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.benchmark import (
    BenchmarkBundle,
    ChapterDocument,
    PreludeDocument,
    TextRootDocument,
)
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.stage2 import (
    BenchmarkCheckpointDeclaration,
    BenchmarkInformationProfile,
    BenchmarkScenario,
    BenchmarkScenarioProfile,
    BootstrapSource,
    IndependentRebuildComparison,
    IndependentRebuildReport,
    ScenarioBuildMode,
    SourceClass,
    SourceClassification,
    SourceDestination,
)
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.content_addressing import canonical_json_bytes, content_id


class BenchmarkScenarioCompiler:
    version = "stage2-scenario-compiler-v0.1"

    def compile(
        self,
        bundle: BenchmarkBundle,
        information_profile: BenchmarkInformationProfile,
    ) -> BenchmarkScenario:
        ordered_cases = tuple(sorted(bundle.case_manifests, key=lambda item: item.history_range[1]))
        if not ordered_cases:
            raise ValueError("continuous benchmark scenario requires checkpoint cases")
        project_ids = {case.project_id for case in ordered_cases}
        if len(project_ids) != 1:
            raise ValueError("continuous scenario cases must belong to one project")
        checkpoints = tuple(case.history_range[1] for case in ordered_cases)
        if len(checkpoints) != len(set(checkpoints)):
            raise ValueError("continuous scenario checkpoint chapters must be unique")
        rebuild = self.independent_rebuild_report(bundle)
        if not rebuild.all_consistent:
            raise ValueError(
                "continuous scenario requires independently consistent checkpoint prefixes"
            )
        reference_case = ordered_cases[-1]
        reference_text = self._text_root(bundle, reference_case.input_text_root)
        sources: list[BootstrapSource] = []
        classifications: list[SourceClassification] = []
        evaluator_sources_by_case: dict[StableId, tuple[StableId, ...]] = {}
        if reference_text.prelude is not None:
            payload = canonical_json_bytes(reference_text.prelude.model_dump(mode="json"))
            source = self._source(
                source_id=StableId("source.chapter.prelude"),
                source_class=SourceClass.CHAPTER_TEXT,
                payload=payload,
                bundle=bundle,
                earliest_visible_chapter=0,
                chapter_index=0,
            )
            sources.append(source)
            classifications.append(
                self._classification(
                    source,
                    (SourceDestination.TEXT, SourceDestination.REFERENCE),
                    (SourceDestination.PLAN, SourceDestination.EVALUATION),
                )
            )
        for chapter in reference_text.chapters:
            payload = canonical_json_bytes(chapter.model_dump(mode="json"))
            source = self._source(
                source_id=StableId(f"source.chapter.{chapter.chapter_index}"),
                source_class=SourceClass.CHAPTER_TEXT,
                payload=payload,
                bundle=bundle,
                earliest_visible_chapter=chapter.chapter_index,
                chapter_index=chapter.chapter_index,
            )
            sources.append(source)
            classifications.append(
                self._classification(
                    source,
                    (SourceDestination.TEXT, SourceDestination.REFERENCE),
                    (SourceDestination.PLAN, SourceDestination.EVALUATION),
                )
            )
        for case in ordered_cases:
            # Per-case PlanRoots are reconstructed from the completed target and
            # remain evaluator-only. Public replay retains only the coarse PlanRoot
            # created from bootstrap author input.
            future = self._text_root(bundle, case.future_text_root_private)
            future_source = self._source(
                source_id=StableId(f"source.future.{case.case_id.root}"),
                source_class=SourceClass.FUTURE_TEXT_PRIVATE,
                payload=canonical_json_bytes(future.model_dump(mode="json")),
                bundle=bundle,
                earliest_visible_chapter=case.history_range[1],
                evaluator_only=True,
            )
            sources.append(future_source)
            classifications.append(self._evaluator_classification(future_source))
            gold_payload = canonical_json_bytes(
                {
                    "observed_use_gold": [
                        item.model_dump(mode="json") for item in case.observed_use_gold
                    ],
                    "operational_constraint_gold": [
                        item.model_dump(mode="json") for item in case.operational_constraint_gold
                    ],
                    "plan_obligation_gold": [
                        item.model_dump(mode="json") for item in case.plan_obligation_gold
                    ],
                }
            )
            gold_source = self._source(
                source_id=StableId(f"source.gold.{case.case_id.root}"),
                source_class=SourceClass.READ_GOLD,
                payload=gold_payload,
                bundle=bundle,
                earliest_visible_chapter=case.history_range[1],
                evaluator_only=True,
            )
            sources.append(gold_source)
            classifications.append(self._evaluator_classification(gold_source))
            evaluator_sources_by_case[case.case_id] = (
                future_source.source_id,
                gold_source.source_id,
            )
        fingerprint = content_id(
            {
                "compiler": self.version,
                "bundle_hash": bundle.content_hash.root,
                "information_profile": information_profile.value,
                "checkpoint_chapters": checkpoints,
                "reference_text_root": reference_text.root_hash.root,
            }
        )
        return BenchmarkScenario(
            scenario_id=StableId(f"scenario.{bundle.bundle_id.root}.{information_profile.value}"),
            project_id=reference_case.project_id,
            branch=f"benchmark/{information_profile.value}",
            sources=tuple(sources),
            classifications=tuple(classifications),
            profile=BenchmarkScenarioProfile(
                profile_id=StableId(f"profile.{bundle.bundle_id.root}.{information_profile.value}"),
                build_mode=ScenarioBuildMode.CONTINUOUS_REPLAY,
                information_profile=information_profile,
                checkpoint_chapters=checkpoints,
                configuration_fingerprint=fingerprint,
            ),
            checkpoint_cases=tuple(
                BenchmarkCheckpointDeclaration(
                    case_id=case.case_id,
                    checkpoint_chapter=case.history_range[1],
                    evaluator_source_ids=evaluator_sources_by_case[case.case_id],
                )
                for case in ordered_cases
            ),
        )

    def independent_rebuild_report(
        self,
        bundle: BenchmarkBundle,
    ) -> IndependentRebuildReport:
        ordered_cases = tuple(sorted(bundle.case_manifests, key=lambda item: item.history_range[1]))
        if not ordered_cases:
            raise ValueError("independent rebuild comparison requires checkpoint cases")
        reference_case = ordered_cases[-1]
        reference = self._text_root(bundle, reference_case.input_text_root)
        reference_chapters = {
            chapter.chapter_index: self._chapter_fingerprint(chapter)
            for chapter in reference.chapters
        }
        reference_prelude = (
            self._prelude_fingerprint(reference.prelude) if reference.prelude is not None else None
        )
        comparisons: list[IndependentRebuildComparison] = []
        for case in ordered_cases:
            checkpoint = self._text_root(bundle, case.input_text_root)
            checkpoint_chapters = {
                chapter.chapter_index: self._chapter_fingerprint(chapter)
                for chapter in checkpoint.chapters
            }
            compared_indexes = tuple(
                sorted(
                    {
                        index
                        for index in (*reference_chapters, *checkpoint_chapters)
                        if index <= case.history_range[1]
                    }
                )
            )
            mismatches = tuple(
                (
                    *(
                        (0,)
                        if (
                            (
                                self._prelude_fingerprint(checkpoint.prelude)
                                if checkpoint.prelude is not None
                                else None
                            )
                            != reference_prelude
                        )
                        else ()
                    ),
                    *(
                        index
                        for index in compared_indexes
                        if reference_chapters.get(index) != checkpoint_chapters.get(index)
                    ),
                )
            )
            comparisons.append(
                IndependentRebuildComparison(
                    case_id=case.case_id,
                    checkpoint_chapter=case.history_range[1],
                    checkpoint_text_root=checkpoint.root_hash,
                    reference_text_root=reference.root_hash,
                    compared_chapters=len(compared_indexes)
                    + (1 if checkpoint.prelude is not None or reference.prelude is not None else 0),
                    mismatched_chapters=mismatches,
                    consistent=not mismatches,
                )
            )
        fingerprint = content_id(
            {
                "compiler": self.version,
                "bundle_hash": bundle.content_hash.root,
                "reference_case_id": reference_case.case_id.root,
                "comparisons": [item.model_dump(mode="json") for item in comparisons],
            }
        )
        return IndependentRebuildReport(
            report_id=StableId(f"independent-rebuild.{bundle.bundle_id.root}"),
            bundle_hash=bundle.content_hash,
            reference_case_id=reference_case.case_id,
            comparisons=tuple(comparisons),
            all_consistent=all(item.consistent for item in comparisons),
            configuration_fingerprint=fingerprint,
        )

    @staticmethod
    def _text_root(bundle: BenchmarkBundle, root_hash: ArtifactId) -> TextRootDocument:
        root = next(
            (item for item in bundle.text_roots if item.root_hash == root_hash),
            None,
        )
        if root is None:
            raise ValueError(f"scenario references a missing TextRoot: {root_hash.root}")
        return root

    @staticmethod
    def _chapter_fingerprint(chapter: ChapterDocument) -> ArtifactId:
        return content_id(
            {
                "chapter_index": chapter.chapter_index,
                "title": chapter.title,
                "scenes": [
                    {
                        "scene_index": scene.scene_index,
                        "title": scene.title,
                        "blocks": [block.text for block in scene.blocks],
                    }
                    for scene in chapter.scenes
                ],
            }
        )

    @staticmethod
    def _prelude_fingerprint(prelude: PreludeDocument) -> ArtifactId:
        return content_id(
            {
                "title": prelude.title,
                "scenes": [
                    {
                        "scene_index": scene.scene_index,
                        "title": scene.title,
                        "blocks": [block.text for block in scene.blocks],
                    }
                    for scene in prelude.scenes
                ],
            }
        )

    @staticmethod
    def _source(
        *,
        source_id: StableId,
        source_class: SourceClass,
        payload: bytes,
        bundle: BenchmarkBundle,
        earliest_visible_chapter: int,
        chapter_index: int | None = None,
        evaluator_only: bool = False,
    ) -> BootstrapSource:
        artifact_id = sha256_id(payload)
        media_type = "application/json"
        artifact = ArtifactRef(
            artifact_id=artifact_id,
            media_type=media_type,
            byte_length=len(payload),
            schema_version=bundle.bundle_schema_version,
        )
        return BootstrapSource(
            source_id=source_id,
            source_class=source_class,
            media_type=media_type,
            content_hash=artifact_id,
            byte_length=len(payload),
            artifact_ref=artifact,
            earliest_visible_chapter=earliest_visible_chapter,
            chapter_index=chapter_index,
            evaluator_only=evaluator_only,
        )

    @staticmethod
    def _classification(
        source: BootstrapSource,
        allowed: tuple[SourceDestination, ...],
        forbidden: tuple[SourceDestination, ...],
    ) -> SourceClassification:
        return SourceClassification(
            source_id=source.source_id,
            source_class=source.source_class,
            allowed_destinations=allowed,
            forbidden_destinations=forbidden,
            classification_reason=f"compiled policy for {source.source_class.value}",
        )

    @classmethod
    def _evaluator_classification(
        cls,
        source: BootstrapSource,
    ) -> SourceClassification:
        return cls._classification(
            source,
            (SourceDestination.EVALUATION,),
            (
                SourceDestination.TEXT,
                SourceDestination.PLAN,
                SourceDestination.WORLD,
                SourceDestination.REFERENCE,
                SourceDestination.PROJECT_PROFILE,
            ),
        )
