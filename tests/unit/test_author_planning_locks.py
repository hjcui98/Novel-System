"""Author planning locks must reach both the Planner and the Writer surfaces."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from novel_agent.agents.planner import (
    BOOTSTRAP_UNRESOLVED_LIMIT,
    _DevelopCandidatesPlannerProposalDraft,
)
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.author_constraints import (
    AuthorConstraintCategory,
    compile_author_constraint_root,
)
from novel_agent.domain.ids import ArtifactId, SchemaVersion, StableId
from novel_agent.domain.planning_locks import (
    ROOT_HASH_PLACEHOLDER,
    AuthorPlanningLock,
    PlanningLockCategory,
    compile_planning_lock_channels,
    load_author_planning_locks,
)
from novel_agent.domain.stage2 import (
    ContractRef,
    ProjectProfileRootDocument,
    PromptContractRef,
    SkillContractRef,
)
from novel_agent.domain.world import PlanLevel, PlanNode
from novel_agent.services.bootstrap_workflow import project_profile_root_content_id
from novel_agent.services.content_addressing import content_id

SCHEMA_VERSION = SchemaVersion("2.0.0")


def _lock_document() -> dict[str, object]:
    return {
        "schema_version": "2.0.0",
        "project_id": "project.yujin-jiuxu.v7",
        "story_title": "余烬九序",
        "locks": [
            {
                "lock_id": "lock.inner-court.101",
                "category": "timeline",
                "description": "斩星府内府资格不得早于第二卷",
                "anchor": "第二卷",
                "not_before_chapter": 101,
                "chapter_latest": 200,
                "satisfies": "第一卷不得出现内府身份推进",
            },
            {
                "lock_id": "lock.er07.201",
                "category": "reveal",
                "description": "第三碎片与陆远线索实质揭露最早第三卷",
                "not_before_chapter": 201,
                "chapter_latest": 300,
            },
            {
                "lock_id": "lock.long-truth.350",
                "category": "timeline",
                "description": "长程真相最早第四卷后段起暗示",
                "not_before_chapter": 350,
                "chapter_latest": 500,
            },
            {
                "lock_id": "lock.copper-token.100",
                "category": "equipment",
                "description": "第一卷末获得铜铭",
                "chapter_earliest": 90,
                "chapter_latest": 100,
            },
        ],
    }


def test_load_derives_content_addressed_root_hash() -> None:
    document = load_author_planning_locks(_lock_document(), schema_version=SCHEMA_VERSION)

    assert document.root_hash != ROOT_HASH_PLACEHOLDER
    assert document.root_hash == content_id(
        {
            "schema_version": "2.0.0",
            "project_id": "project.yujin-jiuxu.v7",
            "locks": tuple(item.model_dump(mode="json") for item in document.locks),
        }
    )


def test_load_rejects_stale_root_hash() -> None:
    payload = {**_lock_document(), "root_hash": "sha256:" + "a" * 64}

    with pytest.raises(ValueError, match="root_hash does not match"):
        load_author_planning_locks(payload, schema_version=SCHEMA_VERSION)


def test_load_rejects_schema_version_drift() -> None:
    with pytest.raises(ValueError, match="schema version mismatch"):
        load_author_planning_locks(_lock_document(), schema_version=SchemaVersion("1.0.0"))


def test_load_stamps_hash_after_edit_so_hash_tracks_content() -> None:
    payload = _lock_document()
    first = load_author_planning_locks(payload, schema_version=SCHEMA_VERSION)
    payload["locks"] = [
        *payload["locks"],
        {  # type: ignore[list-item]
            "lock_id": "lock.forbidden-core.100",
            "category": "progression",
            "description": "断星六号核心回收不得在第一卷完成",
            "not_before_chapter": 101,
            "chapter_latest": 200,
        },
    ]
    second = load_author_planning_locks(payload, schema_version=SCHEMA_VERSION)

    assert second.root_hash != first.root_hash


def test_timeline_lock_requires_declared_window() -> None:
    with pytest.raises(ValidationError, match="explicit not_before_chapter"):
        AuthorPlanningLock(
            lock_id=StableId("lock.bad"),
            category=PlanningLockCategory.TIMELINE,
            description="缺少边界",
        )


def test_reversed_window_is_rejected() -> None:
    with pytest.raises(ValidationError, match="reversed"):
        AuthorPlanningLock(
            lock_id=StableId("lock.bad"),
            category=PlanningLockCategory.REVEAL,
            description="反向窗口",
            chapter_earliest=300,
            chapter_latest=100,
        )


def test_duplicate_lock_ids_are_rejected() -> None:
    payload = _lock_document()
    duplicated = payload["locks"][0]  # type: ignore[index]
    payload["locks"] = [duplicated, duplicated]  # type: ignore[list-item]

    with pytest.raises(ValidationError, match="must be unique"):
        load_author_planning_locks(payload, schema_version=SCHEMA_VERSION)


def test_channels_map_every_lock_with_absolute_boundaries() -> None:
    document = load_author_planning_locks(_lock_document(), schema_version=SCHEMA_VERSION)

    channels = compile_planning_lock_channels(document)

    assert [item["not_before_chapter"] for item in channels["timeline_locks"]] == [101, 350]
    assert [item["not_before_chapter"] for item in channels["reveal_windows"]] == [201]
    assert channels["equipment_locks"] == [
        {
            "lock_id": "lock.copper-token.100",
            "category": "equipment",
            "description": "第一卷末获得铜铭",
            "chapter_start": 90,
            "chapter_end": 100,
        }
    ]
    assert channels["location_preconditions"] == []


def _profile(capability: dict[str, object]) -> ProjectProfileRootDocument:
    contract = ContractRef(
        contract_id=StableId("agent.production-bootstrap"),
        version=SCHEMA_VERSION,
        content_hash=content_id({"bootstrap": "profile"}),
    )
    provisional = ProjectProfileRootDocument(
        root_hash=ArtifactId("sha256:" + "0" * 64),
        schema_version=SCHEMA_VERSION,
        style_profile={"language": "zh-CN"},
        capability_profile=capability,  # type: ignore[arg-type]
        agent_specs=(contract,),
        prompt_contracts=(
            PromptContractRef(
                contract_id=StableId("prompt.system-policy"),
                version=SCHEMA_VERSION,
                content_hash=contract.content_hash,
                render_fingerprint=contract.content_hash,
            ),
        ),
        skill_contracts=(
            SkillContractRef(
                contract_id=StableId("skill.scene-composition"),
                version=SCHEMA_VERSION,
                content_hash=contract.content_hash,
            ),
        ),
        tool_policies=(contract,),
        model_profiles=("qwen38-27b-nvfp4@8003",),
    )
    return provisional.model_copy(
        update={"root_hash": project_profile_root_content_id(provisional)}
    )


def test_apply_planning_locks_seats_author_channel_in_capability_profile() -> None:
    from novel_agent.runtime.production_novel_bootstrap import apply_author_planning_locks

    document = load_author_planning_locks(_lock_document(), schema_version=SCHEMA_VERSION)
    source = ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "b" * 64),
        byte_length=128,
        media_type="application/vnd.novel-agent.author-planning-locks+json",
        schema_version=SCHEMA_VERSION,
    )
    capability: dict[str, object] = {
        "planning_constraints": {"timeline_locks": [{"description": "模型自行猜测"}]}
    }

    apply_author_planning_locks(capability, document, source=source)  # type: ignore[arg-type]

    planning = capability["planning_constraints"]
    assert isinstance(planning, dict)
    assert [item["not_before_chapter"] for item in planning["timeline_locks"]] == [101, 350]
    assert capability["planning_lock_count"] == 4
    assert capability["planning_lock_root"] == document.root_hash.root
    assert capability["planning_lock_source"] == source.artifact_id.root


def test_compiled_channels_reach_the_author_constraint_root() -> None:
    from novel_agent.runtime.production_novel_bootstrap import apply_author_planning_locks

    document = load_author_planning_locks(_lock_document(), schema_version=SCHEMA_VERSION)
    capability: dict[str, object] = {}
    apply_author_planning_locks(capability, document)  # type: ignore[arg-type]
    profile = _profile(capability)
    profile_ref = ArtifactRef(
        artifact_id=profile.root_hash,
        byte_length=1,
        media_type="application/json",
        schema_version=SCHEMA_VERSION,
    )

    root = compile_author_constraint_root(profile=profile, profile_ref=profile_ref)

    assert {item.category for item in root.constraints} == {
        AuthorConstraintCategory.LANGUAGE,
        AuthorConstraintCategory.TIME_LOCK,
        AuthorConstraintCategory.REVEAL_WINDOW,
        AuthorConstraintCategory.EQUIPMENT_MILESTONE,
    }
    timeline = [
        item for item in root.constraints if item.category is AuthorConstraintCategory.TIME_LOCK
    ]
    assert [item.not_before_chapter for item in timeline] == [101, 350]


def test_bootstrap_provider_schema_bounds_the_unresolved_array() -> None:
    """A looping decoder must be stopped by the grammar, not by the token budget."""

    schema = _DevelopCandidatesPlannerProposalDraft.model_json_schema()

    unresolved = schema["properties"]["unresolved"]
    assert unresolved["maxItems"] == BOOTSTRAP_UNRESOLVED_LIMIT
    assert schema["required"] == ["mode", "strategy", "coverage"]


def test_bootstrap_unresolved_limit_is_still_generous_enough_for_real_gaps() -> None:
    assert BOOTSTRAP_UNRESOLVED_LIMIT >= 16


def test_bootstrap_draft_rejects_more_unresolved_entries_than_the_schema_allows() -> None:
    payload = {
        "mode": "project_bootstrap",
        "strategy": "develop_candidates",
        "project_intent_items": [
            {
                "item_id": "intent.author-brief",
                "kind": "author_brief",
                "payload": {"summary": "余烬九序"},
                "provenance": "author_supplied",
                "source_ids": ["source.author-initial-brief"],
            }
        ],
        "unresolved": [f"未明确项 {index}" for index in range(BOOTSTRAP_UNRESOLVED_LIMIT + 1)],
        "coverage": 1.0,
    }

    with pytest.raises(ValidationError, match="at most 24 items"):
        _DevelopCandidatesPlannerProposalDraft.model_validate_json(
            json.dumps(payload, ensure_ascii=False)
        )

    accepted = _DevelopCandidatesPlannerProposalDraft.model_validate_json(
        json.dumps(
            {**payload, "unresolved": payload["unresolved"][:BOOTSTRAP_UNRESOLVED_LIMIT]},
            ensure_ascii=False,
        )
    )
    assert len(accepted.unresolved) == BOOTSTRAP_UNRESOLVED_LIMIT


def test_lock_without_any_boundary_is_rejected() -> None:
    """A boundary-less lock compiles to nothing, so it must not load."""

    payload = _lock_document()
    payload["locks"] = [  # type: ignore[list-item]
        {
            "lock_id": "lock.no-boundary",
            "category": "reveal",
            "description": "只说'后期'但没有任何章节边界",
        }
    ]

    with pytest.raises(ValidationError, match="at least one chapter boundary"):
        load_author_planning_locks(payload, schema_version=SCHEMA_VERSION)


def test_reveal_window_without_latest_still_carries_its_lower_boundary() -> None:
    payload = _lock_document()
    payload["locks"] = [  # type: ignore[list-item]
        {
            "lock_id": "lock.long-truth.350",
            "category": "reveal",
            "description": "长程真相最早第四卷后段起暗示",
            "not_before_chapter": 350,
        }
    ]
    document = load_author_planning_locks(payload, schema_version=SCHEMA_VERSION)

    channels = compile_planning_lock_channels(document)

    assert channels["reveal_windows"] == [
        {
            "lock_id": "lock.long-truth.350",
            "category": "reveal",
            "description": "长程真相最早第四卷后段起暗示",
            "not_before_chapter": 350,
        }
    ]


def _writer_profile(capability: dict[str, object]) -> ProjectProfileRootDocument:
    """A minimal pinned profile the Writer lock projection can read."""

    contract = ContractRef(
        contract_id=StableId("agent.production-bootstrap"),
        version=SCHEMA_VERSION,
        content_hash=content_id({"bootstrap": "profile"}),
    )
    provisional = ProjectProfileRootDocument(
        root_hash=ArtifactId("sha256:" + "0" * 64),
        schema_version=SCHEMA_VERSION,
        style_profile={"language": "zh-CN"},
        capability_profile=capability,  # type: ignore[arg-type]
        agent_specs=(contract,),
        prompt_contracts=(
            PromptContractRef(
                contract_id=StableId("prompt.system-policy"),
                version=SCHEMA_VERSION,
                content_hash=contract.content_hash,
                render_fingerprint=contract.content_hash,
            ),
        ),
        skill_contracts=(
            SkillContractRef(
                contract_id=StableId("skill.scene-composition"),
                version=SCHEMA_VERSION,
                content_hash=contract.content_hash,
            ),
        ),
        tool_policies=(contract,),
        model_profiles=("qwen38-27b-nvfp4@8003",),
    )
    return provisional.model_copy(
        update={"root_hash": project_profile_root_content_id(provisional)}
    )


def test_every_compiled_lock_channel_reaches_the_writer() -> None:
    """Equipment and location locks were compiled but never projected."""

    from novel_agent.adapters.runtime.stage3_writer import ProductionWritingRequestFactory
    from novel_agent.runtime.production_novel_bootstrap import apply_author_planning_locks

    payload = _lock_document()
    payload["locks"] = [  # type: ignore[list-item]
        {
            "lock_id": "lock.equipment",
            "category": "equipment",
            "description": "铜铭须在第一卷末取得",
            "chapter_earliest": 90,
            "chapter_latest": 100,
        },
        {
            "lock_id": "lock.location",
            "category": "location",
            "description": "断星六号内府区域最早第二卷开放",
            "not_before_chapter": 101,
        },
        {
            "lock_id": "lock.reveal",
            "category": "reveal",
            "description": "第三碎片最早第三卷",
            "not_before_chapter": 201,
        },
    ]
    document = load_author_planning_locks(payload, schema_version=SCHEMA_VERSION)
    capability: dict[str, object] = {}
    apply_author_planning_locks(capability, document)  # type: ignore[arg-type]

    constraints, forbids = ProductionWritingRequestFactory._profile_lock_constraints(
        _writer_profile(capability), 1
    )

    joined = " ".join(constraints)
    assert "equipment_locks" in joined
    assert "location_preconditions" in joined
    assert "reveal_windows" in joined
    assert any("locked until chapter 101" in item for item in forbids)
    assert any("locked until chapter 201" in item for item in forbids)


def test_a_lock_deadline_is_not_reported_as_a_lock() -> None:
    """A latest chapter is a deadline; only a lower bound is a lock."""

    from novel_agent.adapters.runtime.stage3_writer import ProductionWritingRequestFactory
    from novel_agent.runtime.production_novel_bootstrap import apply_author_planning_locks

    payload = _lock_document()
    payload["locks"] = [  # type: ignore[list-item]
        {
            "lock_id": "lock.copper-token.100",
            "category": "equipment",
            "description": "第一卷末获得铜铭",
            "chapter_earliest": 90,
            "chapter_latest": 100,
        }
    ]
    document = load_author_planning_locks(payload, schema_version=SCHEMA_VERSION)
    capability: dict[str, object] = {}
    apply_author_planning_locks(capability, document)  # type: ignore[arg-type]
    profile = _writer_profile(capability)

    _, before = ProductionWritingRequestFactory._profile_lock_constraints(profile, 1)
    _, after = ProductionWritingRequestFactory._profile_lock_constraints(profile, 120)

    assert any("locked until chapter 90" in item for item in before)
    assert not any("deadline" in item for item in before)
    assert any("past its chapter 100 deadline" in item for item in after)


def test_volume_stage_slots_reach_the_writer_in_every_declared_shape() -> None:
    """A volume's entry/exit/ceiling slots must not be lost by their JSON shape."""

    from novel_agent.adapters.runtime.stage3_writer import (
        VolumeStageSlot,
        _stage_slot_entries,
    )

    reader = _stage_slot_entries

    assert reader({"entry_conditions": "必须已取得铜铭"}, "entry_conditions") == (
        VolumeStageSlot(body="必须已取得铜铭"),
    )
    assert reader(
        {"exit_conditions": ["断星六号外围记载已取得", "内府资格尚未获得"]},
        "exit_conditions",
    ) == (
        VolumeStageSlot(body="断星六号外围记载已取得"),
        VolumeStageSlot(body="内府资格尚未获得"),
    )
    assert reader(
        {
            "capability_ceiling": {
                "description": "本卷不得突破三阶开脉",
                "chapter_start": 1,
                "chapter_end": 100,
            }
        },
        "capability_ceiling",
    ) == (VolumeStageSlot(body="本卷不得突破三阶开脉", chapter_start=1, chapter_end=100),)
    assert reader({"reveal_window": None}, "reveal_window") == ()


