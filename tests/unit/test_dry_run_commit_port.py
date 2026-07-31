"""WP7: Dry-run commit port must never accept a canonical commit."""

from __future__ import annotations

from types import SimpleNamespace

from novel_agent.adapters.memory_write import RefusingCommitPort
from novel_agent.domain.ids import CommitId, StableId
from novel_agent.ports.memory_write import MemoryWriteCommitStatus


def test_dry_run_commit_port_returns_typed_refusal_without_accepting() -> None:
    """A dry-run RefusingCommitPort returns a typed, side-effect-free refusal."""
    canonical = CommitId("sha256:" + "c" * 64)
    port = RefusingCommitPort(canonical_commit=canonical)

    request = SimpleNamespace(request_id=StableId("request.dry-run"))

    # The port must refuse even if called.
    result = port.resolve_or_replay_exact(request)  # type: ignore[arg-type]
    assert result.status == MemoryWriteCommitStatus.DRY_RUN_REFUSED.value
    assert port.calls == 1
    assert port.accepted_count == 0

    # Canonical commit must remain unchanged.
    assert port.current == canonical

    # Calling again still refuses.
    result2 = port.resolve_or_replay_exact(request)  # type: ignore[arg-type]
    assert result2.status == MemoryWriteCommitStatus.DRY_RUN_REFUSED.value
    assert port.calls == 2
    assert port.accepted_count == 0
    assert port.current == canonical
