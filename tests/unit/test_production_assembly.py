from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from typing import Any, cast

import pytest
from scripts.run_stage5_runtime_evaluation import (
    _assert_input_artifacts_present,
    _assert_object_and_output_roots_disjoint,
    _CommitScopedRealHybridBackend,
    _database_descriptor,
    _isolated_stage4_policy,
    _redacted_argv,
)
from sqlalchemy import create_engine, text

from novel_agent.adapters.filesystem import FilesystemObjectStore
from novel_agent.adapters.model.fake import FakeModelEndpoint
from novel_agent.adapters.postgres.database import Base, build_engine, build_session_factory
from novel_agent.adapters.postgres.runtime import RuntimeTaskQueryRepository
from novel_agent.adapters.runtime.isolated import StrictFakePlanningLeaf
from novel_agent.domain.artifacts import ArtifactRef
from novel_agent.domain.creative_runtime import AutomationMode, CreativeRunPolicy
from novel_agent.domain.ids import ArtifactId, CommitId, ProjectId, RunId, SchemaVersion
from novel_agent.domain.model_calls import ModelRole
from novel_agent.domain.production_assembly import (
    ProductionAssemblySpec,
    ResolvedProductionAssemblyAttestation,
)
from novel_agent.domain.stage5_manifest import load_stage5_manifest
from novel_agent.runtime.creative_assembly import (
    DEFAULT_PRODUCTION_ASSEMBLY_FACTORY,
    ProductionAssemblyContext,
    ProductionRuntimeAssembly,
    build_production_assembly,
    load_production_runtime_assembly,
)
from novel_agent.runtime.production_bootstrap import (
    load_production_assembly_spec,
    preflight_production_environment,
)
from novel_agent.services.artifacts import ArtifactRepository
from novel_agent.services.model_gateway import RegisteredModelEndpoint
from novel_agent.services.retrieval import InMemoryRetrievalBackend, RetrievalBackend

HASH = ArtifactId("sha256:" + "1" * 64)
PERMISSION = "sha256:" + "2" * 64
MIGRATION_HEAD = "0010_model_call_ledger"
SCHEMAS = Path(__file__).parents[2] / "schemas" / "stage5"


def _policy() -> CreativeRunPolicy:
    return CreativeRunPolicy(
        automation_mode=AutomationMode.MANUAL,
        policy_hash=HASH.root,
        permission_hash=PERMISSION,
    )


def _stamp_sqlite(path: Path, *, head: str = MIGRATION_HEAD) -> str:
    url = f"sqlite+pysqlite:///{path}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(text("DELETE FROM alembic_version"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:head)"),
            {"head": head},
        )
    engine.dispose()
    return url


def _endpoints() -> tuple[RegisteredModelEndpoint, ...]:
    return (
        RegisteredModelEndpoint(
            role=ModelRole.IMPLEMENTATION,
            endpoint_name="fake-implementation",
            model_name="fake-v1",
            adapter=FakeModelEndpoint("{}"),
        ),
    )


def test_stage5_runner_planner_override_is_isolated_and_fail_closed() -> None:
    defaults = Namespace(planner_memory_rounds=None, planner_token_budget=None)
    assert _isolated_stage4_policy(defaults) is None

    override = Namespace(planner_memory_rounds=8, planner_token_budget=20_000)
    policy = _isolated_stage4_policy(override)
    assert policy is not None
    assert policy.budgets.planner_memory_rounds == 8
    assert policy.budgets.model_token_budget == 20_000

    with pytest.raises(ValueError, match="must not be negative"):
        _isolated_stage4_policy(Namespace(planner_memory_rounds=-1, planner_token_budget=None))
    with pytest.raises(ValueError, match="must be positive"):
        _isolated_stage4_policy(Namespace(planner_memory_rounds=None, planner_token_budget=0))


def test_stage5_runner_redacts_database_credentials_from_invocation() -> None:
    database_url = "postgresql+psycopg://user:secret@127.0.0.1:5432/isolated"

    assert _database_descriptor(database_url) == "postgresql+psycopg://127.0.0.1:5432/isolated"
    assert _redacted_argv(["runner", "--database-url", database_url, "--max-tasks", "1"]) == [
        "runner",
        "--database-url",
        "postgresql+psycopg://127.0.0.1:5432/isolated",
        "--max-tasks",
        "1",
    ]


