"""Fail-closed Stage 5 production/isolated assembly admission."""

from __future__ import annotations

from novel_agent.domain.stage5_manifest import Stage5DevelopmentManifest


def validate_runtime_assembly(
    manifest: Stage5DevelopmentManifest,
    *,
    planner: object,
    writer: object,
    plan_materializer: object,
    draft_materializer: object,
    production: bool,
) -> None:
    components = (planner, writer, plan_materializer, draft_materializer)
    fixture_flags = tuple(bool(getattr(item, "is_fixture", False)) for item in components)
    if production:
        if not manifest.feature_admission.real_stage4_adapter:
            raise RuntimeError("production Stage 5 requires an admitted real Stage 4 adapter")
        if any(fixture_flags):
            raise RuntimeError("production Stage 5 rejects fixture leaves and materializers")
    else:
        if not fixture_flags[0]:
            raise RuntimeError("isolated A-layer assembly requires the strict fake Planner")
        if fixture_flags[1]:
            raise RuntimeError(
                "isolated A-layer primary path requires the real Stage 3 Writer adapter"
            )


__all__ = ["validate_runtime_assembly"]
