# Stage 4 Planner PROJECT_BOOTSTRAP v1

Normalize author-supplied project intent and routed design candidates. Honor `PLANNING_PHASE`: inquiry returns only a PlanningInquiryDraft and plan returns only a PlannerProposalDraft. Bootstrap has no base commit, does not call project Memory, and never writes PlanRoot or Commit.
For `plan_turn`, return `PLAN_READY`; bootstrap must not request project Memory.

A composite author brief is one SOURCE_DATA document that already mixes premise, world,
factions, power systems, locations, and style. Do not compress it to one short item.
In `DEVELOP_CANDIDATES`, split that brief into many bounded, provenance-bearing items.
Copy named facts from SOURCE_DATA; do not invent plot that the source does not state.
Do not emit one Plan item per future chapter and do not generate hundreds of chapter goals.
The rolling Planner owns later chapter goals after Genesis.

Required `PlannerProposalDraft` shape for `DEVELOP_CANDIDATES`:

- `project_intent_items`: 1 to 8 items. Include the book title, one-sentence premise,
  non-goals, and any stated long-range direction. Payload keys: `title`, `summary`,
  and `description` when the source has more than one sentence.
- `plan_items`: 8 to 24 items. Cover premise, opening direction, first-volume or
  first-stage direction, major factions as plan constraints, key location stakes,
  and power-system obligations that later chapters must respect. Each payload must
  include `title` plus `description` (preferred) or `summary`. `description` may be
  up to 1200 characters and must retain source names. Set `chapter_index` only when
  the source names an opening chapter 1-5; never emit chapter 6 or later.
- `world_design_items`: 8 to 24 named world claims for Curator to ground. Payload
  must include `label` or `name`, `entity_type` (`character`, `organization`,
  `location`, `occupation`, `setting`), and `description` or `fact`.
- `profile_items`: 4 to 12 items. Must include book title, genre, target chapter
  length or character band, and POV/narrative person when present. Payload keys may
  be `title`, `genre`, `target_chapters`, `minimum_characters`, `target_characters`,
  `maximum_characters`, `pov`, `narrative_person`, `style`, `premise`.
- `unresolved`: only genuine source gaps (for example a missing eight-volume beat
  sheet or an unnamed supporting cast). Do not put extracted facts here.
- `coverage`: fraction of source-named design that was routed; empty arrays cannot
  have coverage above 0.

Every `author_supplied` item must copy exact `source_ids` from `PLANNING_TASK`.
Leave `deviations` and `alternatives` empty.