def _stage_node(*, start: int, end: int, payload: dict[str, object]) -> PlanNode:
    return PlanNode(
        plan_node_id=StableId("node.volume.1"),
        node_type=PlanLevel.ARC_VOLUME.value,
        title="第一卷",
        summary="第一卷",
        plan_level=PlanLevel.ARC_VOLUME,
        chapter_start=start,
        chapter_end=end,
        payload=payload,  # type: ignore[arg-type]
    )


def test_volume_stage_grid_binds_free_text_slots_to_their_own_stage() -> None:
    """Ten non-empty fields are not a stage grid: opening, middle and closing differ."""

    from novel_agent.adapters.runtime.stage3_writer import _volume_stage_constraints

    node = _stage_node(
        start=1,
        end=9,
        payload={
            "entry_conditions": "必须已取得铜铭",
            "exit_conditions": "内府资格已获得",
            "capability_ceiling": "不得突破三阶开脉",
        },
    )
    project = _volume_stage_constraints

    opening = project([node], 1)
    middle = project([node], 5)
    closing = project([node], 9)

    assert any(
        item.startswith("当前卷阶段[卷首目标:entry_conditions]：必须已取得铜铭")  # noqa: RUF001
        for item in opening
    )
    assert "当前卷阶段[整卷:capability_ceiling]：不得突破三阶开脉" in opening  # noqa: RUF001
    assert any("出口未到期" in item for item in opening)

    assert (
        "当前卷阶段[卷中·入口待核实:entry_conditions]：必须已取得铜铭"  # noqa: RUF001
        "（按已接纳计划本项应已建立，属于计划要求而非既成事实；"  # noqa: RUF001
        "本章只能使用已在当前事实中核实到的部分，不得把未核实项当作已经发生）" in middle  # noqa: RUF001
    )
    # Stage position is not evidence: a passed opening slot must never be
    # rendered as an established fact.
    assert not any("入口已成立" in item for item in middle)
    assert any("出口未到期" in item for item in middle)

    assert "当前卷阶段[卷尾:exit_conditions]：内府资格已获得" in closing  # noqa: RUF001
    assert (
        "当前卷阶段[卷尾·入口待核实:entry_conditions]：必须已取得铜铭"  # noqa: RUF001
        "（按已接纳计划本项应已建立，属于计划要求而非既成事实；"  # noqa: RUF001
        "本章只能使用已在当前事实中核实到的部分，不得把未核实项当作已经发生）" in closing  # noqa: RUF001
    )
    assert not any("入口已成立" in item for item in closing)
    assert "当前卷阶段[整卷:capability_ceiling]：不得突破三阶开脉" in closing  # noqa: RUF001
    assert opening != middle != closing


def test_a_declared_stage_window_outranks_the_slot_scope() -> None:
    """A structured entry binds exactly inside the window the plan declared for it."""

    from novel_agent.adapters.runtime.stage3_writer import _volume_stage_constraints

    node = _stage_node(
        start=1,
        end=9,
        payload={
            "entry_conditions": "必须已取得铜铭",
            "exit_conditions": [
                {
                    "description": "前段伏笔已埋设",
                    "chapter_start": 4,
                    "chapter_end": 6,
                    "obligation_ids": ["obligation.thread"],
                },
                "内府资格已获得",
            ],
        },
    )
    project = _volume_stage_constraints

    early = project([node], 2)
    inside = project([node], 5)
    late = project([node], 9)

    assert not any("前段伏笔已埋设" in item for item in early)
    assert "当前卷阶段[卷中·窗口4-6:exit_conditions]：前段伏笔已埋设" in inside  # noqa: RUF001
    assert not any("前段伏笔已埋设" in item for item in late)
    assert "当前卷阶段[卷尾:exit_conditions]：内府资格已获得" in late  # noqa: RUF001