def test_stage5_runner_rejects_overlapping_object_and_output_roots(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="disjoint object-store and output roots"):
        _assert_object_and_output_roots_disjoint(
            tmp_path / "shared",
            tmp_path / "shared" / "report.json",
        )

    _assert_object_and_output_roots_disjoint(
        tmp_path / "objects",
        tmp_path / "reports" / "report.json",
    )


def test_stage5_runner_rejects_missing_or_corrupt_input_artifacts(tmp_path: Path) -> None:
    artifact = ArtifactRef(
        artifact_id=ArtifactId("sha256:" + "1" * 64),
        media_type="text/markdown",
        byte_length=4,
        schema_version=SchemaVersion("1.0.0"),
    )
    missing_objects = tmp_path / "objects"
    missing_objects.mkdir()
    with pytest.raises(RuntimeError, match="input artifact is unavailable or invalid"):
        _assert_input_artifacts_present(missing_objects, (artifact,))

    objects = tmp_path / "objects-present"
    repository = ArtifactRepository(FilesystemObjectStore(objects))
    stored = repository.put(b"brief", "text/markdown", SchemaVersion("1.0.0"))
    with pytest.raises(RuntimeError, match="input artifact is unavailable or invalid"):
        _assert_input_artifacts_present(
            objects,
            (artifact.model_copy(update={"artifact_id": stored.artifact_id, "byte_length": 4}),),
        )
    _assert_input_artifacts_present(objects, (stored,))


def test_stage5_runner_real_hybrid_backend_is_commit_scoped() -> None:
    initial = CommitId("sha256:" + "3" * 64)
    next_commit = CommitId("sha256:" + "4" * 64)

    class Gateway:
        def __init__(self) -> None:
            self.calls: list[CommitId] = []

        def backend_for(self, _project: ProjectId, source_commit: CommitId) -> object:
            self.calls.append(source_commit)
            return type("Bundle", (), {"backend": object()})()

    gateway = Gateway()
    first = cast(RetrievalBackend, object())
    backend = _CommitScopedRealHybridBackend(
        project_id=ProjectId("project.test"),
        initial_commit=initial,
        initial_backend=first,
        gateway=gateway,  # type: ignore[arg-type]
    )

    assert backend.backend_for(initial) is first
    resolved = backend.backend_for(next_commit)
    assert resolved is backend.backend_for(next_commit)
    assert gateway.calls == [next_commit]


def _context(
    tmp_path: Path, *, url: str | None = None, **overrides: object
) -> ProductionAssemblyContext:
    manifest = load_stage5_manifest(
        Path(__file__).parents[2] / "src/novel_agent/runtime/stage5_development_manifest.json"
    )
    payload = {
        "database_url": url or _stamp_sqlite(tmp_path / "runtime.db"),
        "object_store_root": tmp_path / "objects",
        "project_id": ProjectId("project.test"),
        "run_id": RunId("run.production-factory"),
        "policy": _policy(),
        "manifest": manifest,
        "model_endpoints": _endpoints(),
        "retrieval_backend": InMemoryRetrievalBackend(()),
    }
    payload.update(overrides)
    return ProductionAssemblyContext(**payload)  # type: ignore[arg-type]


def test_production_assembly_spec_schema_is_exported() -> None:
    spec_schema = json.loads((SCHEMAS / "ProductionAssemblySpec.schema.json").read_text())
    attestation_schema = json.loads(
        (SCHEMAS / "ResolvedProductionAssemblyAttestation.schema.json").read_text()
    )
    assert spec_schema == ProductionAssemblySpec.model_json_schema()
    assert attestation_schema == ResolvedProductionAssemblyAttestation.model_json_schema()


