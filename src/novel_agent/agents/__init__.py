"""Stage 2 and candidate-only Stage 3 agent public surface."""

from typing import Any

from novel_agent.agents.controller import StructuredControllerPolicy
from novel_agent.agents.curator import CuratorReplayAgent
from novel_agent.agents.curator_bootstrap import (
    CuratorBootstrapAgent,
    CuratorBootstrapInvocationError,
)
from novel_agent.agents.curator_repair import CuratorRepairAgent, CuratorRepairContractError
from novel_agent.agents.guardian import GuardianInvocationError, GuardianRiskReviewAgent
from novel_agent.agents.planner import PlannerAgent, PlannerInvocationError
from novel_agent.agents.registry import (
    AgentRegistry,
    RegistryError,
    agent_spec_content_id,
    seal_agent_spec,
    seal_tool_policy,
    tool_policy_content_id,
)
from novel_agent.agents.runner import (
    AgentExecutionError,
    AgentRunResult,
    PreparedAgentRun,
    StructuredAgentRunner,
)

__all__ = [
    "EDITOR_CONTRACT_VERSION",
    "EDITOR_DENIED_TOOLS",
    "EDITOR_MODES",
    "WRITER_DENIED_TOOLS",
    "WRITER_MODES",
    "AgentExecutionError",
    "AgentRegistry",
    "AgentRunResult",
    "CandidateObservationAgent",
    "CandidateObservationError",
    "CuratorBootstrapAgent",
    "CuratorBootstrapInvocationError",
    "CuratorRepairAgent",
    "CuratorRepairContractError",
    "CuratorReplayAgent",
    "EditorAgent",
    "EditorAgentError",
    "EditorContractBundle",
    "GuardianInvocationError",
    "GuardianRiskReviewAgent",
    "PlannerAgent",
    "PlannerInvocationError",
    "PreparedAgentRun",
    "RegistryError",
    "StructuredAgentRunner",
    "StructuredControllerPolicy",
    "WriterAgent",
    "WriterAgentError",
    "WriterContractBundle",
    "agent_spec_content_id",
    "build_editor_contract_bundle",
    "build_writer_contract_bundle",
    "seal_agent_spec",
    "seal_tool_policy",
    "tool_policy_content_id",
]


def __getattr__(name: str) -> Any:
    if name in {"CandidateObservationAgent", "CandidateObservationError"}:
        from novel_agent.agents.candidate_observer import (
            CandidateObservationAgent,
            CandidateObservationError,
        )

        return {
            "CandidateObservationAgent": CandidateObservationAgent,
            "CandidateObservationError": CandidateObservationError,
        }[name]
    if name in {
        "EditorAgent",
        "EditorAgentError",
        "EditorContractBundle",
        "EDITOR_CONTRACT_VERSION",
        "EDITOR_DENIED_TOOLS",
        "EDITOR_MODES",
        "build_editor_contract_bundle",
    }:
        from novel_agent.agents.editor import (
            EDITOR_CONTRACT_VERSION,
            EDITOR_DENIED_TOOLS,
            EDITOR_MODES,
            EditorAgent,
            EditorAgentError,
            EditorContractBundle,
            build_editor_contract_bundle,
        )

        return {
            "EditorAgent": EditorAgent,
            "EditorAgentError": EditorAgentError,
            "EditorContractBundle": EditorContractBundle,
            "EDITOR_CONTRACT_VERSION": EDITOR_CONTRACT_VERSION,
            "EDITOR_DENIED_TOOLS": EDITOR_DENIED_TOOLS,
            "EDITOR_MODES": EDITOR_MODES,
            "build_editor_contract_bundle": build_editor_contract_bundle,
        }[name]
    if name in {
        "WriterAgent",
        "WriterAgentError",
        "WriterContractBundle",
        "WRITER_DENIED_TOOLS",
        "WRITER_MODES",
        "build_writer_contract_bundle",
    }:
        from novel_agent.agents.writer import (
            WRITER_DENIED_TOOLS,
            WRITER_MODES,
            WriterAgent,
            WriterAgentError,
            WriterContractBundle,
            build_writer_contract_bundle,
        )

        return {
            "WriterAgent": WriterAgent,
            "WriterAgentError": WriterAgentError,
            "WriterContractBundle": WriterContractBundle,
            "WRITER_DENIED_TOOLS": WRITER_DENIED_TOOLS,
            "WRITER_MODES": WRITER_MODES,
            "build_writer_contract_bundle": build_writer_contract_bundle,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
