"""U3.5 Temporal feasibility spike: NS-side identities, store, and public payload policy.

Temporal SDK types stay in ``temporal_langgraph_spike.py`` so this module can load
without a Temporal extra. Production assembly is not imported.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from novel_agent.domain.base import DomainModel
from novel_agent.domain.ids import ProjectId, RunId, StableId, TaskId

SPIKE_TASK_QUEUE = "ns-u35-spike"
SPIKE_NAMESPACE = "ns-u35-spike"
FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "gold",
        "raw_answer",
        "raw_response",
        "raw_content",
        "target_text",
        "body",
        "chapter_text",
        "private_text",
        "author_plan",
        "future_text",
    }
)


class SpikePayloadError(ValueError):
    """A Temporal payload carried private or unstructured source text."""


class SpikeTaskIdentity(DomainModel):
    """Public typed identity/ref only. No chapter body, Gold, or raw answer."""

    project_id: ProjectId
    run_id: RunId
    task_id: TaskId
    command_id: StableId
    effect_identity: StableId
    object_root: str = Field(min_length=1)
    candidate_kind: Literal["plan", "draft"] = "draft"


class SpikeLeafResult(DomainModel):
    task_id: TaskId
    command_id: StableId
    effect_identity: StableId
    candidate_kind: Literal["plan", "draft"]
    duplicate: bool
    artifact_name: str = Field(min_length=1)


class SpikeNsEvent(DomainModel):
    event_id: StableId
    run_id: RunId
    task_id: TaskId
    event_type: str = Field(min_length=1)
    effect_identity: StableId


class SpikeReport(DomainModel):
    morphology: str = Field(min_length=1)
    sdk_version: str = Field(min_length=1)
    plugin_available: bool
    plugin_version: str | None = None
    worker_build: str = Field(min_length=1)
    history_payload_types: tuple[str, ...] = ()
    history_bytes: int = Field(ge=0)
    activity_count: int = Field(ge=0)
    node_count: int = Field(ge=0)
    recovery_time_ms: int = Field(ge=0)
    duplicate_effect_count: int = Field(ge=0)
    business_effect_count: int = Field(ge=0)
    paused_resumed: bool = False
    unsupported_conditions: tuple[str, ...] = ()


def assert_public_payload(value: object) -> None:
    """Reject private source fields anywhere in a JSON-like Temporal payload."""

    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in FORBIDDEN_PAYLOAD_KEYS:
                raise SpikePayloadError(f"Temporal payload forbids field {key}")
            assert_public_payload(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            assert_public_payload(item)


def decode_spike_task(raw: dict[str, Any]) -> SpikeTaskIdentity:
    assert_public_payload(raw)
    return SpikeTaskIdentity.model_validate(raw)


class SpikeBusinessStore:
    """File-backed NS truth for the spike. Survives worker restart in the same object root."""

    def __init__(self, object_root: Path) -> None:
        self._root = object_root
        self._root.mkdir(parents=True, exist_ok=True)
        (self._root / "artifacts").mkdir(exist_ok=True)

    @property
    def path(self) -> Path:
        return self._root / "ns_store.json"

    def arm_hold(self) -> None:
        (self._root / "hold").write_text("1", encoding="utf-8")

    def release_hold(self) -> None:
        hold = self._root / "hold"
        if hold.exists():
            hold.unlink()

    def apply_leaf(self, identity: SpikeTaskIdentity) -> SpikeLeafResult:
        payload = self._load()
        key = identity.effect_identity.root
        existing = payload["effects"].get(key)
        if existing is not None:
            recorded = SpikeLeafResult.model_validate(existing)
            return recorded.model_copy(update={"duplicate": True})
        artifact_name = f"{key}.json"
        artifact = self._root / "artifacts" / artifact_name
        artifact.write_text(
            json.dumps(identity.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        result = SpikeLeafResult(
            task_id=identity.task_id,
            command_id=identity.command_id,
            effect_identity=identity.effect_identity,
            candidate_kind=identity.candidate_kind,
            duplicate=False,
            artifact_name=artifact_name,
        )
        payload["effects"][key] = result.model_dump(mode="json")
        payload["events"].append(
            SpikeNsEvent(
                event_id=StableId(f"event.{key}"),
                run_id=identity.run_id,
                task_id=identity.task_id,
                event_type="task.completed",
                effect_identity=identity.effect_identity,
            ).model_dump(mode="json")
        )
        self._save(payload)
        return result

    def effect_count(self) -> int:
        return len(self._load()["effects"])

    def event_count(self) -> int:
        return len(self._load()["events"])

    def wait_until_settled(self, effect_identity: str, *, timeout_seconds: float = 15.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if effect_identity in self._load()["effects"]:
                return
            time.sleep(0.05)
        raise TimeoutError(f"spike effect {effect_identity} did not settle")

    async def wait_until_settled_async(
        self, effect_identity: str, *, timeout_seconds: float = 15.0
    ) -> None:
        import asyncio

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if effect_identity in self._load()["effects"]:
                return
            await asyncio.sleep(0.05)
        raise TimeoutError(f"spike effect {effect_identity} did not settle")

    async def wait_hold_cleared(self) -> None:
        import asyncio

        hold = self._root / "hold"
        while hold.exists():
            await asyncio.sleep(0.05)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"effects": {}, "events": []}
        loaded = json.loads(self.path.read_text(encoding="utf-8"))
        effects = loaded.get("effects")
        events = loaded.get("events")
        if not isinstance(effects, dict) or not isinstance(events, list):
            raise SpikePayloadError("spike NS store is unreadable")
        return {"effects": effects, "events": events}

    def _save(self, payload: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)


def public_spike_task(*, object_root: Path, suffix: str = "a") -> SpikeTaskIdentity:
    return SpikeTaskIdentity(
        project_id=ProjectId("project.u35.public"),
        run_id=RunId(f"run.u35.{suffix}"),
        task_id=TaskId(f"task.u35.{suffix}"),
        command_id=StableId(f"cmd.u35.{suffix}"),
        effect_identity=StableId(f"effect.u35.{suffix}"),
        object_root=str(object_root.resolve()),
        candidate_kind="draft",
    )


def run_spike(object_root: Path) -> dict[str, Any]:
    """Run both morphologies against a local Temporal test server."""

    from novel_agent.runtime.temporal_langgraph_spike import run_both_morphologies

    return run_both_morphologies(object_root)
