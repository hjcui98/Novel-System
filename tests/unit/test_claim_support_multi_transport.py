"""License-free regressions for Claim Support multi-slice transport identity."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from novel_agent.domain.ids import StableId
from novel_agent.services.claim_support import (
    ClaimSupportTransportConfig,
    MultiSliceClaimDraft,
)


def _claim(slice_ids: tuple[str, ...], facet_ids: tuple[str, ...]) -> MultiSliceClaimDraft:
    return MultiSliceClaimDraft(
        need_id=StableId("need.test.multi"),
        need_facet_ids=tuple(StableId(item) for item in facet_ids),
        slice_unit_ids=tuple(StableId(item) for item in slice_ids),
        claim_text="a synthesized multi-slice claim",
    )


def test_multi_slice_claim_rejects_duplicate_slice_ids() -> None:
    with pytest.raises(ValidationError, match="slice_unit_ids must be unique"):
        _claim(("slice.1", "slice.1"), ("facet.1",))


def test_multi_slice_claim_rejects_duplicate_facet_ids() -> None:
    with pytest.raises(ValidationError, match="need_facet_ids must be unique"):
        _claim(("slice.1", "slice.2"), ("facet.1", "facet.1"))


def test_multi_slice_claim_accepts_unique_references() -> None:
    claim = _claim(("slice.1", "slice.2"), ("facet.1", "facet.2"))
    assert claim.slice_unit_ids == (StableId("slice.1"), StableId("slice.2"))


def test_multi_slice_transport_config_rejects_out_of_range_penalty() -> None:
    with pytest.raises(ValueError, match="repetition penalty"):
        ClaimSupportTransportConfig(multi_repetition_penalty=0.0)
    with pytest.raises(ValueError, match="repetition penalty"):
        ClaimSupportTransportConfig(multi_repetition_penalty=2.5)


def test_multi_slice_transport_config_accepts_explicit_penalty() -> None:
    config = ClaimSupportTransportConfig(multi_repetition_penalty=1.10)
    assert config.multi_repetition_penalty == 1.10