def test_repo_spec_names_the_unique_factory_and_has_no_runtime_observations() -> None:
    spec = load_production_assembly_spec()
    dumped = spec.model_dump(mode="json")
    assert spec.factory_locator == DEFAULT_PRODUCTION_ASSEMBLY_FACTORY
    assert spec.expected_migration_head == MIGRATION_HEAD
    assert "migration_head" not in dumped
    assert "session_factory_identity" not in dumped
    assert "endpoints" not in dumped
    assert "secret" not in json.dumps(dumped)


def test_preflight_fails_closed_without_migration_head(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'empty.db'}"
    create_engine(url).dispose()
    spec = load_production_assembly_spec()
    with pytest.raises(RuntimeError, match="migration head"):
        preflight_production_environment(_context(tmp_path, url=url), spec)


def test_preflight_fails_closed_on_wrong_migration_head(tmp_path: Path) -> None:
    url = _stamp_sqlite(tmp_path / "wrong.db", head="0001_stage0_core")
    spec = load_production_assembly_spec()
    with pytest.raises(RuntimeError, match="migration head mismatch"):
        preflight_production_environment(_context(tmp_path, url=url), spec)


def test_factory_rejects_missing_model_endpoints(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="model endpoints"):
        build_production_assembly(_context(tmp_path, model_endpoints=()))


def test_factory_rejects_wrong_adapter_identity(tmp_path: Path) -> None:
    spec = load_production_assembly_spec().model_copy(
        update={"expected_planner_adapter": "tests.fixture.MissingAdapter"}
    )
    with pytest.raises(RuntimeError, match="adapter identity mismatch"):
        build_production_assembly(_context(tmp_path, spec=spec))


def test_factory_starts_and_freezes_attestation_without_model_calls(tmp_path: Path) -> None:
    context = _context(tmp_path)
    assembly = build_production_assembly(context)
    assert assembly.attestation is not None
    assert assembly.attestation.factory_locator == DEFAULT_PRODUCTION_ASSEMBLY_FACTORY
    assert assembly.attestation.migration_head == MIGRATION_HEAD
    assert assembly.attestation.session_factory_identity == str(id(assembly.session_factory))
    assert isinstance(assembly.task_reader, RuntimeTaskQueryRepository)
    assert assembly.task_reader.session_factory is assembly.session_factory
    assert assembly.model_gateway is not None
    assert assembly.model_gateway.admission_controller is not None
    assert assembly.attestation.reranker_declared is False
    assert assembly.memory_maintenance is not None
    assert assembly.runtime.memory_maintenance is assembly.memory_maintenance
    assert (
        assembly.attestation.memory_maintenance
        == "novel_agent.adapters.runtime.memory_maintenance.MemoryMaintenanceAdapter"
    )
    workflow = assembly.chapter_settlement._workflow
    assert workflow._risk_classifier is not None
    assert workflow._guardian is not None
    assert workflow._write_gate is not None
    maintenance = cast(Any, assembly.memory_maintenance)
    assert maintenance._workflow is workflow
    # A production maintenance task may be owned by Graph Curator.  The
    # attested memory-write owner must therefore carry the graph profile; an
    # ordinary-only assembly silently turns relation gaps into no-ops.
    assert maintenance._workflow._curator._graph_curator is not None
    assert maintenance._workflow._curator._graph_curator.gateway is assembly.model_gateway
    spec = load_production_assembly_spec()
    assert "migration_head" not in spec.model_dump()
    assert assembly.planner.is_fixture is False
    assert assembly.writer.is_fixture is False
    assert assembly.model_gateway.call_records == []


def test_major_rewrite_override_is_campaign_local_and_default_stays_one(tmp_path: Path) -> None:
    default = build_production_assembly(_context(tmp_path))
    campaign_root = tmp_path / "campaign-major-rewrite"
    campaign_root.mkdir()
    campaign = build_production_assembly(_context(campaign_root, max_major_rewrites=2))

    default_policy = default.writing_request_factory._policy
    campaign_policy = campaign.writing_request_factory._policy
    assert default_policy.budgets.max_major_rewrites == 1
    assert default_policy.budgets.max_post_draft_model_calls == 5
    assert campaign_policy.budgets.max_major_rewrites == 2
    assert campaign_policy.budgets.max_post_draft_model_calls == 7
    assert (
        campaign_policy.writer_configuration_fingerprint
        != default_policy.writer_configuration_fingerprint
    )


