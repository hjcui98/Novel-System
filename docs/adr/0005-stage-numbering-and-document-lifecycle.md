# ADR-0005: Canonical stage numbering and document lifecycle

- Status: accepted
- Date: 2026-07-31
- Decision owners: project architecture and delivery governance
- Supersedes: the Stage 2B and later numbering in the initial execution plan
- Superseded in part by: ADR-0006 for Stage 3 and later topology; Stage 0～2 naming and this ADR's
  document-lifecycle decision remain accepted
- Related index: `docs/README.md`
- Current status source: `docs/project_status.md`

## Context

The repository accumulated three overlapping numbering systems:

1. the early implementation document used `Phase 0` through `Phase 5`;
2. the formal execution plan introduced `Stage 0`, `Stage 1A/1B`, `Stage 2A/2B`, and
   `Stage 3` through `Stage 6`;
3. Stage 2 later gained the `Stage 2R`, `Stage 2W`, and `Stage 2M` workstreams.

This made `Stage 2B` look like another Memory workstream even though it is the first Writer
generation stage. It also made the old `Stage 3` ambiguous: some documents used it for advanced
agentic retrieval while current planning discussions used “Stage 3” for Writer Core.

Document status was similarly ambiguous. Long-lived architecture, active execution plans,
acceptance evidence, incident analysis, and historical snapshots lived in the same directory
without a common lifecycle vocabulary.

## Decision

The canonical project stage names are:

| Canonical stage | Name | Previous name |
|---|---|---|
| Stage 0 | Engineering Foundation | unchanged |
| Stage 1A | Memory Read Kernel | unchanged |
| Stage 1B | Memory Write Kernel | unchanged |
| Stage 2A | Memory Agent Harness and Real-Project Validation | unchanged |
| Stage 3 | Writer Core and Generation Quality | Stage 2B |
| Stage 4 | Advanced Agentic Retrieval and Risk Paths | Stage 3 |
| Stage 5 | Full Chapter and Volume Creation Loop | Stage 4 |
| Stage 6 | Long-Horizon Autonomous Operation | Stage 5 |
| Stage 7 | Controlled Experience/Skill Evolution and Production Expansion | Stage 6 |

`Stage 2R`, `Stage 2W`, and `Stage 2M` are named workstreams inside the Stage 2A program:

```text
Stage 2A
  ├─ Stage 2R: real hybrid retrieval and derived projection
  ├─ Stage 2W: memory write workflow and repair
  └─ Stage 2M: writer-facing memory benchmark closure
```

They are not stages between Stage 2A and Stage 3 and do not shift later numeric stages.

All new project-facing documents, issue titles, gates, reports, and schemas must use the canonical
stage name. A document may mention an old name only in an explicit migration note such as
“Stage 3 (formerly Stage 2B)”.

Historical Git branches, worktree directories, immutable report paths, artifact media types, and
already-published schema identifiers are not rewritten in place. They are recorded as legacy
identifiers and migrated through an explicit compatibility change before production use.

## Document lifecycle

Every maintained document must be classifiable as exactly one of:

| Lifecycle | Meaning |
|---|---|
| `AUTHORITATIVE` | Long-lived architecture, technical, execution, or governance source of truth |
| `ACTIVE` | Current stage execution plan or runbook |
| `ACCEPTED` | Accepted ADR or completed gate/acceptance evidence |
| `HISTORICAL` | Time-bound snapshot retained for audit; not current status |
| `SUPERSEDED` | Replaced by a named successor and retained only for compatibility |
| `DRAFT` | Proposed design that has not become an execution baseline |

`docs/project_status.md` is the only current progress source. Dated progress reports and result
documents must not silently override it.

## Consequences

- The former Stage 2B Writer documents become Stage 3 documents.
- The former Stage 3 through Stage 6 references shift to Stage 4 through Stage 7.
- The early `Phase` labels in the technical and architecture documents remain conceptual
  implementation phases; they are not project stage identifiers.
- Stage 2A may be development-complete with a conditional gate while Stage 2M continues a
  diagnostic quality program.
- Stage 3 preparation may proceed, but production or semantic promotion still depends on its own
  gates.

## Migration requirements

Before the isolated Writer implementation is merged:

1. rebase it onto the current main worktree;
2. rename project-facing `stage2b` schema/script/test/report namespaces to `stage3`, or publish an
   explicit versioned compatibility mapping;
3. replace the legacy `Stage1ContextPackage` handoff with the accepted
   `WriterContextPackage` read-side product;
4. rerun current repository quality gates and regenerate schemas;
5. preserve old branch and worktree names only as historical provenance.
