"""Compile the human-authored pilot workspace into a typed canonical BenchmarkBundle."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, ClassVar

import yaml

from novel_agent.domain.benchmark import (
    AuthorPlanningContext,
    BenchmarkBundle,
    BenchmarkCaseManifest,
    ChapterDocument,
    ChapterGoal,
    GoldItem,
    GoldKind,
    PlanEvidenceRef,
    PlanRootDocument,
    PreludeDocument,
    SceneDocument,
    TextRootDocument,
    VisibleOutlineNode,
)
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, SchemaVersion, StableId
from novel_agent.domain.memory import WorldRootDocument
from novel_agent.domain.memory_benchmark import (
    BenchmarkInformationProfile,
    EvidenceSet,
    GoldBlindness,
    GoldNeedSpec,
    GoldType,
)
from novel_agent.domain.text import (
    EvidenceRef,
    EvidenceSupportStatus,
    TextBlock,
    TextSpanRef,
)
from novel_agent.domain.world import Entity, PlanNode, StateRecord, StoryTime, TruthClass
from novel_agent.services.artifacts import sha256_id
from novel_agent.services.benchmark_importer import (
    bundle_content_id,
    content_id,
    plan_root_content_id,
    quote_hash,
    text_root_content_id,
    world_root_content_id,
)


class HumanBenchmarkCompileError(ValueError):
    pass


class HumanBenchmarkCompiler:
    """Deterministic compiler for the reviewed directory/YAML pilot representation."""

    version = SchemaVersion("0.1.0")

    _PROFILE_BY_MODE: ClassVar[dict[str, BenchmarkInformationProfile]] = {
        "plan_conditioned": BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,
        "task_intent_only": BenchmarkInformationProfile.TASK_INTENT_ONLY,
        "visible_at_cutoff": BenchmarkInformationProfile.VISIBLE_AT_CUTOFF,
    }

    _GOLD_TYPE_MAP: ClassVar[dict[str, GoldType]] = {
        "CURRENT_STATE": GoldType.CURRENT_STATE,
        "WORLD_STATE": GoldType.CURRENT_STATE,
        "OBSERVED_FACT": GoldType.CAUSAL_HISTORY,
        "OBSERVED_PATTERN": GoldType.CAUSAL_HISTORY,
        "OBSERVED_USE": GoldType.CAUSAL_HISTORY,
        "CAUSAL_HISTORY": GoldType.CAUSAL_HISTORY,
        "INSTITUTIONAL_HISTORY": GoldType.CAUSAL_HISTORY,
        "CHARACTER_TRAIT": GoldType.CURRENT_STATE,
        "CAPABILITY": GoldType.CURRENT_STATE,
        "CULTIVATION_STATE": GoldType.CURRENT_STATE,
        "INSTITUTIONAL_STATE": GoldType.CURRENT_STATE,
        "POLITICAL_STATE": GoldType.CURRENT_STATE,
        "RELATIONSHIP_EMOTION": GoldType.RELATIONSHIP_EMOTION,
        "TRUTH_BOUNDARY": GoldType.KNOWLEDGE_BOUNDARY,
        "KNOWLEDGE_BOUNDARY": GoldType.KNOWLEDGE_BOUNDARY,
        "POLITICAL_BOUNDARY": GoldType.KNOWLEDGE_BOUNDARY,
        "PUBLIC_KNOWLEDGE": GoldType.KNOWLEDGE_BOUNDARY,
        "SECRET_STATE": GoldType.KNOWLEDGE_BOUNDARY,
        "PLAN_PATH": GoldType.PLAN_OBLIGATION,
        "PLAN_OBLIGATION": GoldType.PLAN_OBLIGATION,
        "CONTRACT": GoldType.PLAN_OBLIGATION,
        "OPERATIONAL_CONSTRAINT": GoldType.OBJECT_CONTINUITY,
        "LONG_RANGE_CALLBACK": GoldType.LONG_RANGE_CALLBACK,
        "LONG_RANGE_PAYOFF": GoldType.LONG_RANGE_CALLBACK,
        "OBJECT_CONTINUITY": GoldType.OBJECT_CONTINUITY,
        "OBJECT_STATE": GoldType.OBJECT_CONTINUITY,
    }

    def compile(self, root: Path) -> BenchmarkBundle:
        root = root.resolve()
        bundle_manifest = self._json(root / "bundle.json")
        text_roots: list[TextRootDocument] = []
        plan_roots: list[PlanRootDocument] = []
        world_roots: list[WorldRootDocument] = []
        planning_contexts: list[AuthorPlanningContext] = []
        cases: list[BenchmarkCaseManifest] = []
        for relative in bundle_manifest.get("case_manifests", []):
            raw_case = self._json(root / self._string(relative))
            case_id = StableId(self._string(raw_case["case_id"]))
            history = self._text_root(root / self._string(raw_case["input_text_root"]), case_id)
            future = self._text_root(
                root / self._string(raw_case["future_text_root_private"]),
                StableId(f"{case_id.root}.future"),
            )
            source_commit = CommitId(history.root_hash.root)
            input_data = self._yaml(root / self._string(raw_case["input_plan_root"]))
            gold_data = self._yaml(root / self._string(raw_case["gold_file_private"]))
            plan = self._plan_root(case_id, input_data)
            history_range = self._range(raw_case["history_range"])
            target_range = self._range(raw_case["target_range"])
            planning_context = self._compile_planning_context(
                case_id,
                input_data,
                target_range,
            )
            historical_evidence, accepted_evidence = self._historical_evidence(
                raw_case,
                gold_data,
                history,
                source_commit,
            )
            future_evidence = self._all_evidence(future, source_commit, f"future.{case_id.root}")
            observed, operational = self._gold(
                raw_case,
                gold_data,
                historical_evidence,
                accepted_evidence,
                future_evidence,
            )
            plan_gold = self._plan_gold(raw_case, plan, future, source_commit)
            gold_need_specs = self._gold_need_specs(
                raw_case,
                gold_data,
                root,
            )
            world = self._world_root(
                case_id,
                source_commit,
                (*observed, *operational),
            )
            cases.append(
                BenchmarkCaseManifest(
                    case_id=case_id,
                    project_id=ProjectId(self._string(raw_case["project_id"])),
                    history_range=(max(1, history_range[0]), history_range[1]),
                    target_range=target_range,
                    input_text_root=history.root_hash,
                    future_text_root_private=future.root_hash,
                    input_plan_root=plan.root_hash,
                    input_world_root_verified=world.root_hash,
                    chapter_goal_ids=tuple(goal.goal_id for goal in plan.chapter_goals),
                    information_profile=planning_context.profile,
                    task_intent=planning_context.task_intent,
                    planning_context_ref=content_id(planning_context.model_dump(mode="json")),
                    planning_context_hash=planning_context.source_hash,
                    observed_use_gold=observed,
                    operational_constraint_gold=operational,
                    plan_obligation_gold=plan_gold,
                    gold_need_specs=gold_need_specs,
                    annotation_version=self.version,
                    expected_tracks=(),
                    gate_eligible=False,
                )
            )
            text_roots.extend((history, future))
            plan_roots.append(plan)
            world_roots.append(world)
            planning_contexts.append(planning_context)
        provisional = BenchmarkBundle(
            bundle_id=StableId(f"{self._string(bundle_manifest['bundle_id'])}.canonical"),
            bundle_schema_version=self.version,
            content_hash=ArtifactId("sha256:" + "0" * 64),
            text_roots=tuple(text_roots),
            plan_roots=tuple(plan_roots),
            world_roots=tuple(world_roots),
            planning_contexts=tuple(planning_contexts),
            case_manifests=tuple(cases),
            expected_profiles=("visible_at_cutoff", "author_plan_conditioned"),
        )
        return provisional.model_copy(update={"content_hash": bundle_content_id(provisional)})

    def derive_gate_subset(
        self,
        pilot: BenchmarkBundle,
        *,
        target_width: int = 3,
    ) -> BenchmarkBundle:
        if target_width < 1:
            raise HumanBenchmarkCompileError("gate target width must be positive")
        text_by_hash = {root.root_hash: root for root in pilot.text_roots}
        plan_by_hash = {root.root_hash: root for root in pilot.plan_roots}
        context_by_hash = {item.source_hash: item for item in pilot.planning_contexts}
        retained_text = {
            case.input_text_root: text_by_hash[case.input_text_root]
            for case in pilot.case_manifests
        }
        derived_text: list[TextRootDocument] = []
        derived_plan: list[PlanRootDocument] = []
        derived_contexts: list[AuthorPlanningContext] = []
        derived_cases: list[BenchmarkCaseManifest] = []
        for case in pilot.case_manifests:
            target_start = case.target_range[0]
            target_end = min(case.target_range[1], target_start + target_width - 1)
            future = text_by_hash[case.future_text_root_private]
            future_provisional = TextRootDocument(
                root_hash=ArtifactId("sha256:" + "0" * 64),
                schema_version=future.schema_version,
                prelude=future.prelude,
                chapters=tuple(
                    chapter
                    for chapter in future.chapters
                    if target_start <= chapter.chapter_index <= target_end
                ),
            )
            future = future_provisional.model_copy(
                update={"root_hash": text_root_content_id(future_provisional)}
            )
            if case.input_plan_root is None:
                raise HumanBenchmarkCompileError("gate subset requires an input PlanRoot")
            plan = plan_by_hash[case.input_plan_root]
            plan_provisional = PlanRootDocument(
                root_hash=ArtifactId("sha256:" + "0" * 64),
                schema_version=plan.schema_version,
                nodes=plan.nodes,
                chapter_goals=tuple(
                    goal
                    for goal in plan.chapter_goals
                    if target_start <= goal.chapter_index <= target_end
                ),
            )
            plan = plan_provisional.model_copy(
                update={"root_hash": plan_root_content_id(plan_provisional)}
            )
            if case.planning_context_hash is None or case.planning_context_ref is None:
                raise HumanBenchmarkCompileError("gate subset requires a bound planning context")
            original_context = context_by_hash.get(case.planning_context_hash)
            if original_context is None:
                raise HumanBenchmarkCompileError("gate subset planning context is missing")
            context_provisional = original_context.model_copy(
                update={
                    "target_range": (target_start, target_end),
                    "chapter_goals": plan.chapter_goals,
                }
            )
            derived_context = context_provisional.model_copy(
                update={"source_hash": self._planning_context_source_hash(context_provisional)}
            )
            allowed_chapter_ids = {chapter.chapter_id for chapter in future.chapters}
            allowed_goal_ids = {goal.goal_id for goal in plan.chapter_goals}

            def trim_gold(
                items: tuple[GoldItem, ...],
                *,
                lower_bound: int,
                upper_bound: int,
                future_root_hash: ArtifactId,
                permitted_chapter_ids: set[StableId],
                plan_root_hash: ArtifactId,
                permitted_goal_ids: set[StableId],
            ) -> tuple[GoldItem, ...]:
                trimmed: list[GoldItem] = []
                for item in items:
                    target_chapters = tuple(
                        chapter
                        for chapter in item.target_chapters
                        if lower_bound <= chapter <= upper_bound
                    )
                    if not target_chapters:
                        continue
                    future_refs = tuple(
                        ref.model_copy(update={"root_hash": future_root_hash})
                        for ref in item.future_evidence_refs
                        if ref.chapter_id in permitted_chapter_ids
                    )
                    plan_refs = tuple(
                        ref.model_copy(update={"plan_root_hash": plan_root_hash})
                        for ref in item.plan_evidence_refs
                        if ref.goal_id in permitted_goal_ids
                    )
                    trimmed.append(
                        item.model_copy(
                            update={
                                "target_chapters": target_chapters,
                                "future_evidence_refs": future_refs,
                                "plan_evidence_refs": plan_refs,
                            }
                        )
                    )
                return tuple(trimmed)

            derived_text.append(future)
            derived_plan.append(plan)
            derived_contexts.append(derived_context)
            derived_cases.append(
                case.model_copy(
                    update={
                        "target_range": (target_start, target_end),
                        "future_text_root_private": future.root_hash,
                        "input_plan_root": plan.root_hash,
                        "chapter_goal_ids": tuple(goal.goal_id for goal in plan.chapter_goals),
                        "planning_context_ref": content_id(derived_context.model_dump(mode="json")),
                        "planning_context_hash": derived_context.source_hash,
                        "observed_use_gold": trim_gold(
                            case.observed_use_gold,
                            lower_bound=target_start,
                            upper_bound=target_end,
                            future_root_hash=future.root_hash,
                            permitted_chapter_ids=allowed_chapter_ids,
                            plan_root_hash=plan.root_hash,
                            permitted_goal_ids=allowed_goal_ids,
                        ),
                        "operational_constraint_gold": trim_gold(
                            case.operational_constraint_gold,
                            lower_bound=target_start,
                            upper_bound=target_end,
                            future_root_hash=future.root_hash,
                            permitted_chapter_ids=allowed_chapter_ids,
                            plan_root_hash=plan.root_hash,
                            permitted_goal_ids=allowed_goal_ids,
                        ),
                        "plan_obligation_gold": trim_gold(
                            case.plan_obligation_gold,
                            lower_bound=target_start,
                            upper_bound=target_end,
                            future_root_hash=future.root_hash,
                            permitted_chapter_ids=allowed_chapter_ids,
                            plan_root_hash=plan.root_hash,
                            permitted_goal_ids=allowed_goal_ids,
                        ),
                    }
                )
            )
        provisional = pilot.model_copy(
            update={
                "bundle_id": StableId(f"{pilot.bundle_id.root}.gate-target-width-{target_width}"),
                "content_hash": ArtifactId("sha256:" + "0" * 64),
                "text_roots": (*retained_text.values(), *derived_text),
                "plan_roots": tuple(derived_plan),
                "planning_contexts": tuple(derived_contexts),
                "case_manifests": tuple(derived_cases),
            }
        )
        return provisional.model_copy(update={"content_hash": bundle_content_id(provisional)})

    def _text_root(self, directory: Path, namespace: StableId) -> TextRootDocument:
        if not directory.is_dir():
            raise HumanBenchmarkCompileError(f"text directory does not exist: {directory}")
        grouped: dict[int, list[tuple[str, str]]] = {}
        for path in sorted(directory.glob("*.txt")):
            index = 0 if path.name.startswith("000_") else int(path.stem)
            grouped.setdefault(index, []).append((path.name, path.read_text("utf-8")))
        if not grouped:
            raise HumanBenchmarkCompileError(f"text directory is empty: {directory}")
        prelude: PreludeDocument | None = None
        if prelude_documents := grouped.pop(0, None):
            prelude_id = StableId(f"prelude.{namespace.root}")
            prelude_scenes: list[SceneDocument] = []
            for scene_index, (filename, text) in enumerate(prelude_documents):
                text = self._strip_packaging_frontmatter(text)
                scene_id = StableId(f"scene.{namespace.root}.prelude.{scene_index}")
                prelude_scenes.append(
                    SceneDocument(
                        scene_id=scene_id,
                        scene_index=scene_index,
                        title=filename,
                        blocks=(
                            TextBlock(
                                block_id=StableId(f"block.{namespace.root}.prelude.{scene_index}"),
                                chapter_id=prelude_id,
                                scene_id=scene_id,
                                narrative_index=scene_index,
                                text=text,
                            ),
                        ),
                    )
                )
            prelude = PreludeDocument(
                prelude_id=prelude_id,
                title="Prelude",
                scenes=tuple(prelude_scenes),
            )
        chapters: list[ChapterDocument] = []
        for chapter_index, documents in sorted(grouped.items()):
            chapter_id = StableId(f"chapter.{namespace.root}.{chapter_index}")
            scenes: list[SceneDocument] = []
            for scene_index, (filename, text) in enumerate(documents):
                scene_id = StableId(f"scene.{namespace.root}.{chapter_index}.{scene_index}")
                block = TextBlock(
                    block_id=StableId(f"block.{namespace.root}.{chapter_index}.{scene_index}"),
                    chapter_id=chapter_id,
                    scene_id=scene_id,
                    narrative_index=scene_index,
                    text=text,
                )
                scenes.append(
                    SceneDocument(
                        scene_id=scene_id,
                        scene_index=scene_index,
                        title=filename,
                        blocks=(block,),
                    )
                )
            chapters.append(
                ChapterDocument(
                    chapter_id=chapter_id,
                    chapter_index=chapter_index,
                    scenes=tuple(scenes),
                )
            )
        provisional = TextRootDocument(
            root_hash=ArtifactId("sha256:" + "0" * 64),
            schema_version=self.version,
            prelude=prelude,
            chapters=tuple(chapters),
        )
        return provisional.model_copy(update={"root_hash": text_root_content_id(provisional)})

    def _gold_need_specs(
        self,
        raw_case: dict[str, Any],
        gold_data: dict[str, Any],
        root: Path,
    ) -> tuple[GoldNeedSpec, ...]:
        """Compile evaluator-only GoldNeedSpec from the sibling spec YAML.

        The spec file is optional: cases without it contribute no Need Recall
        components and are not scored on that segment.
        """

        gold_path = root / self._string(raw_case["gold_file_private"])
        spec_path = gold_path.with_name("gold_need_spec.yaml")
        if not spec_path.exists():
            return ()
        raw_spec = self._yaml(spec_path)
        items = raw_spec.get("items", [])
        if not isinstance(items, list):
            raise HumanBenchmarkCompileError("gold_need_spec items must be a list")
        by_id = {
            self._string(item["id"]): item
            for item in gold_data.get("items", [])
            if isinstance(item, dict)
        }
        compiled: list[GoldNeedSpec] = []
        for raw_item in items:
            if not isinstance(raw_item, dict):
                raise HumanBenchmarkCompileError("gold_need_spec item must be an object")
            gold_id = self._string(raw_item["id"])
            if gold_id not in by_id:
                raise HumanBenchmarkCompileError(
                    f"gold_need_spec references unknown Gold: {gold_id}"
                )
            raw_blindness = raw_item.get("blindness", "blind_recoverable")
            try:
                blindness = GoldBlindness(str(raw_blindness))
            except ValueError as error:
                raise HumanBenchmarkCompileError(
                    f"gold_need_spec blindness is invalid: {gold_id}/{raw_blindness}"
                ) from error
            compiled.append(
                GoldNeedSpec(
                    gold_id=StableId(gold_id),
                    blindness=blindness,
                    required_need_scopes=self._strings(raw_item.get("required_need_scopes", [])),
                    required_entities=self._strings(raw_item.get("required_entities", [])),
                    required_facets=self._strings(raw_item.get("required_facets", [])),
                )
            )
        return tuple(compiled)

    @staticmethod
    def _strip_packaging_frontmatter(value: str) -> str:
        """Exclude distributor metadata and synopsis before the first volume."""

        marker = re.search(
            r"(?im)^第[一二三四五六七八九十百千万0-9]+卷(?:\s|$)",
            value,
        )
        if marker is None:
            return value
        prefix = value[: marker.start()]
        packaging_markers = ("内容简介", "作者\uff1a", "本作品来自互联网", "全本校对")
        if not any(item in prefix for item in packaging_markers):
            return value
        return value[marker.start() :]

    def _compile_planning_context(
        self,
        case_id: StableId,
        raw: dict[str, Any],
        target_range: tuple[int, int],
    ) -> AuthorPlanningContext:
        """Compile the typed AuthorPlanningContext from the raw input.yaml.

        This is the single compile-time reader of the author-visible planning
        fields (task / mode / visible_outline / target_plan).  Raw YAML never
        enters any public payload; only the normalized context does.
        """

        mode = self._string(raw.get("mode", ""))
        try:
            profile = self._PROFILE_BY_MODE[mode]
        except KeyError as error:
            raise HumanBenchmarkCompileError(f"unsupported input mode: {mode}") from error
        task_intent = self._string(raw.get("task", ""))
        outlines = self._strings(raw.get("visible_outline", []))
        goals_raw = raw.get("target_plan", [])
        if not isinstance(goals_raw, list):
            raise HumanBenchmarkCompileError("target_plan must be a list")
        nodes = tuple(
            VisibleOutlineNode(
                node_id=StableId(f"plan.{case_id.root}.outline.{index}"),
                title=f"Visible outline {index}",
                summary=summary,
            )
            for index, summary in enumerate(outlines, start=1)
        )
        goals = tuple(
            ChapterGoal(
                goal_id=StableId(f"goal.{case_id.root}.{self._integer(raw_goal['chapter'])}"),
                chapter_index=self._integer(raw_goal["chapter"]),
                summary=self._string(raw_goal["goal"]),
            )
            for raw_goal in goals_raw
            if isinstance(raw_goal, dict)
        )
        provisional = AuthorPlanningContext(
            profile=profile,
            task_intent=task_intent,
            target_range=target_range,
            visible_outline_nodes=nodes,
            chapter_goals=goals,
            source_hash=ArtifactId("sha256:" + "0" * 64),
        )
        return provisional.model_copy(
            update={"source_hash": self._planning_context_source_hash(provisional)}
        )

    @staticmethod
    def _planning_context_source_hash(context: AuthorPlanningContext) -> ArtifactId:
        return content_id(
            {
                "profile": context.profile.value,
                "task_intent": context.task_intent,
                "target_range": context.target_range,
                "visible_outline_nodes": [
                    node.model_dump(mode="json") for node in context.visible_outline_nodes
                ],
                "chapter_goals": [goal.model_dump(mode="json") for goal in context.chapter_goals],
                "planner_may_read_plan": context.planner_may_read_plan,
            }
        )

    def _plan_root(self, case_id: StableId, raw: dict[str, Any]) -> PlanRootDocument:
        outlines = tuple(self._strings(raw.get("visible_outline", [])))
        goals_raw = raw.get("target_plan", [])
        if not isinstance(goals_raw, list):
            raise HumanBenchmarkCompileError("target_plan must be a list")
        nodes = tuple(
            PlanNode(
                plan_node_id=StableId(f"plan.{case_id.root}.outline.{index}"),
                node_type="visible_outline",
                title=f"Visible outline {index}",
                summary=summary,
            )
            for index, summary in enumerate(outlines, start=1)
        )
        goals = tuple(
            ChapterGoal(
                goal_id=StableId(f"goal.{case_id.root}.{self._integer(raw_goal['chapter'])}"),
                chapter_index=self._integer(raw_goal["chapter"]),
                summary=self._string(raw_goal["goal"]),
            )
            for raw_goal in goals_raw
            if isinstance(raw_goal, dict)
        )
        provisional = PlanRootDocument(
            root_hash=ArtifactId("sha256:" + "0" * 64),
            schema_version=self.version,
            nodes=nodes,
            chapter_goals=goals,
        )
        return provisional.model_copy(update={"root_hash": plan_root_content_id(provisional)})

    def _historical_evidence(
        self,
        raw_case: dict[str, Any],
        gold_data: dict[str, Any],
        root: TextRootDocument,
        commit: CommitId,
    ) -> tuple[
        dict[str, tuple[EvidenceRef, ...]],
        dict[str, tuple[EvidenceSet, ...]],
    ]:
        raw_refs = raw_case.get("gold_evidence_refs", {})
        if not isinstance(raw_refs, dict):
            raise HumanBenchmarkCompileError("gold_evidence_refs must be an object")
        raw_items = gold_data.get("items", [])
        if not isinstance(raw_items, list):
            raise HumanBenchmarkCompileError("Gold items must be a list")
        annotations = {
            self._string(item["id"]): item for item in raw_items if isinstance(item, dict)
        }
        result: dict[str, tuple[EvidenceRef, ...]] = {}
        accepted: dict[str, tuple[EvidenceSet, ...]] = {}
        for gold_id, chapter_refs in raw_refs.items():
            if not isinstance(gold_id, str) or not isinstance(chapter_refs, list):
                raise HumanBenchmarkCompileError("gold evidence mapping is malformed")
            annotation = annotations.get(gold_id, {})
            raw_sets = annotation.get("accepted_evidence_sets")
            if raw_sets is not None:
                if not isinstance(raw_sets, list) or not raw_sets:
                    raise HumanBenchmarkCompileError(
                        f"accepted evidence sets must be a non-empty list: {gold_id}"
                    )
                compiled_sets: list[EvidenceSet] = []
                flattened: list[EvidenceRef] = []
                for set_index, raw_set in enumerate(raw_sets, start=1):
                    if not isinstance(raw_set, dict):
                        raise HumanBenchmarkCompileError(
                            f"accepted evidence set must be an object: {gold_id}"
                        )
                    raw_evidence = raw_set.get("evidence")
                    if not isinstance(raw_evidence, list) or not raw_evidence:
                        raise HumanBenchmarkCompileError(
                            f"accepted evidence set requires evidence: {gold_id}"
                        )
                    refs = tuple(
                        self._annotated_evidence(
                            raw_ref,
                            root,
                            commit,
                            namespace=f"history.{gold_id}.set{set_index}.ref{ref_index}",
                        )
                        for ref_index, raw_ref in enumerate(raw_evidence, start=1)
                    )
                    flattened.extend(refs)
                    raw_components = raw_set.get(
                        "components", annotation.get("target_components", [])
                    )
                    if not isinstance(raw_components, list):
                        raise HumanBenchmarkCompileError(
                            f"accepted evidence components must be a list: {gold_id}"
                        )
                    set_id = str(raw_set.get("id", f"accepted.{gold_id}.{set_index}"))
                    compiled_sets.append(
                        EvidenceSet(
                            evidence_set_id=StableId(set_id),
                            evidence_refs=refs,
                            component_ids=tuple(
                                self._string(component) for component in raw_components
                            ),
                        )
                    )
                result[gold_id] = tuple(dict.fromkeys(flattened))
                accepted[gold_id] = tuple(compiled_sets)
                continue
            evidence: list[EvidenceRef] = []
            for chapter_ref in chapter_refs:
                if isinstance(chapter_ref, str):
                    continue
                chapter_index = self._integer(chapter_ref)
                if chapter_index == 0:
                    if root.prelude is None or not root.prelude.scenes:
                        raise HumanBenchmarkCompileError(f"evidence prelude is absent: {gold_id}")
                    scene = root.prelude.scenes[-1]
                    evidence.append(
                        self._block_evidence(
                            scene.blocks[0], root.root_hash, commit, f"history.{gold_id}"
                        )
                    )
                    continue
                chapter = next(
                    (item for item in root.chapters if item.chapter_index == chapter_index),
                    None,
                )
                if chapter is None:
                    raise HumanBenchmarkCompileError(
                        f"evidence chapter is absent: {gold_id}/{chapter_index}"
                    )
                scene = chapter.scenes[-1]
                evidence.append(
                    self._block_evidence(
                        scene.blocks[0], root.root_hash, commit, f"history.{gold_id}"
                    )
                )
            result[gold_id] = tuple(evidence)
        return result, accepted

    def _gold(
        self,
        raw_case: dict[str, Any],
        gold_data: dict[str, Any],
        historical: dict[str, tuple[EvidenceRef, ...]],
        accepted: dict[str, tuple[EvidenceSet, ...]],
        future: tuple[EvidenceRef, ...],
    ) -> tuple[tuple[GoldItem, ...], tuple[GoldItem, ...]]:
        raw_items = gold_data.get("items", [])
        if not isinstance(raw_items, list):
            raise HumanBenchmarkCompileError("Gold items must be a list")
        by_id = {self._string(item["id"]): item for item in raw_items if isinstance(item, dict)}
        targets = tuple(range(*self._inclusive_range(raw_case["target_range"])))

        def compile_ids(field: str, kind: GoldKind) -> tuple[GoldItem, ...]:
            values = raw_case.get(field, [])
            if not isinstance(values, list):
                raise HumanBenchmarkCompileError(f"{field} must be a list")
            compiled: list[GoldItem] = []
            for raw_id in values:
                identity = self._string(raw_id)
                item = by_id.get(identity)
                evidence = historical.get(identity, ())
                if item is None or not evidence:
                    raise HumanBenchmarkCompileError(f"Gold item lacks source evidence: {identity}")
                compiled.append(
                    GoldItem(
                        gold_id=StableId(identity),
                        kind=kind,
                        description=self._string(item["fact"]),
                        target_chapters=targets,
                        evidence_refs=evidence,
                        future_evidence_refs=future,
                        mandatory=bool(item.get("mandatory", False)),
                        gold_type=self._gold_type(item),
                        fact=self._string(item["fact"]),
                        why_needed=self._string(
                            item.get("why_needed", "required by the benchmark annotation")
                        ),
                        weight=float(item.get("weight", 1.0)),
                        applicable_profiles=self._applicable_profiles(item),
                        accepted_evidence_sets=accepted.get(
                            identity,
                            (
                                EvidenceSet(
                                    evidence_set_id=StableId(f"accepted.{identity}.historical"),
                                    evidence_refs=evidence,
                                    component_ids=tuple(
                                        str(value) for value in item.get("target_components", [])
                                    ),
                                ),
                            ),
                        ),
                        target_components=tuple(
                            str(value) for value in item.get("target_components", [])
                        ),
                    )
                )
            return tuple(compiled)

        return (
            compile_ids("observed_use_gold", GoldKind.OBSERVED_USE),
            compile_ids("operational_constraint_gold", GoldKind.OPERATIONAL_CONSTRAINT),
        )

    def _annotated_evidence(
        self,
        raw: Any,
        root: TextRootDocument,
        commit: CommitId,
        *,
        namespace: str,
    ) -> EvidenceRef:
        if not isinstance(raw, dict):
            raise HumanBenchmarkCompileError(f"reviewed evidence must be an object: {namespace}")
        chapter_index = self._integer(raw.get("chapter"))
        quote = self._string(raw.get("quote"))
        if chapter_index == 0:
            if root.prelude is None:
                raise HumanBenchmarkCompileError(
                    f"reviewed evidence prelude is absent: {namespace}"
                )
            blocks = tuple(block for scene in root.prelude.scenes for block in scene.blocks)
        else:
            chapter = next(
                (item for item in root.chapters if item.chapter_index == chapter_index),
                None,
            )
            if chapter is None:
                raise HumanBenchmarkCompileError(
                    f"reviewed evidence chapter is absent: {namespace}/{chapter_index}"
                )
            blocks = tuple(block for scene in chapter.scenes for block in scene.blocks)
        matches: list[tuple[TextBlock, int]] = []
        for block in blocks:
            start = block.text.find(quote)
            while start >= 0:
                matches.append((block, start))
                start = block.text.find(quote, start + 1)
        if len(matches) != 1:
            raise HumanBenchmarkCompileError(
                "reviewed evidence quote must occur exactly once in its chapter: "
                f"{namespace} found={len(matches)}"
            )
        block, start = matches[0]
        end = start + len(quote)
        suffix = sha256_id(f"{namespace}:{block.block_id.root}:{start}:{end}".encode()).root[-24:]
        return EvidenceRef(
            evidence_id=StableId(f"evidence.{suffix}"),
            root_hash=root.root_hash,
            object_hash=sha256_id(block.text.encode("utf-8")),
            chapter_id=block.chapter_id,
            scene_id=block.scene_id,
            span=TextSpanRef(block_id=block.block_id, start=start, end=end),
            quote_hash=quote_hash(quote),
            support_status=EvidenceSupportStatus.CURRENT,
            resolved_at_commit=commit,
        )

    def _plan_gold(
        self,
        raw_case: dict[str, Any],
        plan: PlanRootDocument,
        future: TextRootDocument,
        source_commit: CommitId,
    ) -> tuple[GoldItem, ...]:
        raw_ids = raw_case.get("plan_obligation_gold", [])
        if not isinstance(raw_ids, list):
            raise HumanBenchmarkCompileError("plan_obligation_gold must be a list")
        raw_refs = raw_case.get("gold_evidence_refs", {})
        if not isinstance(raw_refs, dict):
            raise HumanBenchmarkCompileError("gold_evidence_refs must be an object")
        goals = {goal.chapter_index: goal for goal in plan.chapter_goals}
        future_by_chapter = {chapter.chapter_index: chapter for chapter in future.chapters}
        compiled: list[GoldItem] = []
        for raw_id in raw_ids:
            identity = self._string(raw_id)
            references = raw_refs.get(identity)
            if not isinstance(references, list) or not references:
                raise HumanBenchmarkCompileError(
                    f"plan Gold lacks explicit manifest evidence references: {identity}"
                )
            chapters: list[int] = []
            for reference in references:
                if not isinstance(reference, str) or not reference.startswith("plan:"):
                    raise HumanBenchmarkCompileError(
                        f"plan Gold reference must use plan:<chapter>: {identity}"
                    )
                try:
                    chapter = int(reference.removeprefix("plan:"))
                except ValueError as error:
                    raise HumanBenchmarkCompileError(
                        f"plan Gold reference chapter is invalid: {identity}/{reference}"
                    ) from error
                if chapter in chapters:
                    raise HumanBenchmarkCompileError(
                        f"plan Gold repeats a goal reference: {identity}/{reference}"
                    )
                chapters.append(chapter)
            referenced_goals = tuple(goals.get(chapter) for chapter in chapters)
            if any(goal is None for goal in referenced_goals) or any(
                chapter not in future_by_chapter for chapter in chapters
            ):
                raise HumanBenchmarkCompileError(
                    f"plan Gold references a missing goal or target chapter: {identity}"
                )
            resolved_goals = tuple(goal for goal in referenced_goals if goal is not None)
            future_evidence = tuple(
                self._block_evidence(
                    block,
                    future.root_hash,
                    source_commit,
                    f"future-plan.{identity}",
                )
                for chapter in chapters
                for scene in future_by_chapter[chapter].scenes
                for block in scene.blocks
            )
            compiled.append(
                GoldItem(
                    gold_id=StableId(identity),
                    kind=GoldKind.PLAN_OBLIGATION,
                    description="; ".join(goal.summary for goal in resolved_goals),
                    target_chapters=tuple(chapters),
                    plan_evidence_refs=tuple(
                        PlanEvidenceRef(
                            evidence_id=StableId(f"plan-evidence.{identity}.{goal.chapter_index}"),
                            plan_root_hash=plan.root_hash,
                            goal_id=goal.goal_id,
                            object_hash=content_id(goal.model_dump(mode="json")),
                        )
                        for goal in resolved_goals
                    ),
                    future_evidence_refs=future_evidence,
                    mandatory=True,
                    gold_type=GoldType.PLAN_OBLIGATION,
                    fact="; ".join(goal.summary for goal in resolved_goals),
                    why_needed="author-visible plan obligation applies to the target horizon",
                    weight=1.0,
                    applicable_profiles=(BenchmarkInformationProfile.AUTHOR_PLAN_CONDITIONED,),
                    accepted_evidence_sets=(
                        EvidenceSet(
                            evidence_set_id=StableId(f"accepted.{identity}.plan"),
                            plan_node_ids=tuple(
                                StableId(
                                    "plan.bootstrap.rough-story-outline.range."
                                    f"{((goal.chapter_index - 1) // 20) * 20 + 1}-"
                                    f"{((goal.chapter_index - 1) // 20 + 1) * 20}"
                                )
                                for goal in resolved_goals
                            ),
                        ),
                    ),
                    target_components=tuple(goal.goal_id.root for goal in resolved_goals),
                )
            )
        return tuple(compiled)

    def _gold_type(self, item: dict[str, Any]) -> GoldType:
        raw = self._string(item.get("type", "CAUSAL_HISTORY")).upper()
        try:
            return self._GOLD_TYPE_MAP[raw]
        except KeyError as error:
            raise HumanBenchmarkCompileError(f"unsupported Gold type: {raw}") from error

    @staticmethod
    def _applicable_profiles(
        item: dict[str, Any],
    ) -> tuple[BenchmarkInformationProfile, ...]:
        raw = item.get("applicable_profiles")
        if raw is None:
            return tuple(BenchmarkInformationProfile)
        if not isinstance(raw, list) or not raw:
            raise HumanBenchmarkCompileError("applicable_profiles must be a non-empty list")
        try:
            return tuple(BenchmarkInformationProfile(str(value)) for value in raw)
        except ValueError as error:
            raise HumanBenchmarkCompileError(
                "Gold item contains an invalid information profile"
            ) from error

    def _world_root(
        self,
        case_id: StableId,
        commit: CommitId,
        gold: tuple[GoldItem, ...],
    ) -> WorldRootDocument:
        entity = Entity(
            entity_id=StableId(f"entity.{case_id.root}.oracle"),
            entity_type="benchmark_oracle",
            internal_label=case_id.root,
        )
        states = tuple(
            StateRecord(
                state_id=StableId(f"state.{item.gold_id.root}"),
                subject_id=entity.entity_id,
                predicate=item.kind.value,
                value=item.description,
                valid_time=StoryTime(worldline="main"),
                evidence_refs=item.evidence_refs,
                truth_class=TruthClass.ACCEPTED_WORLD_FACT,
            )
            for item in gold
        )
        provisional = WorldRootDocument(
            root_hash=ArtifactId("sha256:" + "0" * 64),
            schema_version=self.version,
            source_commit=commit,
            entities=(entity,),
            states=states,
        )
        return provisional.model_copy(update={"root_hash": world_root_content_id(provisional)})

    def _all_evidence(
        self,
        root: TextRootDocument,
        commit: CommitId,
        namespace: str,
    ) -> tuple[EvidenceRef, ...]:
        return tuple(
            self._block_evidence(block, root.root_hash, commit, namespace)
            for scene in (
                *(root.prelude.scenes if root.prelude is not None else ()),
                *(scene for chapter in root.chapters for scene in chapter.scenes),
            )
            for block in scene.blocks
        )

    @staticmethod
    def _block_evidence(
        block: TextBlock,
        root_hash: ArtifactId,
        commit: CommitId,
        namespace: str,
    ) -> EvidenceRef:
        suffix = sha256_id(f"{namespace}:{block.block_id.root}".encode()).root[-24:]
        return EvidenceRef(
            evidence_id=StableId(f"evidence.{suffix}"),
            root_hash=root_hash,
            object_hash=sha256_id(block.text.encode("utf-8")),
            chapter_id=block.chapter_id,
            scene_id=block.scene_id,
            span=TextSpanRef(block_id=block.block_id, start=0, end=len(block.text)),
            quote_hash=quote_hash(block.text),
            support_status=EvidenceSupportStatus.CURRENT,
            resolved_at_commit=commit,
        )

    @staticmethod
    def _json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HumanBenchmarkCompileError(f"cannot read JSON: {path}") from error
        if not isinstance(value, dict):
            raise HumanBenchmarkCompileError(f"JSON root must be an object: {path}")
        return value

    @staticmethod
    def _yaml(path: Path) -> dict[str, Any]:
        try:
            value = yaml.safe_load(path.read_text("utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise HumanBenchmarkCompileError(f"cannot read YAML: {path}") from error
        if not isinstance(value, dict):
            raise HumanBenchmarkCompileError(f"YAML root must be an object: {path}")
        return value

    @staticmethod
    def _string(value: Any) -> str:
        if not isinstance(value, str) or not value:
            raise HumanBenchmarkCompileError("expected a non-empty string")
        return value

    @classmethod
    def _strings(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, list):
            raise HumanBenchmarkCompileError("expected a string list")
        return tuple(cls._string(item) for item in value)

    @staticmethod
    def _integer(value: Any) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise HumanBenchmarkCompileError("expected an integer")
        return value

    @classmethod
    def _range(cls, value: Any) -> tuple[int, int]:
        if not isinstance(value, list) or len(value) != 2:
            raise HumanBenchmarkCompileError("expected a two-item range")
        return cls._integer(value[0]), cls._integer(value[1])

    @classmethod
    def _inclusive_range(cls, value: Any) -> tuple[int, int]:
        start, end = cls._range(value)
        return start, end + 1
