# Novel Agent

Long-form fiction agent runtime developed from the contracts in the repository design documents.

## Documentation and current stage

Start with:

- `docs/README.md` for the authoritative document map and lifecycle;
- `docs/project_status.md` for current progress, blockers, and permitted transitions;
- `docs/adr/0005-stage-numbering-and-document-lifecycle.md` for canonical stage names.

The current project stage is Stage 2A development-complete with a conditional Memory Gate. Stage 2M
continues Writer Context benchmark diagnostics, while Stage 3 Writer Core preparation is active.
“Stage 2B” is a retired name retained only in historical branch, worktree, report, or schema
identifiers.

## Stage 0 bootstrap

The standard fresh-clone path is:

```bash
make stage0
```

It creates the project-local `.conda-env`, installs frozen Python dependencies, downloads the locked
official OpenSearch, MinIO, and OpenTelemetry Collector artifacts, verifies their checksums, starts
all four services as the current user on loopback-only ports, applies Alembic migrations, and runs
the deterministic interrupt/resume demo. Conda is required; Docker and sudo are not.

Credentials are generated into the ignored `.env` with mode `0600`. Service binaries, runtime
state, logs, and data stay under this repository's ignored `tmp/native` and `volumes/native` trees.
OpenSearch uses `node.store.allow_mmap: false` for functional testing on hosts whose
`vm.max_map_count` is below the production recommendation. Results from this mode are not
production performance evidence.

Useful commands:

```bash
make bootstrap
make infra-up
make infra-health
make integration
make infra-down
```

`make integration` creates suite-exclusive service processes, ports, credentials, and data
directories. Its four real infrastructure tests inject outages and verify recovery from the same
data. An unavailable service fails the run; it is never converted into a successful all-skip run.

Docker/Testcontainers remain an explicit parity backend:

```bash
make INFRA_BACKEND=docker integration
```

## Development checks

Run deterministic checks, which prohibit model endpoint calls:

```bash
make quality
```

The Stage 0 ASGI application is exposed as `novel_agent.api:app`. It intentionally contains only
the versioned `GET /health` operational contract; creative project APIs remain out of scope until
their application services and authorization contracts exist.

Inspect the effective non-secret application configuration with:

```bash
.conda-env/bin/novel-agent doctor
```

## Stage 1 memory kernel

The Stage 1 engineering smoke suite is deterministic and does not call a model endpoint:

```bash
make stage1-smoke
```

It validates the normalized `BenchmarkBundle`, history-only evidence boundary, Oracle/System track
interfaces, intent routing, typed Anchor/Grounded candidate pools, application-owned RRF, evidence
expansion, context budgets, Curator/Overlay/Validator write-back, atomic outbox propagation,
freshness decisions, and the license-free synthetic 20→3 read / 21→22→23 replay fixtures.

Run a user-supplied read-side bundle with:

```bash
.conda-env/bin/python scripts/run_stage1_benchmark.py /path/to/bundle.json \
  --case-id case.example --track oracle_verified
```

The locked real BGE-M3 / BGE-reranker CPU path is explicit and model-isolated:

```bash
make models-bootstrap
make models-up
make model-smoke
make stage1-native-benchmark \
  BUNDLE=/absolute/private/path/bundle.json \
  CASE_ID=case.example \
  OUTPUT=/absolute/private/path/result.json
make models-down
```

This path uses PostgreSQL R1 and OpenSearch BM25/k-NN with the locked BAAI model revisions. Model
weights remain ignored, endpoints bind only to loopback, and batch inference is always labelled
`batch_test_model`. Every inference call is written to `RunEventLog`; the 16 profile results are
also written to the append-only PostgreSQL Evaluation Ledger with the model revisions, runtime
fingerprints, aggregate local cost/latency, metrics, and failure codes. See
`docs/retrieval_model_runtime.md` for the artifact and PID safety contract.

Synthetic results prove the harness, not novel quality. Stage 1 cannot pass its formal quality gate
until an authorized real `BenchmarkBundle` supplies the 20→3 cases and a Gold-labelled replay of at
least 50 chapters. Current status is recorded in `docs/stage1_acceptance.md`.
