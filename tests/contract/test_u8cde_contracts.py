from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_agent.domain.base import DomainModel
from novel_agent.domain.evolution import (
    EvolutionCampaignManifest,
    EvolutionCampaignResult,
    EvolutionCandidate,
    EvolutionCandidateGenerationRequest,
    EvolutionPromotionReceipt,
    EvolutionRollbackReceipt,
)
from novel_agent.domain.recovery_reasoning import (
    RecoveryProposal,
    RecoveryReasonerAdmission,
    RecoveryReasonerRequest,
)

SCHEMAS = Path(__file__).parents[2] / "schemas" / "stage5"


@pytest.mark.parametrize(
    "model",
    (
        RecoveryReasonerAdmission,
        RecoveryReasonerRequest,
        RecoveryProposal,
        EvolutionCandidateGenerationRequest,
        EvolutionCandidate,
        EvolutionCampaignManifest,
        EvolutionCampaignResult,
        EvolutionPromotionReceipt,
        EvolutionRollbackReceipt,
    ),
)
def test_u8cde_public_schema_is_exported(model: type[DomainModel]) -> None:
    path = SCHEMAS / f"{model.__name__}.schema.json"

    assert json.loads(path.read_text(encoding="utf-8")) == model.model_json_schema()