def test_local_repair_override_is_campaign_local_and_default_stays_one(tmp_path: Path) -> None:
    default = build_production_assembly(_context(tmp_path))
    campaign_root = tmp_path / "campaign-local-repair"
    campaign_root.mkdir()
    campaign = build_production_assembly(_context(campaign_root, max_local_repairs=2))

    default_policy = default.writing_request_factory._policy
    campaign_policy = campaign.writing_request_factory._policy
    assert default_policy.budgets.max_local_repairs == 1
    assert default_policy.budgets.max_post_draft_model_calls == 5
    assert campaign_policy.budgets.max_local_repairs == 2
    assert campaign_policy.budgets.max_post_draft_model_calls == 9
    assert (
        campaign_policy.writer_configuration_fingerprint
        != default_policy.writer_configuration_fingerprint
    )


def test_settlement_timeout_override_is_campaign_local(tmp_path: Path) -> None:
    default = build_production_assembly(_context(tmp_path))
    campaign_root = tmp_path / "campaign"
    campaign_root.mkdir()
    campaign = build_production_assembly(_context(campaign_root, settlement_timeout_seconds=120.0))

    default_policy = default.chapter_settlement._policy
    campaign_policy = campaign.chapter_settlement._policy
    assert default_policy.budget.model_transport.timeout_seconds == 60.0
    assert campaign_policy.budget.model_transport.timeout_seconds == 120.0
    assert campaign_policy.configuration_fingerprint != default_policy.configuration_fingerprint


def test_settlement_output_override_is_campaign_local(tmp_path: Path) -> None:
    default = build_production_assembly(_context(tmp_path))
    campaign_root = tmp_path / "campaign-output"
    campaign_root.mkdir()
    campaign = build_production_assembly(_context(campaign_root, settlement_output_tokens=12_000))

    default_policy = default.chapter_settlement._policy
    campaign_policy = campaign.chapter_settlement._policy
    default_curator = default.chapter_settlement._workflow._curator
    campaign_curator = campaign.chapter_settlement._workflow._curator
    assert default_curator is not None
    assert campaign_curator is not None
    default_factory = default_curator._request_factory
    campaign_factory = campaign_curator._request_factory
    assert default_factory._max_output_tokens == 8_000
    assert campaign_factory._max_output_tokens == 12_000
    assert campaign_policy.configuration_fingerprint != default_policy.configuration_fingerprint
    assert campaign.attestation is not None
    assert default.attestation is not None
    assert (
        campaign.attestation.configuration_fingerprint
        != default.attestation.configuration_fingerprint
    )


def test_settlement_token_budget_override_is_campaign_local(tmp_path: Path) -> None:
    default = build_production_assembly(_context(tmp_path))
    campaign_root = tmp_path / "campaign-token-budget"
    campaign_root.mkdir()
    campaign = build_production_assembly(_context(campaign_root, settlement_token_budget=60_000))

    default_policy = default.chapter_settlement._policy
    campaign_policy = campaign.chapter_settlement._policy
    assert default_policy.budget.token_budget == 24_000
    assert campaign_policy.budget.token_budget == 60_000
    assert campaign_policy.configuration_fingerprint != default_policy.configuration_fingerprint
    assert campaign.attestation is not None
    assert default.attestation is not None
    assert (
        campaign.attestation.configuration_fingerprint
        != default.attestation.configuration_fingerprint
    )


def test_settlement_max_total_model_calls_override_is_campaign_local(tmp_path: Path) -> None:
    default = build_production_assembly(_context(tmp_path))
    campaign_root = tmp_path / "campaign-model-calls"
    campaign_root.mkdir()
    campaign = build_production_assembly(
        _context(campaign_root, settlement_max_total_model_calls=8)
    )

    default_policy = default.chapter_settlement._policy
    campaign_policy = campaign.chapter_settlement._policy
    assert default_policy.budget.max_total_model_calls == 4
    assert campaign_policy.budget.max_total_model_calls == 8
    assert campaign_policy.configuration_fingerprint != default_policy.configuration_fingerprint


