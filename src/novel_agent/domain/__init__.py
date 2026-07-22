"""Public Stage 0 domain contract."""

from novel_agent.domain.artifacts import (
    ArtifactRef,
    PlanRootRef,
    ProjectProfileRootRef,
    ReferenceRootRef,
    RootKind,
    RootManifest,
    TextRootRef,
    WorldRootRef,
)
from novel_agent.domain.base import DomainModel
from novel_agent.domain.changes import (
    CandidateChangeBundle,
    ChangeOperation,
    CommitRequest,
    CommitResult,
    ObservedChangeSet,
    ValidationReport,
)
from novel_agent.domain.context import (
    ContextAssemblyPlan,
    ContextPackage,
    EvidenceItem,
    EvidencePack,
    MemoryNeed,
    QueryContract,
)
from novel_agent.domain.evaluation import BenchmarkRunConfig, EvaluationParameter
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, RunId, SchemaVersion, TaskId
from novel_agent.domain.model_calls import (
    ModelCallRecord,
    ModelRequest,
    ModelTextResult,
    ModelUsage,
    ProviderModelResult,
)
from novel_agent.domain.runtime import EffectReceipt, EvaluationEntry, RunCheckpoint, RunEvent
from novel_agent.domain.text import EvidenceRef, QuoteHash, TextBlock, TextSpanRef
from novel_agent.domain.world import (
    Entity,
    Event,
    NarrativeOrder,
    PlanNode,
    RelationRecord,
    StateRecord,
    StoryTime,
    TruthClass,
)

__all__ = [
    "ArtifactId",
    "ArtifactRef",
    "BenchmarkRunConfig",
    "CandidateChangeBundle",
    "ChangeOperation",
    "CommitId",
    "CommitRequest",
    "CommitResult",
    "ContextAssemblyPlan",
    "ContextPackage",
    "DomainModel",
    "EffectReceipt",
    "Entity",
    "EvaluationEntry",
    "EvaluationParameter",
    "Event",
    "EvidenceItem",
    "EvidencePack",
    "EvidenceRef",
    "MemoryNeed",
    "ModelCallRecord",
    "ModelRequest",
    "ModelTextResult",
    "ModelUsage",
    "NarrativeOrder",
    "ObservedChangeSet",
    "PlanNode",
    "PlanRootRef",
    "ProjectId",
    "ProjectProfileRootRef",
    "ProviderModelResult",
    "QueryContract",
    "QuoteHash",
    "ReferenceRootRef",
    "RelationRecord",
    "RootKind",
    "RootManifest",
    "RunCheckpoint",
    "RunEvent",
    "RunId",
    "SchemaVersion",
    "StateRecord",
    "StoryTime",
    "TaskId",
    "TextBlock",
    "TextRootRef",
    "TextSpanRef",
    "TruthClass",
    "ValidationReport",
    "WorldRootRef",
]
