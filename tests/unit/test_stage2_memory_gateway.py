from pathlib import Path

import pytest
from pydantic import ValidationError

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.ids import ArtifactId, StableId
from novel_agent.domain.stage2 import (
    ControllerArm,
    MemoryGatewayMode,
    MemoryGatewayPolicy,
    MemoryGatewayResult,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.memory_gateway import MemoryGateway, MemoryGatewayBlockedError
from tests.unit.test_stage2_paired_controller import (
    CONFIG,
    PRIVATE,
    VERSION,
    need,
    request,
    runner,
    text_root,
    unit,
)


def gateway(
    tmp_path: Path,
    mode: MemoryGatewayMode,
    *,
    fresh: bool = True,
    fallback: bool = True,
    source_artifact: ArtifactId = CONFIG,
    configuration: ArtifactId = CONFIG,
) -> tuple[MemoryGateway, ArtifactRepository]:
    item = need()
    repository = ArtifactRepository(FilesystemObjectStore(tmp_path / mode.value))
    service = MemoryGateway(
        runner(item, unit(source_artifact=source_artifact), fresh=fresh),
        MemoryGatewayPolicy(
            policy_id=StableId(f"gateway-policy.{mode.value}"),
            mode=mode,
            allow_deterministic_fallback=fallback,
            promotion_evidence=(
                ArtifactRef(
                    artifact_id=CONFIG,
                    media_type="application/vnd.novel-agent.stage2-gate-report+json",
                    byte_length=1,
                    schema_version=VERSION,
                )
                if mode is MemoryGatewayMode.BOUNDED_R2
                else None
            ),
            configuration_fingerprint=configuration,
        ),
        repository,
        schema_version=VERSION,
    )
    return service, repository


def test_memory_gateway_selects_and_freezes_safe_bounded_context(tmp_path: Path) -> None:
    service, repository = gateway(tmp_path, MemoryGatewayMode.BOUNDED_R2)
    item = need()
    result = service.resolve(request(item), text_root(), thread_id="gateway-bounded")

    assert result.selected_arm is ControllerArm.BOUNDED_R2
    assert result.fallback_used is False
    assert result.fallback_reason is None
    assert repository.read_verified(result.frozen_context_artifact)


def test_memory_gateway_supports_frozen_deterministic_profile(tmp_path: Path) -> None:
    service, repository = gateway(tmp_path, MemoryGatewayMode.DETERMINISTIC)
    item = need()
    result = service.resolve(request(item), text_root(), thread_id="gateway-deterministic")
    assert result.selected_arm is ControllerArm.DETERMINISTIC
    assert result.fallback_used is False
    assert repository.read_verified(result.frozen_context_artifact)


def test_memory_gateway_falls_back_on_controller_stop_or_future_leakage(tmp_path: Path) -> None:
    item = need()
    stale, _ = gateway(tmp_path, MemoryGatewayMode.BOUNDED_R2, fresh=False)
    stale_result = stale.resolve(request(item), text_root(), thread_id="gateway-stale")
    assert stale_result.selected_arm is ControllerArm.DETERMINISTIC
    assert stale_result.fallback_used is True
    assert stale_result.fallback_reason is not None
    assert "stopped" in stale_result.fallback_reason

    leaking, _ = gateway(
        tmp_path,
        MemoryGatewayMode.BOUNDED_R2,
        source_artifact=PRIVATE,
    )
    with pytest.raises(MemoryGatewayBlockedError, match="deterministic fallback"):
        leaking.resolve(
            request(item),
            text_root(),
            thread_id="gateway-leak",
            evaluator_only_artifacts=(PRIVATE,),
        )


def test_memory_gateway_blocks_when_bounded_is_ineligible_and_fallback_disabled(
    tmp_path: Path,
) -> None:
    service, _ = gateway(
        tmp_path,
        MemoryGatewayMode.BOUNDED_R2,
        fresh=False,
        fallback=False,
    )
    item = need()
    with pytest.raises(MemoryGatewayBlockedError, match="fallback is disabled"):
        service.resolve(request(item), text_root(), thread_id="gateway-blocked")


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("context", "context must equal"),
        ("fallback_flag", "flag and reason"),
        ("fallback_arm", "must select deterministic"),
        ("non_comparable", "non-comparable"),
        ("configuration", "configuration differs"),
    ),
)
def test_memory_gateway_result_rejects_unsafe_or_contradictory_selection(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    service, _ = gateway(tmp_path, MemoryGatewayMode.BOUNDED_R2)
    item = need()
    result = service.resolve(request(item), text_root(), thread_id=f"gateway-invalid-{mutation}")
    payload = result.model_dump()
    comparison = runner(item, unit()).run(
        request(item),
        text_root(),
        thread_id=f"gateway-invalid-pair-{mutation}",
    )
    payload["comparison"] = comparison.model_dump()
    if mutation == "context":
        payload["context"] = comparison.deterministic.context
    elif mutation == "fallback_flag":
        payload["fallback_used"] = True
    elif mutation == "fallback_arm":
        payload["fallback_used"] = True
        payload["fallback_reason"] = "forced"
    elif mutation == "non_comparable":
        payload["comparison"]["comparable"] = False
        payload["comparison"]["blockers"] = ("unsafe",)
    else:
        payload["configuration_fingerprint"] = PRIVATE
    with pytest.raises(ValidationError, match=message):
        MemoryGatewayResult.model_validate(payload)


def test_memory_gateway_policy_configuration_must_match_shared_pair(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="configuration differ"):
        gateway(
            tmp_path,
            MemoryGatewayMode.BOUNDED_R2,
            configuration=PRIVATE,
        )
