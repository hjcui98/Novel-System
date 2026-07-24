"""Adapters that connect existing Stage 2 services to Stage 2W ports."""

from novel_agent.adapters.memory_write.teacher_forced import (
    CommitServiceMemoryWriteAdapter,
    InformationBoundaryRegistryAdapter,
    LegacyGuardianPortAdapter,
    LegacyRiskClassifierAdapter,
    LegacyWriteGateAdapter,
    ProjectionServiceReadinessAdapter,
    RefusingCommitPort,
    RepositoryCanonicalReadAdapter,
    TeacherForcedCuratorPort,
)

__all__ = [
    "CommitServiceMemoryWriteAdapter",
    "InformationBoundaryRegistryAdapter",
    "LegacyGuardianPortAdapter",
    "LegacyRiskClassifierAdapter",
    "LegacyWriteGateAdapter",
    "ProjectionServiceReadinessAdapter",
    "RefusingCommitPort",
    "RepositoryCanonicalReadAdapter",
    "TeacherForcedCuratorPort",
]