def test_settlement_max_total_model_calls_override_rejects_zero(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        build_production_assembly(_context(tmp_path, settlement_max_total_model_calls=0))


def test_settlement_timeout_override_rejects_explicit_policy(tmp_path: Path) -> None:
    explicit = build_production_assembly(_context(tmp_path)).chapter_settlement._policy
    conflict_root = tmp_path / "conflict"
    conflict_root.mkdir()
    with pytest.raises(ValueError, match="explicit settlement_policy"):
        build_production_assembly(
            _context(
                conflict_root,
                settlement_policy=explicit,
                settlement_timeout_seconds=120.0,
            )
        )


def test_settlement_output_override_rejects_explicit_policy(tmp_path: Path) -> None:
    explicit = build_production_assembly(_context(tmp_path)).chapter_settlement._policy
    conflict_root = tmp_path / "conflict-output"
    conflict_root.mkdir()
    with pytest.raises(ValueError, match="explicit settlement_policy"):
        build_production_assembly(
            _context(
                conflict_root,
                settlement_policy=explicit,
                settlement_output_tokens=12_000,
            )
        )


def test_cli_and_runner_resolve_the_same_factory_spec(tmp_path: Path) -> None:
    context = _context(tmp_path)
    via_loader = load_production_runtime_assembly(DEFAULT_PRODUCTION_ASSEMBLY_FACTORY, context)
    other = tmp_path / "second"
    other.mkdir()
    via_symbol = build_production_assembly(_context(other))
    assert via_loader.attestation is not None
    assert via_symbol.attestation is not None
    assert via_loader.attestation.factory_locator == via_symbol.attestation.factory_locator
    assert via_loader.attestation.planner_adapter == via_symbol.attestation.planner_adapter
    assert via_loader.attestation.writer_adapter == via_symbol.attestation.writer_adapter
    assert via_loader.attestation.chapter_settlement == via_symbol.attestation.chapter_settlement


def test_second_session_factory_fails_closed(tmp_path: Path) -> None:
    assembly = build_production_assembly(_context(tmp_path))
    other = RuntimeTaskQueryRepository(
        build_session_factory(build_engine(_stamp_sqlite(tmp_path / "other.db")))
    )
    with pytest.raises(ValueError, match="session factory"):
        ProductionRuntimeAssembly(
            runtime=assembly.runtime,
            dispatcher=assembly.dispatcher,
            planner=assembly.planner,
            planner_invocation_factory=assembly.planner_invocation_factory,
            writer=assembly.writer,
            writing_request_factory=assembly.writing_request_factory,
            plan_materializer=assembly.plan_materializer,
            draft_materializer=assembly.draft_materializer,
            chapter_settlement=assembly.chapter_settlement,
            task_reader=other,
            session_factory=assembly.session_factory,
            model_gateway=assembly.model_gateway,
            memory_gateway=assembly.memory_gateway,
            attestation=assembly.attestation,
        )


def test_fixture_planner_still_fails_production_admission(tmp_path: Path) -> None:
    from novel_agent.adapters.filesystem.object_store import FilesystemObjectStore
    from novel_agent.runtime.creative_assembly import validate_runtime_assembly
    from novel_agent.services.artifacts import ArtifactRepository

    assembly = build_production_assembly(_context(tmp_path))
    manifest = load_stage5_manifest(
        Path(__file__).parents[2] / "src/novel_agent/runtime/stage5_development_manifest.json"
    )
    fake = StrictFakePlanningLeaf(
        ArtifactRepository(FilesystemObjectStore(tmp_path / "fixture-objects"))
    )
    with pytest.raises(RuntimeError, match="fixture"):
        validate_runtime_assembly(
            manifest,
            planner=fake,
            writer=assembly.writer,
            plan_materializer=assembly.plan_materializer,
            draft_materializer=assembly.draft_materializer,
            chapter_settlement=assembly.chapter_settlement,
            production=True,
        )


def test_schema_version_constant() -> None:
    assert SchemaVersion("1.0.0").root == load_production_assembly_spec().spec_version.root
